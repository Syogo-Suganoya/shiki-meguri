"""手配エージェント。

受取場所（自宅配送 / 店舗 / ロッカー）を当日の動線コミで比較し、
最良案を「起案」する。**金銭確定は本人同意必須**のため、このエージェントは
予約 API を自分の判断では呼ばない（§7-3）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agents.deps import Deps
from app.domain.models import (
    DEFAULT_CATEGORY,
    ConsentRequest,
    ConsentStatus,
    Event,
    EventStatus,
    EventType,
    Outfit,
    OutfitCandidate,
    PickupOption,
    RentalState,
    ReturnMethod,
    ReturnPlan,
    RoutePlan,
    UserProfile,
)
from app.domain.timeline import build_timeline

AGENT = "arrange-agent"


@dataclass
class PickupEvaluation:
    option: PickupOption
    plan: RoutePlan
    travel_minutes: int
    fee_yen: int
    fare_yen: int

    @property
    def score(self) -> float:
        """移動時間 1 分 = 30 円 とみなして総コストで並べる。"""
        penalty = 0 if self.plan.feasible else 10_000
        return self.travel_minutes * 30 + self.fee_yen + self.fare_yen + penalty


class ArrangeAgent:
    def __init__(self, deps: Deps) -> None:
        self._d = deps

    async def evaluate_pickups(
        self, event: Event, user: UserProfile
    ) -> list[PickupEvaluation]:
        """受取場所ごとに当日の逆算タイムラインを引いて比較する。"""

        now = self._d.clock.now()
        category = DEFAULT_CATEGORY[event.type]
        options = await self._d.rental.pickup_options(
            event_type=event.type,
            category=category,
            venue_station=event.schedule.venue_station,
            home_station=user.home_station,
            ceremony_start_at=event.schedule.ceremony_start_at,
        )

        evaluations: list[PickupEvaluation] = []
        for option in options:
            if option.kind == "home_delivery":
                leg_to_pickup = None
                leg_to_venue = await self._d.transit.search(
                    user.home_station,
                    event.schedule.venue_station,
                    event.schedule.ceremony_start_at,
                )
            else:
                leg_to_pickup = await self._d.transit.search(
                    user.home_station, option.station, event.schedule.ceremony_start_at
                )
                leg_to_venue = await self._d.transit.search(
                    option.station,
                    event.schedule.venue_station,
                    event.schedule.ceremony_start_at,
                )

            plan = build_timeline(
                ceremony_start_at=event.schedule.ceremony_start_at,
                venue_name=event.schedule.venue_name,
                venue_station=event.schedule.venue_station,
                home_station=user.home_station,
                category=category,
                pickup=option,
                leg_to_pickup=leg_to_pickup,
                leg_to_venue=leg_to_venue,
                arrival_buffer_minutes=self._d.settings.arrival_buffer_minutes,
                generated_at=now,
                now=now,
            )
            travel = sum(leg.total_minutes for leg in plan.legs)
            fare = sum(leg.fare_yen for leg in plan.legs)
            evaluations.append(
                PickupEvaluation(
                    option=option,
                    plan=plan,
                    travel_minutes=travel,
                    fee_yen=option.fee_yen,
                    fare_yen=fare,
                )
            )

        return sorted(evaluations, key=lambda e: e.score)

    async def propose(
        self,
        event: Event,
        user: UserProfile,
        outfit_id: str,
        pickup_id: str | None = None,
        replace: bool = False,
    ) -> tuple[Event, ConsentRequest]:
        """衣装＋受取場所を確定案としてまとめ、同意ゲートに載せる。

        `pickup_id` を渡せば受取場所を指名できる（省略時は総コスト最良）。
        すでに予約が確定している場合は `replace=True` を要求し、
        **先に事業者側の予約を取り消してから**新しい案を起案する。
        黙って上書きすると、事業者には予約が残ったままアプリ側だけ
        「予約していない」状態になる。
        """

        candidate = _find_candidate(event, outfit_id)
        event = await self._release_current(event, replace=replace)
        evaluations = await self.evaluate_pickups(event, user)
        if not evaluations:
            raise ValueError("受取可能な場所が見つかりませんでした")
        if pickup_id is None:
            best = evaluations[0]
        else:
            best = next(
                (e for e in evaluations if e.option.pickup_id == pickup_id), None
            )
            if best is None:
                raise ValueError(f"選べない受取場所です: {pickup_id}")

        now = self._d.clock.now()
        amount = candidate.rental_fee_yen + best.fee_yen
        breakdown = {
            "レンタル料": candidate.rental_fee_yen,
            f"受取（{best.option.label}）": best.fee_yen,
        }
        summary = (
            f"{candidate.name}を{best.option.name}で受け取り、"
            f"{event.schedule.venue_name}の{event.schedule.ceremony_start_at:%H:%M}開式に間に合う手配です。"
        )
        consent = ConsentRequest(
            consent_id=uuid.uuid4().hex[:12],
            event_id=event.event_id,
            action="reserve",
            summary=summary,
            amount_yen=amount,
            breakdown=breakdown,
            requested_by=AGENT,
            requested_at=now,
        )

        schedule = event.schedule.model_copy(update={"pickup": best.option})
        event = event.model_copy(
            update={
                "schedule": schedule,
                "outfit": Outfit(
                    candidate=candidate, state=RentalState.AWAITING_CONSENT
                ),
                "pickup_options": [e.option for e in evaluations],
                "route": best.plan,
                "consents": [*event.consents, consent],
                "status": EventStatus.AWAITING_CONSENT,
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)

        await self._d.audit.record(
            agent=AGENT,
            action="propose_reservation",
            basis=(
                f"受取候補{len(evaluations)}件を比較 → {best.option.name}"
                f"（移動{best.travel_minutes}分 / 手数料{best.fee_yen}円 / 運賃{best.fare_yen}円）"
            ),
            event_id=event.event_id,
            consent_ref=consent.consent_id,
            payload={
                "amount_yen": amount,
                "autonomous_execution": False,
                "comparison": [
                    {
                        "pickup_id": e.option.pickup_id,
                        "score": e.score,
                        "feasible": e.plan.feasible,
                        "warning": e.plan.warning,
                    }
                    for e in evaluations
                ],
            },
        )
        await self._notify(event, user, consent)
        return event, consent

    async def _release_current(self, event: Event, *, replace: bool) -> Event:
        """起案し直す前に、いまの手配を明示的に畳む。

        - 受取済み以降は変えられない（衣装が手元にある）
        - 予約確定済みは `replace` を要求し、事業者側の予約を取り消す
        - 承認待ちの起案は「差し替え」として取り下げる（承認札を二重に出さない）
        """

        outfit = event.outfit
        now = self._d.clock.now()

        if outfit is not None and outfit.state in {
            RentalState.PICKED_UP,
            RentalState.RETURNING,
            RentalState.RETURNED,
            RentalState.OVERDUE,
        }:
            raise ValueError("受取済みのため、衣装は変更できません")

        if outfit is not None and outfit.state is RentalState.RESERVED:
            if not replace:
                raise ValueError(
                    "すでに予約が確定しています。変更するには、いまの予約の取り消しが要ります"
                )
            if outfit.reservation_id:
                await self._d.rental.cancel(outfit.reservation_id)
                await self._d.audit.record(
                    agent=AGENT,
                    action="reservation_cancelled",
                    basis="本人が衣装を変更。差し替え前に事業者側の予約を取り消した。",
                    event_id=event.event_id,
                    payload={"reservation_id": outfit.reservation_id},
                )
                await self._d.chat.say(
                    event.uid,
                    f"予約（{outfit.reservation_id}）を取り消しました。"
                    "新しい内容をご確認のうえ、改めて承認をお願いします。",
                    kind="consent",
                    event_id=event.event_id,
                )

        # 承認待ちのまま残っている起案は取り下げる。
        superseded = [
            c.model_copy(update={"status": ConsentStatus.SUPERSEDED, "decided_at": now})
            if c.status is ConsentStatus.PENDING and c.action == "reserve"
            else c
            for c in event.consents
        ]
        return event.model_copy(update={"consents": superseded})

    async def decide(
        self, event: Event, consent_id: str, approved: bool, note: str | None = None
    ) -> Event:
        """本人の承認／却下を受けて初めて予約を実行する。"""

        consent = event.find_consent(consent_id)
        if consent is None:
            raise ValueError(f"同意リクエストが見つかりません: {consent_id}")
        if consent.status is not ConsentStatus.PENDING:
            raise ValueError("この同意リクエストは既に処理済みです")

        now = self._d.clock.now()
        decided = consent.model_copy(
            update={
                "status": ConsentStatus.APPROVED if approved else ConsentStatus.REJECTED,
                "decided_at": now,
                "note": note,
            }
        )
        consents = [decided if c.consent_id == consent_id else c for c in event.consents]

        if not approved:
            event = event.model_copy(
                update={
                    "consents": consents,
                    "status": EventStatus.OUTFIT_PROPOSED,
                    "outfit": None,
                    "updated_at": now,
                }
            )
            await self._d.repo.save_event(event)
            await self._d.audit.record(
                agent=AGENT,
                action="reservation_rejected",
                basis="本人が却下。予約 API は呼び出していない。",
                event_id=event.event_id,
                consent_ref=consent_id,
            )
            return event

        assert event.outfit is not None
        assert event.schedule.pickup is not None
        reservation_id = await self._d.rental.reserve(
            event.outfit.candidate.outfit_id, event.schedule.pickup.pickup_id
        )
        return_due_at = _return_due_at(event.type, event.schedule.effective_end_at)
        return_method, return_place = _return_plan_of(event.type, event.schedule.pickup)
        schedule = event.schedule.model_copy(update={"return_due_at": return_due_at})
        event = event.model_copy(
            update={
                "consents": consents,
                "schedule": schedule,
                "outfit": event.outfit.model_copy(
                    update={
                        "state": RentalState.RESERVED,
                        "reservation_id": reservation_id,
                        "reserved_at": now,
                    }
                ),
                "return_plan": ReturnPlan(
                    due_at=return_due_at, method=return_method, place=return_place
                ),
                "status": EventStatus.RESERVED,
                "ttl_at": event.schedule.effective_end_at
                + timedelta(days=self._d.settings.event_ttl_days),
                "updated_at": now,
            }
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="reservation_confirmed",
            basis=f"本人承認済み（{decided.amount_yen}円）→ レンタル事業者APIで予約確定",
            event_id=event.event_id,
            consent_ref=consent_id,
            payload={
                "reservation_id": reservation_id,
                "return_due_at": return_due_at.isoformat(),
                "ttl_at": event.ttl_at.isoformat() if event.ttl_at else None,
            },
        )
        # 動線エージェントがタイムラインを流す前に、確定の事実を先に伝える。
        await self._d.chat.say(
            event.uid,
            f"予約を確定しました（予約番号 {reservation_id}）。"
            f"返却期限は{return_due_at:%-m月%-d日 %H:%M}です。",
            kind="consent",
            event_id=event.event_id,
        )
        return event

    async def _notify(
        self, event: Event, user: UserProfile, consent: ConsentRequest
    ) -> None:
        # 承認を求める文面は LLM に書かせない。利用者はこの文を読んで承認するので、
        # 金額の書き違いや、入力に紛れた指示で文面が変わる余地を残さない。
        text = (
            f"{consent.summary} 合計{consent.amount_yen:,}円です。"
            "内容をご確認のうえ、承認をお願いします（承認まで確定しません）。"
        )
        await self._d.chat.say(
            event.uid, text, kind="consent", event_id=event.event_id
        )


def _find_candidate(event: Event, outfit_id: str) -> OutfitCandidate:
    for c in event.candidates:
        if c.outfit_id == outfit_id:
            return c
    raise ValueError(f"候補にない衣装です: {outfit_id}")


def _return_due_at(event_type: EventType, ceremony_end: datetime) -> datetime:
    if event_type is EventType.TOURISM:
        # 観光着物は当日返却。店舗の締めに合わせる。
        return ceremony_end.replace(hour=18, minute=30, second=0, microsecond=0)
    # それ以外は翌日 21:00 までを標準の返却期限とする。
    return (ceremony_end + timedelta(days=1)).replace(
        hour=21, minute=0, second=0, microsecond=0
    )


def _return_plan_of(event_type: EventType, pickup: PickupOption) -> tuple[ReturnMethod, str]:
    """返却手段と、その手段に対応する返却場所を組で決める。"""

    if event_type is EventType.TOURISM:
        # 観光着物は借りた店に返す（当日返却）。
        return ReturnMethod.STORE, pickup.name
    if pickup.kind == "locker":
        return ReturnMethod.LOCKER, pickup.name
    # 式後は疲れ・二次会・遠方帰路があるため、店舗に戻らせない。
    return ReturnMethod.CONVENIENCE_STORE, "最寄りのコンビニ"
