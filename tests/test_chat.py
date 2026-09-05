"""自前チャットの会話フロー（設計書 §4 Orchestrator「会話の受付」）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.agents import conversation
from app.agents.deps import Deps
from app.agents.orchestrator import Orchestrator
from app.domain.models import EventStatus, EventType, RentalState
from tests.conftest import NOW


def _texts(messages) -> str:
    return "\n".join(m.text for m in messages if m.role == "agent")


# ---------------------------------------------------------------- 入力の解釈


@pytest.mark.parametrize(
    ("text", "hour", "days_ahead"),
    [
        ("明日16時から", 16, 1),
        ("明後日 15:30 です", 15, 2),
        ("今日の23時", 23, 0),
    ],
)
def test_日時の解釈(text: str, hour: int, days_ahead: int):
    parsed = conversation.parse_datetime(text, NOW)
    assert parsed is not None
    assert parsed.hour == hour
    assert (parsed.date() - NOW.date()).days == days_ahead


def test_日付指定がなく時刻を過ぎていれば翌日とみなす():
    # NOW は 8:00。「7時」は今日はもう過ぎている。
    parsed = conversation.parse_datetime("7時に集合", NOW)
    assert parsed is not None
    assert (parsed.date() - NOW.date()).days == 1


def test_最寄り駅と会場駅を語順から見分ける():
    home, venue = conversation.find_stations("最寄りは吉祥寺です。会場は品川です")
    assert home == "吉祥寺"
    assert venue == "品川"


# ---------------------------------------------------------------- 会話フロー


async def test_足りない情報を聞き返す(orchestrator: Orchestrator):
    messages, event = await orchestrator.handle_message("u1", "結婚式に呼ばれました")

    assert event is None
    assert "会場の最寄り駅" in _texts(messages)
    assert "式の日時" in _texts(messages)


async def test_会話だけで候補提示まで進む(orchestrator: Orchestrator, deps: Deps):
    await orchestrator.handle_message("u1", "最寄りは吉祥寺です")
    messages, event = await orchestrator.handle_message(
        "u1", "明日16時、品川の結婚式にお呼ばれ"
    )

    assert event is not None
    assert event.type is EventType.WEDDING
    assert event.status is EventStatus.OUTFIT_PROPOSED
    assert event.schedule.venue_station == "品川"
    assert event.schedule.ceremony_start_at.hour == 16
    # 発言を跨いで最寄り駅が保持されている。
    user = await deps.repo.get_user("u1")
    assert user.home_station == "吉祥寺"
    assert "1." in _texts(messages)


async def test_番号選択から承認までチャットで完結する(orchestrator: Orchestrator):
    await orchestrator.handle_message("u1", "最寄りは吉祥寺です")
    _, event = await orchestrator.handle_message("u1", "明日16時、品川の結婚式")

    messages, event = await orchestrator.handle_message("u1", "1番でお願いします")
    assert event.status is EventStatus.AWAITING_CONSENT
    assert event.outfit.reservation_id is None  # まだ確定しない
    assert "承認" in _texts(messages)

    messages, event = await orchestrator.handle_message("u1", "承認します")
    assert event.status is EventStatus.ROUTED
    assert event.outfit.state is RentalState.RESERVED
    assert event.route.departure_at is not None


async def test_却下すると候補提示に戻る(orchestrator: Orchestrator):
    await orchestrator.handle_message("u1", "最寄りは吉祥寺です")
    await orchestrator.handle_message("u1", "明日16時、品川の結婚式")
    await orchestrator.handle_message("u1", "2番")

    messages, event = await orchestrator.handle_message("u1", "却下")
    assert event.status is EventStatus.OUTFIT_PROPOSED
    assert event.outfit is None
    assert "取り下げ" in _texts(messages)


async def test_返却を尋ねると期限を答える(orchestrator: Orchestrator):
    await orchestrator.handle_message("u1", "最寄りは吉祥寺です")
    await orchestrator.handle_message("u1", "明日16時、品川の結婚式")
    await orchestrator.handle_message("u1", "1番")
    await orchestrator.handle_message("u1", "承認")

    messages, _ = await orchestrator.handle_message("u1", "返却はいつまで？")
    assert "返却" in _texts(messages)


async def test_弔事はチャットからも判定される(orchestrator: Orchestrator):
    _, event = await orchestrator.handle_message(
        "u1", "訃報がありました。明日13時、上野の斎場です"
    )

    assert event is not None
    assert event.type is EventType.FUNERAL


async def test_発言はすべてチャット履歴に残る(orchestrator: Orchestrator, deps: Deps):
    await orchestrator.handle_message("u1", "こんにちは")
    history = await deps.chat.history("u1")

    assert [m.role for m in history][0] == "user"
    assert any(m.role == "agent" for m in history)
