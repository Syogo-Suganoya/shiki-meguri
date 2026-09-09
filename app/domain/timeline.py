"""逆算タイムライン（設計書 §4 動線エージェント）。

式の開始時刻を起点に、会場到着 → 移動 → 着付け → 受取 → 出発 の順で
時間を「後ろから前へ」割り付ける純粋関数。外部 I/O を持たないため
受取場所を変えて何度でも呼び直せる。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import (
    OutfitCategory,
    PickupOption,
    RoutePlan,
    TimelineStep,
    TransitLeg,
)


def build_timeline(
    *,
    ceremony_start_at: datetime,
    venue_name: str,
    venue_station: str,
    home_station: str,
    category: OutfitCategory,
    pickup: PickupOption,
    leg_to_pickup: TransitLeg | None,
    leg_to_venue: TransitLeg,
    arrival_buffer_minutes: int,
    generated_at: datetime,
    return_due_at: datetime | None = None,
    return_place: str | None = None,
    now: datetime | None = None,
) -> RoutePlan:
    """式開始時刻から逆算して当日タイムラインを組み立てる。

    `pickup.kind == "home_delivery"` のときは受取のための移動が消え、
    自宅で着替えてそのまま会場へ向かう構成になる。
    """

    steps: list[TimelineStep] = []
    legs: list[TransitLeg] = []
    warnings: list[str] = []

    is_home = pickup.kind == "home_delivery"
    dressing_place = "自宅" if is_home else pickup.name
    dressing_minutes = category.dressing_minutes

    # --- 後ろから前へ ---------------------------------------------------
    arrive_end = ceremony_start_at
    arrive_start = arrive_end - timedelta(minutes=arrival_buffer_minutes)

    venue_walk = timedelta(minutes=leg_to_venue.transfers * 0)  # 予備（現状は 0）
    leg2_end = arrive_start - venue_walk
    leg2_start = leg2_end - timedelta(minutes=leg_to_venue.total_minutes)

    dressing_end = leg2_start
    dressing_start = dressing_end - timedelta(minutes=dressing_minutes)

    if is_home:
        handling_start = dressing_start
        departure_at = dressing_start
    else:
        handling_end = dressing_start
        handling_start = handling_end - timedelta(
            minutes=pickup.handling_minutes + pickup.walk_minutes
        )
        assert leg_to_pickup is not None, "店舗/ロッカー受取には受取地点までの経路が必要"
        leg1_end = handling_start
        leg1_start = leg1_end - timedelta(minutes=leg_to_pickup.total_minutes)
        departure_at = leg1_start

    # --- 前から後ろへ組み立て -------------------------------------------
    steps.append(
        TimelineStep(
            kind="depart",
            label="自宅を出発",
            place=f"{home_station}駅",
            starts_at=departure_at,
            ends_at=departure_at,
            detail=None if is_home else f"{pickup.label}（{pickup.name}）へ向かう",
        )
    )

    if not is_home:
        assert leg_to_pickup is not None
        legs.append(leg_to_pickup)
        steps.append(
            TimelineStep(
                kind="transit",
                label=f"{leg_to_pickup.from_station} → {leg_to_pickup.to_station}",
                place=" / ".join(leg_to_pickup.lines) or "移動",
                starts_at=leg1_start,
                ends_at=leg1_end,
                detail=_leg_detail(leg_to_pickup),
            )
        )
        steps.append(
            TimelineStep(
                kind="pickup",
                label=f"{pickup.label}で衣装を受け取る",
                place=pickup.name,
                starts_at=handling_start,
                ends_at=dressing_start,
                detail=f"徒歩{pickup.walk_minutes}分＋手続き{pickup.handling_minutes}分",
            )
        )

    steps.append(
        TimelineStep(
            kind="dressing",
            label="着付け" if category.needs_dresser else "着替え",
            place=dressing_place,
            starts_at=dressing_start,
            ends_at=dressing_end,
            detail=f"{category.label} / 所要{dressing_minutes}分",
        )
    )

    legs.append(leg_to_venue)
    same_station = leg_to_venue.from_station == leg_to_venue.to_station
    steps.append(
        TimelineStep(
            kind="transit",
            label="会場へ移動（徒歩圏）"
            if same_station
            else f"{leg_to_venue.from_station} → {leg_to_venue.to_station}",
            place=venue_name if same_station else " / ".join(leg_to_venue.lines) or "移動",
            starts_at=leg2_start,
            ends_at=leg2_end,
            detail=_leg_detail(leg_to_venue),
        )
    )
    steps.append(
        TimelineStep(
            kind="arrive",
            label="会場到着（受付前の余裕）",
            place=venue_name,
            starts_at=arrive_start,
            ends_at=arrive_end,
            detail=f"余裕{arrival_buffer_minutes}分",
        )
    )
    steps.append(
        TimelineStep(
            kind="ceremony",
            label="開式",
            place=venue_name,
            starts_at=ceremony_start_at,
            ends_at=ceremony_start_at,
        )
    )

    if return_due_at is not None:
        steps.append(
            TimelineStep(
                kind="return",
                label="返却期限",
                place=return_place or (pickup.name if pickup.accepts_return else "返却窓口"),
                starts_at=return_due_at,
                ends_at=return_due_at,
            )
        )

    # --- 実現可能性チェック ----------------------------------------------
    feasible = True
    if now is not None and departure_at < now:
        feasible = False
        late = int((now - departure_at).total_seconds() // 60)
        warnings.append(f"出発時刻を{late}分過ぎています。受取場所か手段の変更が必要です。")

    if not is_home:
        # closes_at_hour は 24（＝翌0時）を取り得るので、時刻ではなく
        # 「その日の何分目か」で比較する。
        minute_of_day = handling_start.hour * 60 + handling_start.minute
        if not (pickup.opens_at_hour * 60 <= minute_of_day <= pickup.closes_at_hour * 60):
            feasible = False
            warnings.append(
                f"{pickup.name}の営業時間外（{pickup.opens_at_hour}:00–{pickup.closes_at_hour}:00）に受取が入っています。"
            )
        if category.needs_dresser and not pickup.supports_dressing:
            feasible = False
            warnings.append(f"{category.label}の着付けに対応していない受取場所です。")

    return RoutePlan(
        steps=steps,
        legs=legs,
        generated_at=generated_at,
        feasible=feasible,
        warning=" ".join(warnings) or None,
    )


def _leg_detail(leg: TransitLeg) -> str:
    if leg.from_station == leg.to_station:
        # 受取場所が会場の最寄りと同じとき。「0分 / 0円 / 乗換0回」は読ませても意味がない。
        return "同じ駅。歩いて向かう"
    parts = [f"{leg.total_minutes}分", f"{leg.fare_yen}円", f"乗換{leg.transfers}回"]
    return " / ".join(parts)
