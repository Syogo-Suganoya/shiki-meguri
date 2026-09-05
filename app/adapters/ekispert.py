"""駅すぱあと API MCPサーバー アダプタ（設計書 §5 スポンサー）。

mock は決定的な擬似ダイヤを返す。live は MCP サーバーへ HTTP で問い合わせる。
どちらも同じ `TransitLeg` を返すため、上位の動線エージェントは差を意識しない。
"""

from __future__ import annotations

import abc
import math
from datetime import datetime

import httpx

from app.config import Settings
from app.domain.models import TransitLeg

# デモ用の駅座標（緯度・経度）。mock の所要時間・運賃はここから導く。
STATION_COORDS: dict[str, tuple[float, float]] = {
    "東京": (35.681, 139.767),
    "品川": (35.628, 139.739),
    "渋谷": (35.658, 139.701),
    "新宿": (35.690, 139.700),
    "池袋": (35.729, 139.711),
    "上野": (35.714, 139.777),
    "浅草": (35.711, 139.797),
    "銀座": (35.671, 139.765),
    "恵比寿": (35.646, 139.710),
    "目黒": (35.633, 139.715),
    "有楽町": (35.675, 139.763),
    "赤坂見附": (35.677, 139.737),
    "吉祥寺": (35.703, 139.579),
    "横浜": (35.465, 139.622),
    "大宮": (35.906, 139.623),
    "千葉": (35.613, 140.113),
    "京都": (34.985, 135.758),
    "祇園四条": (35.003, 135.771),
    "嵐山": (35.014, 135.677),
}

LINES_BY_HUB: dict[str, list[str]] = {
    "東京": ["JR山手線"],
    "品川": ["JR山手線"],
    "渋谷": ["JR山手線", "東京メトロ半蔵門線"],
    "新宿": ["JR中央線"],
    "池袋": ["JR山手線"],
    "上野": ["JR山手線"],
    "浅草": ["東京メトロ銀座線"],
    "銀座": ["東京メトロ銀座線"],
    "恵比寿": ["JR山手線"],
    "目黒": ["JR山手線"],
    "有楽町": ["JR山手線"],
    "赤坂見附": ["東京メトロ銀座線"],
    "吉祥寺": ["JR中央線"],
    "横浜": ["JR東海道線"],
    "大宮": ["JR埼京線"],
    "千葉": ["JR総武線"],
    "京都": ["JR京都線"],
    "祇園四条": ["京阪本線"],
    "嵐山": ["嵐電嵐山本線"],
}


class TransitClient(abc.ABC):
    @abc.abstractmethod
    async def search(
        self, from_station: str, to_station: str, arrive_by: datetime | None = None
    ) -> TransitLeg: ...


class MockTransitClient(TransitClient):
    """座標から距離を出し、所要時間・運賃・乗換回数を決定的に算出する。"""

    async def search(
        self, from_station: str, to_station: str, arrive_by: datetime | None = None
    ) -> TransitLeg:
        km = _distance_km(from_station, to_station)
        duration = max(6, int(km * 2.6) + 6)
        fare = 140 + int(km) * 22
        transfers = 0 if km < 6 else 1 if km < 25 else 2
        lines = _lines_between(from_station, to_station)
        return TransitLeg(
            from_station=from_station,
            to_station=to_station,
            duration_minutes=duration,
            fare_yen=fare,
            transfers=transfers,
            lines=lines,
        )



class EkispertMcpClient(TransitClient):
    """駅すぱあと MCP サーバー（live）。"""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def search(
        self, from_station: str, to_station: str, arrive_by: datetime | None = None
    ) -> TransitLeg:
        payload = {
            "from": from_station,
            "to": to_station,
            "arriveBy": arrive_by.isoformat() if arrive_by else None,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"{self._base_url}/tools/search_course",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            res.raise_for_status()
            data = res.json()
        return TransitLeg(
            from_station=from_station,
            to_station=to_station,
            duration_minutes=int(data["durationMinutes"]),
            fare_yen=int(data.get("fareYen", 0)),
            transfers=int(data.get("transfers", 0)),
            lines=list(data.get("lines", [])),
        )



def _distance_km(a: str, b: str) -> float:
    pa = STATION_COORDS.get(a)
    pb = STATION_COORDS.get(b)
    if pa is None or pb is None:
        # 未知の駅は名前から決定的に距離を作る（デモが止まらないように）。
        seed = sum(ord(c) for c in f"{a}{b}")
        return 3.0 + (seed % 17)
    dlat = (pa[0] - pb[0]) * 111.0
    dlon = (pa[1] - pb[1]) * 91.0
    return math.hypot(dlat, dlon)


def _lines_between(a: str, b: str) -> list[str]:
    lines = LINES_BY_HUB.get(a, ["JR線"]) + LINES_BY_HUB.get(b, ["JR線"])
    seen: list[str] = []
    for line in lines:
        if line not in seen:
            seen.append(line)
    return seen[:2]


def build_transit_client(settings: Settings) -> TransitClient:
    if settings.ekispert_mode == "live" and settings.ekispert_mcp_url:
        return EkispertMcpClient(settings.ekispert_mcp_url, settings.ekispert_api_key)
    return MockTransitClient()
