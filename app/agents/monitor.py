"""返却監視エージェント（設計書 §4）。

Cloud Scheduler から定期的に叩かれ、返却期限との残り時間を評価する。
自律性は「提案まで」。延長は料金が発生するため同意ゲートを立てる（§7-3）。
"""

from __future__ import annotations

import uuid

from app.agents.deps import Deps
from app.domain.models import (
    ConsentRequest,
    Event,
    EventStatus,
    RentalState,
    ReturnAlert,
    UserProfile,
)

AGENT = "return-agent"

WARN_MINUTES = 180
CRITICAL_MINUTES = 60
EXTENSION_MINUTES = 120


class ReturnMonitorAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    async def check(self, event: Event, user: UserProfile) -> tuple[Event, ReturnAlert | None]:
        plan = event.return_plan
        if plan is None or plan.completed_at is not None:
            return event, None

        now = self._d.clock.now()
        remaining = plan.remaining_minutes(now)
        if remaining > WARN_MINUTES:
            return event, None

        level = "info" if remaining > CRITICAL_MINUTES else "warn" if remaining > 0 else "critical"
        message = await self._d.llm.compose(
            purpose="return_reminder",
            context={
                "remaining": max(remaining, 0),
                "method_label": plan.method.label,
                "place": plan.place or "返却窓口",
            },
            mourning=event.type.is_mourning,
        )

        extension_fee: int | None = None
        consents = event.consents
        if level == "critical" and event.outfit is not None:
            extension_fee = await self._d.rental.extension_fee(
                event.outfit.candidate.outfit_id, EXTENSION_MINUTES
            )
            consent = ConsentRequest(
                consent_id=uuid.uuid4().hex[:12],
                event_id=event.event_id,
                action="extend",
                summary=f"返却期限を{EXTENSION_MINUTES}分延長します（延滞金の発生を回避）。",
                amount_yen=extension_fee,
                breakdown={"延長料": extension_fee},
                requested_by=AGENT,
                requested_at=now,
            )
            consents = [*event.consents, consent]
            message = f"{message} 延長（{EXTENSION_MINUTES}分・{extension_fee:,}円）も起案しました。"

        alert = ReturnAlert(
            alert_id=uuid.uuid4().hex[:12],
            level=level,  # type: ignore[arg-type]
            message=message,
            remaining_minutes=remaining,
            suggested_method=plan.method,
            suggested_place=plan.place,
            extension_fee_yen=extension_fee,
            created_at=now,
        )

        outfit = event.outfit
        if outfit is not None and remaining <= 0 and outfit.state is not RentalState.RETURNED:
            outfit = outfit.model_copy(update={"state": RentalState.OVERDUE})

        event = event.model_copy(
            update={
                "alerts": [*event.alerts, alert],
                "consents": consents,
                "outfit": outfit,
                "status": EventStatus.RETURN_PENDING,
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="return_alert",
            basis=f"返却期限まで{remaining}分 → {level}",
            event_id=event.event_id,
            consent_ref=consents[-1].consent_id if extension_fee is not None else None,
            payload={
                "remaining_minutes": remaining,
                "suggested_method": plan.method.value,
                "extension_fee_yen": extension_fee,
                "autonomous_execution": False,
            },
        )
        await self._d.chat.say(
            event.uid, message, kind="return", event_id=event.event_id, at=now
        )
        return event, alert

    async def mark_returned(self, event: Event) -> Event:
        if event.return_plan is None:
            raise ValueError("返却プランがありません")
        now = self._d.clock.now()
        event = event.model_copy(
            update={
                "return_plan": event.return_plan.model_copy(update={"completed_at": now}),
                "outfit": event.outfit.model_copy(update={"state": RentalState.RETURNED})
                if event.outfit
                else None,
                "status": EventStatus.COMPLETED,
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="return_completed",
            basis="本人が返却完了を報告",
            event_id=event.event_id,
        )
        return event
