"""式ムービー工房エージェント（設計書 §11 追加案）。

写真を集める → 構成を組む → 補正 → 動画化 → BGM、までを一本にする。
GMI Cloud を使う工程だけをまとめ、本体（衣装・動線・返却）とは疎に保つ。

自律性は**提案まで**。生成には課金が発生するため、レンダリングの実行は
本体と同じ同意ゲートを通す（設計書 §7-3）。さらにその手前に、
**第三者が写る写真の利用同意**という別のゲートを置く（設計書 §11 ガバナンス）。
"""

from __future__ import annotations

import uuid

from app.agents.deps import Deps
from app.domain.models import (
    ConsentRequest,
    Event,
    MovieProject,
    MovieScene,
    MovieStatus,
    PhotoAsset,
)

AGENT = "movie-agent"


class MovieAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    # ------------------------------------------------------------ 素材

    async def add_photos(
        self, event: Event, photos: list[tuple[str, str | None, bool]]
    ) -> Event:
        """写真（参照）を登録する。`(image_ref, caption, 第三者が写るか)` の並び。"""

        now = self._d.clock.now()
        project = event.movie or MovieProject(
            project_id=uuid.uuid4().hex[:12], created_at=now, updated_at=now
        )
        added = [
            PhotoAsset(
                photo_id=uuid.uuid4().hex[:8],
                image_ref=image_ref,
                caption=caption,
                contains_third_party=third_party,
            )
            for image_ref, caption, third_party in photos
        ]
        project = project.model_copy(
            update={
                "photos": [*project.photos, *added],
                "updated_at": now,
            }
        )
        project = project.model_copy(update={"status": _status_for(project)})
        event = await self._save(event, project)

        await self._d.audit.record(
            agent=AGENT,
            action="add_photos",
            basis=(
                f"素材{len(added)}枚を登録。うち第三者が写る写真は"
                f"{sum(1 for p in added if p.contains_third_party)}枚（利用同意の確認が必要）"
            ),
            event_id=event.event_id,
            payload={
                "photo_ids": [p.photo_id for p in added],
                "source_image_retained": False,
            },
        )
        pending = project.pending_consent_photos()
        if pending:
            await self._d.chat.say(
                event.uid,
                f"写真を{len(added)}枚お預かりしました。"
                f"うち{len(pending)}枚にご本人以外が写っています。"
                "掲載してよいか確認してから制作に進みます。",
                kind="consent",
                event_id=event.event_id,
            )
        return event

    async def confirm_photo_consent(
        self, event: Event, photo_ids: list[str], confirmed: bool = True
    ) -> Event:
        """第三者が写る写真の利用同意を記録する。"""

        project = _require_project(event)
        now = self._d.clock.now()
        photos = [
            p.model_copy(update={"consent_confirmed": confirmed})
            if p.photo_id in photo_ids
            else p
            for p in project.photos
        ]
        project = project.model_copy(update={"photos": photos, "updated_at": now})
        project = project.model_copy(update={"status": _status_for(project)})
        event = await self._save(event, project)

        await self._d.audit.record(
            agent=AGENT,
            action="photo_consent_confirmed" if confirmed else "photo_consent_withdrawn",
            basis=f"第三者が写る写真の利用同意を{'確認' if confirmed else '取り消し'}（{len(photo_ids)}枚）",
            event_id=event.event_id,
            payload={"photo_ids": photo_ids, "autonomous_execution": False},
        )
        return event

    # ------------------------------------------------------------ 構成と起案

    async def propose(self, event: Event, theme: str) -> tuple[Event, ConsentRequest]:
        """構成（絵コンテ）を組み、制作費を同意ゲートに載せる。"""

        project = _require_project(event)
        pending = project.pending_consent_photos()
        if pending:
            raise ValueError(
                f"第三者が写る写真{len(pending)}枚の利用同意が未確認です。確認してから制作に進みます。"
            )
        usable = project.usable_photos
        if not usable:
            raise ValueError("素材写真がありません")

        now = self._d.clock.now()
        scenes: list[MovieScene] = []
        for order, photo in enumerate(usable, start=1):
            narration = await self._d.llm.compose(
                purpose="movie_scene",
                context={
                    "theme": theme,
                    "order": order,
                    "total": len(usable),
                    "caption": photo.caption or "",
                },
                mourning=event.type.is_mourning,
            )
            scenes.append(
                MovieScene(
                    scene_id=uuid.uuid4().hex[:8],
                    order=order,
                    photo_id=photo.photo_id,
                    title=f"シーン{order}",
                    narration=narration,
                    duration_seconds=self._d.settings.movie_scene_seconds,
                )
            )

        scene_cost = len(scenes) * self._d.settings.movie_scene_price_yen
        bgm_cost = self._d.settings.movie_bgm_price_yen
        consent = ConsentRequest(
            consent_id=uuid.uuid4().hex[:12],
            event_id=event.event_id,
            action="movie",
            summary=(
                f"「{theme}」で{len(scenes)}シーン"
                f"（各{self._d.settings.movie_scene_seconds}秒）とオリジナルBGMを制作します。"
            ),
            amount_yen=scene_cost + bgm_cost,
            breakdown={f"映像 {len(scenes)}シーン": scene_cost, "BGM": bgm_cost},
            requested_by=AGENT,
            requested_at=now,
        )

        project = project.model_copy(
            update={
                "theme": theme,
                "scenes": scenes,
                "status": MovieStatus.AWAITING_CONSENT,
                "updated_at": now,
            }
        )
        event = await self._save(event, project)
        event = event.model_copy(update={"consents": [*event.consents, consent]})
        await self._d.repo.save_event(event)

        await self._d.audit.record(
            agent=AGENT,
            action="propose_movie",
            basis=f"素材{len(usable)}枚から{len(scenes)}シーンの構成を起案（テーマ: {theme}）",
            event_id=event.event_id,
            consent_ref=consent.consent_id,
            payload={
                "amount_yen": consent.amount_yen,
                "autonomous_execution": False,
                "models": {
                    "restore": self._d.settings.gmi_restore_model,
                    "video": self._d.settings.gmi_video_model,
                    "music": self._d.settings.gmi_music_model,
                },
            },
        )
        await self._d.chat.say(
            event.uid,
            f"{consent.summary} 合計{consent.amount_yen:,}円です。"
            "承認をお願いします（承認まで生成は始めません）。",
            kind="consent",
            event_id=event.event_id,
        )
        return event, consent

    # ------------------------------------------------------------ 生成

    async def render(self, event: Event) -> Event:
        """承認後に GMI Cloud で実際に作る。ここで初めて課金が発生する。"""

        project = _require_project(event)
        if project.pending_consent_photos():
            raise ValueError("第三者が写る写真の利用同意が未確認です")

        now = self._d.clock.now()
        project = project.model_copy(update={"status": MovieStatus.RENDERING})
        event = await self._save(event, project)

        photos: list[PhotoAsset] = list(project.photos)
        scenes: list[MovieScene] = []
        for scene in sorted(project.scenes, key=lambda s: s.order):
            index = next(i for i, p in enumerate(photos) if p.photo_id == scene.photo_id)
            photo = photos[index]

            # 1) 年代のばらついた写真を補正して色調を揃える
            restored = await self._d.movie_studio.restore_photo(
                photo.image_ref, now, style=project.theme
            )
            photos[index] = photo.model_copy(
                update={"restored_url": restored.url, "restored_at": restored.generated_at}
            )

            # 2) 静止画に動きを付ける
            video = await self._d.movie_studio.image_to_video(
                restored.url, scene.narration, scene.duration_seconds, now
            )
            scenes.append(
                scene.model_copy(update={"video_url": video.url, "model": video.model})
            )
            await self._d.audit.record(
                agent=AGENT,
                action="render_scene",
                basis=f"GMI Cloud {restored.model} → {video.model} / シーン{scene.order}",
                event_id=event.event_id,
                payload={
                    "scene_id": scene.scene_id,
                    "restore_request_id": restored.request_id,
                    "video_request_id": video.request_id,
                    "watermark": video.watermark,
                },
            )

        # 3) 式のテーマに合わせた BGM（権利クリアランス不要）
        bgm_prompt = f"{project.theme} / {event.type.label} / 感動的で穏やか"
        bgm = await self._d.movie_studio.generate_music(bgm_prompt, now)

        project = project.model_copy(
            update={
                "photos": photos,
                "scenes": scenes,
                "bgm_url": bgm.url,
                "bgm_prompt": bgm_prompt,
                "bgm_model": bgm.model,
                "status": MovieStatus.COMPLETED,
                # 素材も生成物も本体の TTL で一緒に消える（設計書 §11）
                "delete_after": event.ttl_at,
                "updated_at": now,
            }
        )
        event = await self._save(event, project)

        await self._d.audit.record(
            agent=AGENT,
            action="movie_completed",
            basis=f"{len(scenes)}シーン + BGM を生成（{bgm.model}）",
            event_id=event.event_id,
            payload={
                "bgm_request_id": bgm.request_id,
                "watermark": project.watermark,
                "delete_after": event.ttl_at.isoformat() if event.ttl_at else None,
            },
        )
        await self._d.chat.say(
            event.uid,
            f"ムービーができました（{len(scenes)}シーン・BGM付き）。"
            f"AI生成物である旨の表示を入れています。素材と生成物は式後に自動削除されます。",
            event_id=event.event_id,
        )
        return event

    async def _save(self, event: Event, project: MovieProject) -> Event:
        event = event.model_copy(
            update={"movie": project, "updated_at": self._d.clock.now()}
        )
        await self._d.repo.save_event(event)
        return event


def _require_project(event: Event) -> MovieProject:
    if event.movie is None:
        raise ValueError("ムービーの素材がまだありません")
    return event.movie


def _status_for(project: MovieProject) -> MovieStatus:
    if project.status in {MovieStatus.RENDERING, MovieStatus.COMPLETED}:
        return project.status
    if project.pending_consent_photos():
        return MovieStatus.CONSENT_REQUIRED
    return MovieStatus.DRAFT
