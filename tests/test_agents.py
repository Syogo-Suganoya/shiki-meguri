"""エージェントの自律性と同意ゲートの検証（設計書 §4 / §7）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.agents.deps import Deps
from app.agents.orchestrator import Orchestrator
from app.domain.models import (
    ConsentStatus,
    EventStatus,
    EventType,
    RentalState,
)
from tests.conftest import NOW

CEREMONY = NOW + timedelta(hours=5)


async def _prepare(orchestrator: Orchestrator, event_type: EventType = EventType.WEDDING):
    await orchestrator.register_user(
        "u1", home_station="吉祥寺", size="M"
    )
    event = await orchestrator.create_event(
        uid="u1",
        event_type=event_type,
        ceremony_start_at=CEREMONY,
        ceremony_end_at=CEREMONY + timedelta(hours=3),
        venue_name="ベイサイド迎賓館",
        venue_station="品川",
    )
    return await orchestrator.propose_outfits(event.event_id, image_ref="photo-u1")


# ---------------------------------------------------------------- シーン判定


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("明後日の結婚式にお呼ばれしていて", EventType.WEDDING),
        ("急な訃報で明日の通夜に出ることになった", EventType.FUNERAL),
        ("成人式の振袖を探しています", EventType.SEIJIN),
        ("京都を着物で観光したい", EventType.TOURISM),
    ],
)
def test_シーン判定(orchestrator: Orchestrator, text: str, expected: EventType):
    detected, basis = orchestrator.detect_scene(text)
    assert detected is expected
    assert basis


# ---------------------------------------------------------------- 試着


async def test_パーソナルカラーはスコアのみ保存し画像は破棄する(
    orchestrator: Orchestrator, deps: Deps
):
    event = await _prepare(orchestrator)

    user = await deps.repo.get_user("u1")
    assert user is not None and user.personal_color is not None
    assert user.personal_color.source_image_destroyed_at is not None
    assert set(user.personal_color.scores) == {"spring", "summer", "autumn", "winter"}

    logs = await deps.audit.list()
    analyze = next(log for log in logs if log.action == "analyze_personal_color")
    assert analyze.image_destroyed_at is not None
    assert analyze.payload["stored"] == "scores_only"

    assert event.status is EventStatus.OUTFIT_PROPOSED
    assert len(event.candidates) == 3
    assert all(c.tryon_image_url for c in event.candidates)


async def test_パーソナルカラーに合う候補が上位に来る(orchestrator: Orchestrator, deps: Deps):
    event = await _prepare(orchestrator)
    scores = [c.match_score for c in event.candidates]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------- 同意ゲート


async def test_承認前は予約が確定しない(orchestrator: Orchestrator, deps: Deps):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )

    assert consent.status is ConsentStatus.PENDING
    assert event.status is EventStatus.AWAITING_CONSENT
    assert event.outfit is not None
    assert event.outfit.state is RentalState.AWAITING_CONSENT
    assert event.outfit.reservation_id is None

    log = next(
        log for log in await deps.audit.list() if log.action == "propose_reservation"
    )
    assert log.payload["autonomous_execution"] is False
    assert log.consent_ref == consent.consent_id


async def test_承認で予約確定と返却期限が入る(orchestrator: Orchestrator, deps: Deps):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    assert event.outfit is not None
    assert event.outfit.state is RentalState.RESERVED
    assert event.outfit.reservation_id is not None
    assert event.return_plan is not None
    assert event.schedule.return_due_at is not None
    # 承認後は動線まで自動で引かれる。
    assert event.status is EventStatus.ROUTED
    assert event.route is not None and event.route.departure_at is not None
    # TTL は式終了 + 7日。
    assert event.ttl_at == event.schedule.effective_end_at + timedelta(days=7)

    actions = [log.action for log in await deps.audit.list()]
    assert "reservation_confirmed" in actions


async def test_却下すると予約されない(orchestrator: Orchestrator, deps: Deps):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    event = await orchestrator.decide_consent(
        event.event_id, consent.consent_id, False, note="予算オーバー"
    )

    assert event.outfit is None
    assert event.status is EventStatus.OUTFIT_PROPOSED
    log = next(
        log for log in await deps.audit.list() if log.action == "reservation_rejected"
    )
    assert "予約 API は呼び出していない" in log.basis


async def test_同じ同意リクエストは二度使えない(orchestrator: Orchestrator):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    with pytest.raises(ValueError):
        await orchestrator.decide_consent(event.event_id, consent.consent_id, True)


# ---------------------------------------------------------------- 受取最適化


async def test_弔事は会場最寄りの店舗受取に限定される(orchestrator: Orchestrator, deps: Deps):
    event = await _prepare(orchestrator, EventType.FUNERAL)
    event, _ = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )

    assert {o.kind for o in event.pickup_options} == {"store"}


async def test_振袖は自宅配送が候補から外れる(orchestrator: Orchestrator):
    event = await _prepare(orchestrator, EventType.SEIJIN)
    event, _ = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )

    assert "home_delivery" not in {o.kind for o in event.pickup_options}


# ---------------------------------------------------------------- 遅延再計算


async def test_遅延は自律的に再計算され通知される(orchestrator: Orchestrator, deps: Deps):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)
    before = event.route.departure_at

    line = event.route.legs[0].lines[0]
    deps.transit.inject(line, 15)
    event = await orchestrator.recalculate(event.event_id)

    assert event.route.departure_at == before - timedelta(minutes=15)
    assert len(event.route.history) == 1
    assert event.route.history[0].delay_minutes == 15

    log = next(
        log for log in await deps.audit.list() if log.action == "recalculate_timeline"
    )
    assert log.payload["autonomous_execution"] is True
    assert any(m.kind == "delay" for m in await deps.chat.history("u1"))


async def test_同じ遅延を繰り返し通知しない(orchestrator: Orchestrator, deps: Deps):
    """定期実行のたびに同じ遅延を通知すると、利用者には騒音になる。"""

    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    deps.transit.inject(event.route.legs[0].lines[0], 15)
    event = await orchestrator.recalculate(event.event_id)
    after_first = len([m for m in await deps.chat.history("u1") if m.kind == "delay"])

    event = await orchestrator.recalculate(event.event_id)
    assert len(event.route.history) == 1
    assert (
        len([m for m in await deps.chat.history("u1") if m.kind == "delay"])
        == after_first
    )

    # 遅延が伸びたときは改めて通知する。
    deps.transit.inject(event.route.legs[0].lines[0], 25)
    event = await orchestrator.recalculate(event.event_id)
    assert len(event.route.history) == 2


async def test_遅延がなければ再計算しない(orchestrator: Orchestrator):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)

    event = await orchestrator.recalculate(event.event_id)
    assert event.route.history == []


# ---------------------------------------------------------------- 返却監視


async def _reserved(orchestrator: Orchestrator):
    event = await _prepare(orchestrator)
    event, consent = await orchestrator.propose_reservation(
        event.event_id, event.candidates[0].outfit_id
    )
    return await orchestrator.decide_consent(event.event_id, consent.consent_id, True)


async def test_期限まで余裕があれば通知しない(orchestrator: Orchestrator):
    event = await _reserved(orchestrator)
    event, alert = await orchestrator.check_return(event.event_id)
    assert alert is None


async def test_期限直前は警告し延長を起案する(orchestrator: Orchestrator, deps: Deps, clock):
    event = await _reserved(orchestrator)
    clock.set(event.return_plan.due_at - timedelta(minutes=30))

    event, alert = await orchestrator.check_return(event.event_id)
    assert alert is not None and alert.level == "warn"
    assert event.pending_consent() is None  # warn では金銭を伴う起案はしない

    clock.set(event.return_plan.due_at + timedelta(minutes=5))
    event, alert = await orchestrator.check_return(event.event_id)
    assert alert.level == "critical"
    assert alert.extension_fee_yen and alert.extension_fee_yen > 0
    assert event.outfit.state is RentalState.OVERDUE

    consent = event.pending_consent()
    assert consent is not None and consent.action == "extend"
    due_before = event.return_plan.due_at
    event = await orchestrator.decide_consent(event.event_id, consent.consent_id, True)
    assert event.return_plan.due_at > due_before


async def test_返却完了で式が閉じる(orchestrator: Orchestrator):
    event = await _reserved(orchestrator)
    event = await orchestrator.mark_returned(event.event_id)

    assert event.status is EventStatus.COMPLETED
    assert event.outfit.state is RentalState.RETURNED
    assert event.return_plan.completed_at is not None


# ---------------------------------------------------------------- 定期実行


async def test_sweepはTTL超過のイベントを削除する(orchestrator: Orchestrator, deps: Deps, clock):
    event = await _reserved(orchestrator)
    clock.set(event.ttl_at + timedelta(minutes=1))

    result = await orchestrator.sweep()
    assert event.event_id in result["purged"]
    assert await deps.repo.get_event(event.event_id) is None

    # 監査ログは残る（追記専用・イベント削除とは独立）。
    assert any(
        log.action == "purge_expired_events" for log in await deps.audit.list()
    )


# ---------------------------------------------------------------- データ最小化


async def test_慶弔の当事者情報を保持するフィールドがない(orchestrator: Orchestrator):
    event = await _prepare(orchestrator)
    fields = set(event.model_dump().keys()) | set(event.schedule.model_dump().keys())

    for forbidden in ("deceased", "relationship", "couple_name", "attendee_name"):
        assert forbidden not in fields
