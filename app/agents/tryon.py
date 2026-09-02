"""試着エージェント（設計書 §4）。

自律性は「提案まで」。候補の絞り込み（パーソナルカラー×ドレスコード）と
試着画像生成を行い、選定そのものは本人に委ねる。
"""

from __future__ import annotations

from datetime import timedelta

from app.agents.deps import Deps
from app.domain.models import (
    DEFAULT_CATEGORY,
    Event,
    EventStatus,
    OutfitCandidate,
    PersonalColor,
    UserProfile,
)
from app.adapters.rental import COLOR_SEASONS

AGENT = "tryon-agent"


class TryOnAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    async def analyze_personal_color(self, user: UserProfile, image_ref: str) -> UserProfile:
        """顔画像からパーソナルカラーを判定し、スコアだけを保存する。"""

        now = self._d.clock.now()
        result = await self._d.tryon.analyze_color_tones(image_ref, now)
        user = user.model_copy(update={"personal_color": result})
        await self._d.repo.save_user(user)
        await self._d.audit.record(
            agent=AGENT,
            action="analyze_personal_color",
            basis=f"YouCam Facial Color Tones → {result.season.label}",
            image_destroyed_at=result.source_image_destroyed_at,
            payload={"scores": result.scores, "stored": "scores_only"},
        )
        return user

    async def propose(
        self, event: Event, user: UserProfile, image_ref: str | None = None, limit: int = 3
    ) -> Event:
        """候補を絞り込み、上位のみ試着画像を生成する。"""

        now = self._d.clock.now()
        category = DEFAULT_CATEGORY[event.type]
        season = user.personal_color.season if user.personal_color else None

        candidates = await self._d.rental.search_outfits(category, user.size)
        scored = sorted(
            (self._score(c, season) for c in candidates),
            key=lambda c: (-c.match_score, c.rental_fee_yen),
        )[:limit]

        ttl_seconds = (
            self._d.settings.mourning_image_ttl_seconds
            if event.type.is_mourning
            else self._d.settings.tryon_image_ttl_seconds
        )

        proposed: list[OutfitCandidate] = []
        for candidate in scored:
            rationale = await self._d.llm.compose(
                purpose="outfit_rationale",
                context={
                    "name": candidate.name,
                    "color": candidate.color,
                    "size": candidate.size,
                    "season_label": season.label if season else "肌映り未解析",
                    "event_label": event.type.label,
                    # 適合度に応じて説明を変える（合わない色を「似合う」と書かない）
                    "fits": candidate.match_score >= 0.9,
                },
                mourning=event.type.is_mourning,
            )
            update = {"rationale": rationale}
            if image_ref:
                result = await self._d.tryon.try_on(image_ref, candidate.outfit_id, now)
                update["tryon_image_url"] = result.image_url
                # 生成物自体も TTL 付きで破棄する。弔事は保持をさらに短く。
                update["image_destroyed_at"] = result.generated_at + timedelta(
                    seconds=ttl_seconds
                )
                await self._d.audit.record(
                    agent=AGENT,
                    action="generate_tryon_image",
                    basis=f"YouCam AI Clothes Try-On / outfit={candidate.outfit_id}",
                    event_id=event.event_id,
                    image_destroyed_at=result.source_destroyed_at,
                    payload={
                        "source_image_retained": False,
                        "result_ttl_seconds": ttl_seconds,
                    },
                )
            proposed.append(candidate.model_copy(update=update))

        event = event.model_copy(
            update={
                "candidates": proposed,
                "status": EventStatus.OUTFIT_PROPOSED,
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="propose_outfits",
            basis=(
                f"{event.type.label} / {category.label} / "
                f"パーソナルカラー={season.label if season else '未解析'} で {len(proposed)}件に絞り込み"
            ),
            event_id=event.event_id,
            payload={"outfit_ids": [c.outfit_id for c in proposed]},
        )
        return event

    @staticmethod
    def _score(candidate: OutfitCandidate, season: PersonalColor | None) -> OutfitCandidate:
        seasons = COLOR_SEASONS.get(candidate.color, set())
        if not seasons:
            # 色の指定が効かない区分（喪服など）は価格の安さで並べる。
            score = 0.6
        elif season is None:
            score = 0.5
        elif season in seasons:
            score = 1.0
        else:
            score = 0.3
        return candidate.model_copy(update={"match_score": score})
