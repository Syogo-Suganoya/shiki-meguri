"""動線エージェント（設計書 §4）。

開式から逆算した当日タイムラインを引き、確定した内容を会話で伝える。
運行情報を見て自動で引き直す機能は持たない（プロダクトの判断で削除）。
"""

from __future__ import annotations

from app.agents.deps import Deps
from app.domain.models import (
    DEFAULT_CATEGORY,
    Event,
    EventStatus,
    RoutePlan,
    UserProfile,
)
from app.domain.timeline import build_timeline

AGENT = "route-agent"


class RouteAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    async def plan(self, event: Event, user: UserProfile) -> Event:
        """確定した受取場所で当日タイムラインを引き直して保存する。"""

        plan = await self._build(event, user)
        now = self._d.clock.now()
        event = event.model_copy(
            update={
                "route": plan,
                "status": EventStatus.ROUTED,
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="build_timeline",
            basis=(
                f"開式{event.schedule.ceremony_start_at:%H:%M}から逆算 → "
                f"出発{plan.departure_at:%H:%M}"
            ),
            event_id=event.event_id,
            payload={
                "feasible": plan.feasible,
                "warning": plan.warning,
                "steps": len(plan.steps),
            },
        )
        await self._notify_timeline(event, user, plan)
        return event

    async def _build(self, event: Event, user: UserProfile) -> RoutePlan:
        pickup = event.schedule.pickup
        if pickup is None:
            raise ValueError("受取場所が未確定です")

        if pickup.kind == "home_delivery":
            leg_to_pickup = None
            leg_to_venue = await self._d.transit.search(
                user.home_station,
                event.schedule.venue_station,
                event.schedule.ceremony_start_at,
            )
        else:
            leg_to_pickup = await self._d.transit.search(
                user.home_station, pickup.station, event.schedule.ceremony_start_at
            )
            leg_to_venue = await self._d.transit.search(
                pickup.station,
                event.schedule.venue_station,
                event.schedule.ceremony_start_at,
            )

        return build_timeline(
            ceremony_start_at=event.schedule.ceremony_start_at,
            venue_name=event.schedule.venue_name,
            venue_station=event.schedule.venue_station,
            home_station=user.home_station,
            category=DEFAULT_CATEGORY[event.type],
            pickup=pickup,
            leg_to_pickup=leg_to_pickup,
            leg_to_venue=leg_to_venue,
            arrival_buffer_minutes=self._d.settings.arrival_buffer_minutes,
            generated_at=self._d.clock.now(),
            return_due_at=event.schedule.return_due_at,
            return_place=event.return_plan.place if event.return_plan else None,
            now=self._d.clock.now(),
        )

    async def _notify_timeline(
        self, event: Event, user: UserProfile, plan: RoutePlan
    ) -> None:
        if plan.departure_at is None:
            return
        pickup = event.schedule.pickup
        text = await self._d.llm.compose(
            purpose="timeline_summary",
            context={
                "departure": f"{plan.departure_at:%H:%M}",
                "home_station": user.home_station,
                "pickup_label": pickup.name if pickup else "受取場所",
                "ceremony": f"{event.schedule.ceremony_start_at:%H:%M}",
            },
            mourning=event.type.is_mourning,
        )
        await self._d.chat.say(
            event.uid, text, kind="timeline", event_id=event.event_id
        )
