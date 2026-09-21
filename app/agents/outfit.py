"""衣装エージェント。

自律性は「提案まで」。式の区分とサイズで候補を絞り、費用の安い順に並べる。
選定そのものは本人に委ねる。
"""

from __future__ import annotations

from app.agents.deps import Deps
from app.domain.models import (
    DEFAULT_CATEGORY,
    Event,
    EventStatus,
    OutfitCandidate,
    UserProfile,
)

AGENT = "outfit-agent"


class OutfitAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    async def propose(self, event: Event, user: UserProfile, limit: int = 3) -> Event:
        """式の区分に合う候補を、費用の安い順に絞り込む。"""

        now = self._d.clock.now()
        category = DEFAULT_CATEGORY[event.type]

        candidates = await self._d.rental.search_outfits(category, user.size)
        picked = sorted(candidates, key=lambda c: c.rental_fee_yen)[:limit]

        proposed: list[OutfitCandidate] = []
        for candidate in picked:
            rationale = await self._d.llm.compose(
                purpose="outfit_rationale",
                context={
                    "name": candidate.name,
                    "color": candidate.color,
                    "size": candidate.size,
                    "event_label": event.type.label,
                },
                mourning=event.type.is_mourning,
            )
            proposed.append(candidate.model_copy(update={"rationale": rationale}))

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
                f"{event.type.label} / {category.label} / サイズ{user.size} で "
                f"{len(proposed)}件に絞り込み（費用の安い順）"
            ),
            event_id=event.event_id,
            payload={"outfit_ids": [c.outfit_id for c in proposed]},
        )
        return event
