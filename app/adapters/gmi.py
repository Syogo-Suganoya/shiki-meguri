"""GMI Cloud アダプタ（設計書 §11 追加案「式ムービー工房」）。

- bria-fibo-restore / restyle … 年代のばらついた写真の補正・色調統一
- Kling-Image2Video …………… 静止画に数秒の動きを付ける
- minimax-music-2.5 ………… 式のテーマに合わせたオリジナルBGM

GMI Cloud は「リクエストキュー」方式で、モデル種別によらず同じエンドポイントに
`{"model": ..., "payload": {...}}` を投げ、`request_id` を受け取って結果を取りに行く。
動画は非同期なのでポーリングし、音楽は同期で返る（応答形は同じ）。
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import Settings


@dataclass
class GeneratedAsset:
    """生成物。実体は保存せず URL と出所だけ持つ（設計書 §7-1 と同じ方針）。"""

    url: str
    model: str
    request_id: str
    generated_at: datetime
    # AI 生成物であることの明示（設計書 §11 ガバナンス）
    watermark: str = "AI生成"


class MovieStudioClient(abc.ABC):
    @abc.abstractmethod
    async def restore_photo(
        self, image_ref: str, at: datetime, style: str | None = None
    ) -> GeneratedAsset:
        """古い写真の補正。`style` を渡すと restyle で色調を揃える。"""

    @abc.abstractmethod
    async def image_to_video(
        self, image_ref: str, prompt: str, duration_seconds: int, at: datetime
    ) -> GeneratedAsset: ...

    @abc.abstractmethod
    async def generate_music(
        self, prompt: str, at: datetime, lyrics: str | None = None
    ) -> GeneratedAsset: ...


class MockMovieStudioClient(MovieStudioClient):
    """参照文字列のハッシュから決定的に URL を作る。実画像・実音源は扱わない。"""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @staticmethod
    def _token(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    async def restore_photo(
        self, image_ref: str, at: datetime, style: str | None = None
    ) -> GeneratedAsset:
        model = self._s.gmi_restyle_model if style else self._s.gmi_restore_model
        token = self._token(image_ref, style or "restore")
        return GeneratedAsset(
            url=f"/gmi/photos/{token}.png",
            model=model,
            request_id=f"mock-{token}",
            generated_at=at,
        )

    async def image_to_video(
        self, image_ref: str, prompt: str, duration_seconds: int, at: datetime
    ) -> GeneratedAsset:
        token = self._token(image_ref, prompt, str(duration_seconds))
        return GeneratedAsset(
            url=f"/gmi/videos/{token}.mp4",
            model=self._s.gmi_video_model,
            request_id=f"mock-{token}",
            generated_at=at,
        )

    async def generate_music(
        self, prompt: str, at: datetime, lyrics: str | None = None
    ) -> GeneratedAsset:
        token = self._token(prompt, lyrics or "")
        return GeneratedAsset(
            url=f"/gmi/music/{token}.mp3",
            model=self._s.gmi_music_model,
            request_id=f"mock-{token}",
            generated_at=at,
        )


class GmiCloudClient(MovieStudioClient):
    """live。API キーは Secret Manager から環境変数で注入する。"""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._s.gmi_api_key}",
            "Content-Type": "application/json",
        }

    async def _run(self, model: str, payload: dict, at: datetime) -> GeneratedAsset:
        """キューに積んで、結果が出るまで待つ。"""

        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                self._s.gmi_base_url,
                json={"model": model, "payload": payload},
                headers=self._headers(),
            )
            res.raise_for_status()
            body = res.json()
            request_id = body["request_id"]

            outcome = _outcome_of(body)
            deadline = asyncio.get_running_loop().time() + self._s.gmi_poll_timeout_seconds
            while outcome is None:
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError(f"GMI Cloud の生成が終わりません: {request_id}")
                await asyncio.sleep(5)
                poll = await client.get(
                    f"{self._s.gmi_base_url}/{request_id}", headers=self._headers()
                )
                poll.raise_for_status()
                body = poll.json()
                if body.get("status") in {"failed", "error"}:
                    raise RuntimeError(f"GMI Cloud の生成に失敗しました: {request_id}")
                outcome = _outcome_of(body)

        return GeneratedAsset(
            url=outcome, model=model, request_id=request_id, generated_at=at
        )

    async def restore_photo(
        self, image_ref: str, at: datetime, style: str | None = None
    ) -> GeneratedAsset:
        model = self._s.gmi_restyle_model if style else self._s.gmi_restore_model
        payload: dict = {"image_url": image_ref}
        if style:
            payload["prompt"] = style
        return await self._run(model, payload, at)

    async def image_to_video(
        self, image_ref: str, prompt: str, duration_seconds: int, at: datetime
    ) -> GeneratedAsset:
        return await self._run(
            self._s.gmi_video_model,
            {
                "image_url": image_ref,
                "prompt": prompt,
                "durationSeconds": str(duration_seconds),
                "aspectRatio": "16:9",
            },
            at,
        )

    async def generate_music(
        self, prompt: str, at: datetime, lyrics: str | None = None
    ) -> GeneratedAsset:
        return await self._run(
            self._s.gmi_music_model,
            {
                "prompt": prompt,
                "lyrics": lyrics or "",
                "sample_rate": 44100,
                "bitrate": 256000,
                "format": "mp3",
            },
            at,
        )


def _outcome_of(body: dict) -> str | None:
    """完了応答から成果物の URL を取り出す。未完了なら None。"""

    if body.get("status") != "success":
        return None
    outcome = body.get("outcome") or {}
    for key in ("video_url", "audio_url", "image_url", "url"):
        if outcome.get(key):
            return outcome[key]
    return None


def build_movie_studio_client(settings: Settings) -> MovieStudioClient:
    if settings.gmi_mode == "live" and settings.gmi_api_key:
        return GmiCloudClient(settings)
    return MockMovieStudioClient(settings)
