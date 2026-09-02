"""動線エージェント（設計書 §4）。

逆算タイムラインの生成と、当日の遅延監視・再計算を担う。
このエージェントだけは**再計算と通知を自律実行**する（金銭が動かないため）。
受取場所の変更が必要になった場合は手配エージェントの起案に引き渡す。
"""

from __future__ import annotations

from app.agents.deps import Deps
from app.domain.models import (
    DEFAULT_CATEGORY,
    Event,
    EventStatus,
    RoutePlan,
    TransitLeg,
    UserProfile,
)
from app.domain.timeline import apply_delays, build_timeline, make_recalc_record

AGENT = "route-agent"


class RouteAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    async def plan(self, event: Event, user: UserProfile) -> Event:
        """確定した受取場所で当日タイムラインを引き直して保存する。"""

        plan = await self._build(event, user, legs=None)
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

    async def recalculate(self, event: Event, user: UserProfile) -> Event:
        """運行情報を取り込んで再計算する。遅延がなければ何もしない。"""

        if event.route is None:
            return event

        lines = sorted({line for leg in event.route.legs for line in leg.lines})
        delays = await self._d.transit.disruptions(lines)
        if not delays:
            return event

        updated_legs = apply_delays(event.route.legs, delays)
        if all(leg.delay_minutes == 0 for leg in updated_legs):
            return event
        if [leg.delay_minutes for leg in updated_legs] == [
            leg.delay_minutes for leg in event.route.legs
        ]:
            # 反映済みの遅延。定期実行のたびに同じ通知を送らない。
            return event

        now = self._d.clock.now()
        plan = await self._build(event, user, legs=updated_legs)
        worst_line = max(delays, key=lambda line: delays[line])
        record = make_recalc_record(
            previous=event.route,
            current=plan,
            reason=f"{worst_line} 遅延{delays[worst_line]}分",
            recalculated_at=now,
        )
        plan = plan.model_copy(update={"history": [*event.route.history, record]})

        event = event.model_copy(
            update={
                "route": plan,
                "status": EventStatus.IN_PROGRESS,
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="recalculate_timeline",
            basis=f"{record.reason} → 出発を{record.delay_minutes}分前倒し",
            event_id=event.event_id,
            payload={
                "delays": delays,
                "new_departure_at": record.new_departure_at.isoformat(),
                "feasible": plan.feasible,
                "warning": plan.warning,
                "autonomous_execution": True,
            },
        )

        text = await self._d.llm.compose(
            purpose="delay_notice",
            context={
                "line": worst_line,
                "delay": delays[worst_line],
                "new_departure": f"{record.new_departure_at:%H:%M}",
                "feasible": plan.feasible,
            },
            mourning=event.type.is_mourning,
        )
        await self._d.chat.say(
            event.uid, text, kind="delay", event_id=event.event_id, at=now
        )
        return event

    async def _build(
        self, event: Event, user: UserProfile, legs: list[TransitLeg] | None
    ) -> RoutePlan:
        pickup = event.schedule.pickup
        if pickup is None:
            raise ValueError("受取場所が未確定です")

        if legs is None:
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
        elif pickup.kind == "home_delivery":
            leg_to_pickup, leg_to_venue = None, legs[-1]
        else:
            leg_to_pickup, leg_to_venue = legs[0], legs[-1]

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
