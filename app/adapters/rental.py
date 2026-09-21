"""レンタル事業者アダプタ（MVP ではモック）。

在庫検索・受取地点・予約・延長を 1 つの口にまとめる。MVP では mock のみ。
予約と延長は「金銭が動く操作」なので、必ず同意ゲート通過後に呼ばれる
（呼び出し側の責務。ここでは実行するだけ）。
"""

from __future__ import annotations

import abc
import uuid
from datetime import datetime

from app.config import Settings
from app.domain.models import (
    EventType,
    OutfitCandidate,
    OutfitCategory,
    PickupOption,
)

_CATALOG: dict[OutfitCategory, list[tuple[str, str, int, str]]] = {
    # (商品名, 色, レンタル料, 事業者)
    OutfitCategory.DRESS: [
        ("シフォンドレープドレス", "コーラルピンク", 9800, "MockRental A"),
        ("サテンロングドレス", "ロイヤルブルー", 12800, "MockRental A"),
        ("レースワンピース", "ラベンダー", 8800, "MockRental B"),
        ("タックドレス", "スモーキーブルー", 10800, "MockRental B"),
        ("ベロアドレス", "ワインレッド", 13800, "MockRental C"),
    ],
    OutfitCategory.FURISODE: [
        ("古典柄振袖 松竹梅", "ワインレッド", 39800, "MockKimono"),
        ("モダン振袖 幾何", "ロイヤルブルー", 45800, "MockKimono"),
        ("レトロ振袖 椿", "テラコッタ", 42800, "MockKimono"),
    ],
    OutfitCategory.HAKAMA: [
        ("袴セット 矢絣", "テラコッタ", 29800, "MockKimono"),
        ("袴セット 無地", "オリーブ", 25800, "MockKimono"),
    ],
    OutfitCategory.KIMONO: [
        ("小紋 花小紋", "コーラルピンク", 4800, "MockKimono"),
        ("小紋 縞", "スモーキーブルー", 4800, "MockKimono"),
        ("訪問着 淡彩", "アイボリー", 7800, "MockKimono"),
    ],
    OutfitCategory.MOFUKU: [
        ("ブラックフォーマル 一式", "ブラック", 6800, "MockFormal"),
        ("ブラックフォーマル 小物付", "ブラック", 8800, "MockFormal"),
    ],
}


class RentalClient(abc.ABC):
    @abc.abstractmethod
    async def search_outfits(
        self, category: OutfitCategory, size: str
    ) -> list[OutfitCandidate]: ...

    @abc.abstractmethod
    async def pickup_options(
        self,
        *,
        event_type: EventType,
        category: OutfitCategory,
        venue_station: str,
        home_station: str,
        ceremony_start_at: datetime,
    ) -> list[PickupOption]: ...

    @abc.abstractmethod
    async def reserve(self, outfit_id: str, pickup_id: str) -> str: ...

    @abc.abstractmethod
    async def cancel(self, reservation_id: str) -> None:
        """確定済みの予約を取り消す。衣装を変えるときに必ず通す。"""

    @abc.abstractmethod
    async def extension_fee(self, outfit_id: str, minutes: int) -> int: ...


class MockRentalClient(RentalClient):
    async def search_outfits(
        self, category: OutfitCategory, size: str
    ) -> list[OutfitCandidate]:
        items = _CATALOG[category]
        return [
            OutfitCandidate(
                outfit_id=f"{category.value}-{i:02d}",
                name=name,
                category=category,
                color=color,
                size=size,
                provider=provider,
                rental_fee_yen=fee,
            )
            for i, (name, color, fee, provider) in enumerate(items, start=1)
        ]

    async def pickup_options(
        self,
        *,
        event_type: EventType,
        category: OutfitCategory,
        venue_station: str,
        home_station: str,
        ceremony_start_at: datetime,
    ) -> list[PickupOption]:
        options = [
            PickupOption(
                pickup_id="home",
                kind="home_delivery",
                name="自宅配送",
                station=home_station,
                walk_minutes=0,
                handling_minutes=0,
                fee_yen=1200,
                opens_at_hour=0,
                closes_at_hour=23,
                supports_dressing=False,
                accepts_return=False,
            ),
            PickupOption(
                pickup_id="store-venue",
                kind="store",
                name=f"{venue_station}店",
                station=venue_station,
                walk_minutes=5,
                handling_minutes=15,
                fee_yen=0,
                opens_at_hour=9,
                closes_at_hour=20,
                supports_dressing=True,
                accepts_return=True,
            ),
            PickupOption(
                pickup_id="locker-venue",
                kind="locker",
                name=f"{venue_station}駅ロッカー",
                station=venue_station,
                walk_minutes=3,
                handling_minutes=5,
                fee_yen=500,
                opens_at_hour=5,
                closes_at_hour=24,
                supports_dressing=False,
                accepts_return=True,
            ),
        ]
        if category.needs_dresser:
            # 着付けが要る衣装は自宅配送では成立しない。
            options = [o for o in options if o.kind != "home_delivery"]
        if event_type is EventType.FUNERAL:
            # 弔事は即日性が最優先。会場最寄りの店舗のみを提示する。
            options = [o for o in options if o.kind == "store"]
        return options

    def __init__(self) -> None:
        # 取り消した予約番号。二重取消や取消漏れを検出できるように残す。
        self.cancelled: list[str] = []

    async def reserve(self, outfit_id: str, pickup_id: str) -> str:
        return f"MOCK-{outfit_id}-{pickup_id}-{uuid.uuid4().hex[:6].upper()}"

    async def cancel(self, reservation_id: str) -> None:
        self.cancelled.append(reservation_id)

    async def extension_fee(self, outfit_id: str, minutes: int) -> int:
        # 30 分単位・550 円刻みの想定。
        blocks = max(1, (minutes + 29) // 30)
        return blocks * 550


def build_rental_client(settings: Settings) -> RentalClient:
    # live 事業者 API は MVP 対象外。常に mock を返す。
    return MockRentalClient()
