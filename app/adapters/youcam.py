"""YouCam API アダプタ（設計書 §5 スポンサー）。

- Facial Color Tones Analyzer → パーソナルカラー（スコアのみ保持）
- AI Clothes Try-On → 試着画像

設計書 §7-1 に従い、顔・全身画像は結果生成後に必ず破棄する。破棄時刻は
呼び出し側が監査ログへ記録できるよう戻り値に含める。
"""

from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import Settings
from app.domain.models import PersonalColor, PersonalColorResult

SEASONS = [
    PersonalColor.SPRING,
    PersonalColor.SUMMER,
    PersonalColor.AUTUMN,
    PersonalColor.WINTER,
]


@dataclass
class TryOnResult:
    image_url: str
    generated_at: datetime
    source_destroyed_at: datetime
    """入力画像（顔・全身）を破棄した時刻。監査ログ用。"""


class TryOnClient(abc.ABC):
    @abc.abstractmethod
    async def analyze_color_tones(
        self, image_ref: str, analyzed_at: datetime
    ) -> PersonalColorResult: ...

    @abc.abstractmethod
    async def try_on(
        self, image_ref: str, outfit_id: str, generated_at: datetime
    ) -> TryOnResult: ...


class MockTryOnClient(TryOnClient):
    """画像参照文字列のハッシュから決定的に結果を作る。実画像は扱わない。"""

    async def analyze_color_tones(
        self, image_ref: str, analyzed_at: datetime
    ) -> PersonalColorResult:
        digest = hashlib.sha256(image_ref.encode()).digest()
        raw = [digest[i] + 1 for i in range(4)]
        total = sum(raw)
        scores = {
            season.value: round(value / total, 3) for season, value in zip(SEASONS, raw)
        }
        season = max(SEASONS, key=lambda s: scores[s.value])
        return PersonalColorResult(
            season=season,
            scores=scores,
            analyzed_at=analyzed_at,
            # 解析元画像は結果生成と同時に破棄する。
            source_image_destroyed_at=analyzed_at,
        )

    async def try_on(
        self, image_ref: str, outfit_id: str, generated_at: datetime
    ) -> TryOnResult:
        token = hashlib.sha256(f"{image_ref}:{outfit_id}".encode()).hexdigest()[:16]
        return TryOnResult(
            image_url=f"/tryon/{outfit_id}/{token}.png",
            generated_at=generated_at,
            source_destroyed_at=generated_at,
        )


class YouCamClient(TryOnClient):
    """live。API キーは Secret Manager から環境変数で注入する。"""

    BASE_URL = "https://yce-api-01.perfectcorp.com/s2s/v1.1"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "X-Secret-Key": self._secret_key,
        }

    async def analyze_color_tones(
        self, image_ref: str, analyzed_at: datetime
    ) -> PersonalColorResult:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{self.BASE_URL}/task/skin-analysis",
                json={"file_id": image_ref, "dst_actions": ["facial_color_tones"]},
                headers=self._headers(),
            )
            res.raise_for_status()
            data = res.json()
            # 結果取得後、アップロード済み画像を即時削除する（§7-1）。
            await client.delete(
                f"{self.BASE_URL}/file/{image_ref}", headers=self._headers()
            )

        scores = {k: float(v) for k, v in data["result"]["seasons"].items()}
        season = PersonalColor(max(scores, key=lambda k: scores[k]))
        return PersonalColorResult(
            season=season,
            scores=scores,
            analyzed_at=analyzed_at,
            source_image_destroyed_at=analyzed_at,
        )

    async def try_on(
        self, image_ref: str, outfit_id: str, generated_at: datetime
    ) -> TryOnResult:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                f"{self.BASE_URL}/task/clothes-try-on",
                json={"file_id": image_ref, "clothes_id": outfit_id},
                headers=self._headers(),
            )
            res.raise_for_status()
            data = res.json()
            await client.delete(
                f"{self.BASE_URL}/file/{image_ref}", headers=self._headers()
            )
        return TryOnResult(
            image_url=data["result"]["url"],
            generated_at=generated_at,
            source_destroyed_at=generated_at,
        )


def build_tryon_client(settings: Settings) -> TryOnClient:
    if settings.youcam_mode == "live" and settings.youcam_api_key:
        return YouCamClient(settings.youcam_api_key, settings.youcam_secret_key)
    return MockTryOnClient()
