"""逆算タイムライン（設計書 §4 動線エージェント）の検証。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import OutfitCategory, PickupOption, TransitLeg
from app.domain.timeline import apply_delays, build_timeline, make_recalc_record
from app.infra.clock import JST

CEREMONY = datetime(2026, 10, 10, 13, 0, tzinfo=JST)

STORE = PickupOption(
    pickup_id="store-venue",
    kind="store",
    name="品川店",
    station="品川",
    walk_minutes=5,
    handling_minutes=15,
    supports_dressing=True,
)

LEG_TO_PICKUP = TransitLeg(
    from_station="吉祥寺",
    to_station="品川",
    duration_minutes=35,
    fare_yen=400,
    transfers=1,
    lines=["JR中央線"],
)
LEG_TO_VENUE = TransitLeg(
    from_station="品川",
    to_station="品川",
    duration_minutes=8,
    fare_yen=0,
    transfers=0,
    lines=["JR山手線"],
)


def _build(**overrides):
    kwargs = dict(
        ceremony_start_at=CEREMONY,
        venue_name="ベイサイド迎賓館",
        venue_station="品川",
        home_station="吉祥寺",
        category=OutfitCategory.DRESS,
        pickup=STORE,
        leg_to_pickup=LEG_TO_PICKUP,
        leg_to_venue=LEG_TO_VENUE,
        arrival_buffer_minutes=15,
        generated_at=CEREMONY - timedelta(hours=5),
    )
    kwargs.update(overrides)
    return build_timeline(**kwargs)


def test_逆算で出発時刻が決まる():
    plan = _build()

    # 13:00 開式 − 余裕15 − 移動8 − 着替え20 − 受取(15+徒歩5) − 移動35 = 11:22
    assert plan.departure_at == datetime(2026, 10, 10, 11, 22, tzinfo=JST)
    assert plan.feasible is True
    assert [s.kind for s in plan.steps] == [
        "depart",
        "transit",
        "pickup",
        "dressing",
        "transit",
        "arrive",
        "ceremony",
    ]


def test_着付けが要る衣装は出発が早まる():
    dress = _build(category=OutfitCategory.DRESS).departure_at
    furisode = _build(category=OutfitCategory.FURISODE).departure_at

    delta = (dress - furisode).total_seconds() / 60
    assert delta == OutfitCategory.FURISODE.dressing_minutes - OutfitCategory.DRESS.dressing_minutes


def test_自宅配送は受取の移動が消える():
    home = PickupOption(
        pickup_id="home",
        kind="home_delivery",
        name="自宅配送",
        station="吉祥寺",
        handling_minutes=0,
    )
    plan = _build(
        pickup=home,
        leg_to_pickup=None,
        leg_to_venue=TransitLeg(
            from_station="吉祥寺",
            to_station="品川",
            duration_minutes=42,
            fare_yen=480,
            transfers=1,
            lines=["JR中央線"],
        ),
    )

    assert [s.kind for s in plan.steps] == [
        "depart",
        "dressing",
        "transit",
        "arrive",
        "ceremony",
    ]
    # 13:00 − 15 − 42 − 20 = 11:43（自宅で着替えてから出発）
    assert plan.departure_at == datetime(2026, 10, 10, 11, 43, tzinfo=JST)


def test_着付け非対応の受取場所は実現不能と判定される():
    locker = STORE.model_copy(update={"kind": "locker", "supports_dressing": False})
    plan = _build(pickup=locker, category=OutfitCategory.FURISODE)

    assert plan.feasible is False
    assert "着付け" in (plan.warning or "")


def test_出発時刻を過ぎていたら実現不能():
    plan = _build(now=datetime(2026, 10, 10, 12, 0, tzinfo=JST))

    assert plan.feasible is False
    assert "出発時刻" in (plan.warning or "")


def test_遅延を反映すると出発が前倒しになる():
    before = _build()
    delayed_legs = apply_delays(before.legs, {"JR中央線": 12})
    after = _build(leg_to_pickup=delayed_legs[0], leg_to_venue=delayed_legs[1])

    record = make_recalc_record(
        previous=before,
        current=after,
        reason="JR中央線 遅延12分",
        recalculated_at=CEREMONY - timedelta(hours=4),
    )
    assert record.delay_minutes == 12
    assert after.departure_at == before.departure_at - timedelta(minutes=12)
