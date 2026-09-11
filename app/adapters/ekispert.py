"""駅すぱあと API MCPサーバー アダプタ（設計書 §5 スポンサー）。

mock は決定的な擬似ダイヤを返す。live は MCP サーバーへ HTTP で問い合わせる。
どちらも同じ `TransitLeg` を返すため、上位の動線エージェントは差を意識しない。
"""

from __future__ import annotations

import abc
import asyncio
import json
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
        if from_station == to_station:
            # live 側（実サービス）も同じ駅には経路を返さない。振る舞いを揃える。
            return TransitLeg(
                from_station=from_station,
                to_station=to_station,
                duration_minutes=0,
                fare_yen=0,
                transfers=0,
                lines=[],
            )
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
    """駅すぱあと API MCPサーバー（live）。

    MCP の Streamable HTTP で `ekispert_api_search_routes` を呼ぶ。
    素の REST ではないので、JSON-RPC の初期化（initialize →
    notifications/initialized）を一度だけ済ませてから tools/call を投げる。

    探索は `plain`（平均待ち時間探索）のみ。着時刻からのダイヤ探索
    （searchType=arrival と time）は専用のアクセスキーが要るため、
    いまのキーではツールの引数として公開されない。開式からの逆算は
    `domain/timeline.py` が所要時間から自前で組むので、これで足りる。
    """

    #: 公式のエンドポイント。環境ごとに変わるものではないので設定にしない。
    URL = "https://api-mcp.ekispert.jp/mcp"
    #: 認証はこのヘッダ。Authorization ヘッダではない。
    KEY_HEADER = "ekispert-api-access-key"
    TOOL = "ekispert_api_search_routes"
    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, api_key: str, url: str | None = None, timeout: float = 20.0) -> None:
        self._url = url or self.URL
        self._headers = {
            self.KEY_HEADER: api_key,
            "ekispert-api-response-format": "json",
            # Streamable HTTP は JSON でも SSE でも返ってくる。両方受ける。
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.PROTOCOL_VERSION,
        }
        self._client = httpx.AsyncClient(timeout=timeout)
        self._ready: asyncio.Lock = asyncio.Lock()
        self._initialized = False
        self._rpc_id = 0

    async def search(
        self, from_station: str, to_station: str, arrive_by: datetime | None = None
    ) -> TransitLeg:
        if from_station == to_station:
            # 受取場所が会場の最寄りと同じことがある。探索しても経路は返らないし、
            # 呼べばアクセス数が加算されるので、ここで畳む。
            return TransitLeg(
                from_station=from_station,
                to_station=to_station,
                duration_minutes=0,
                fare_yen=0,
                transfers=0,
                lines=[],
            )
        args: dict[str, object] = {
            # 出発・経由・目的地をコロンで並べる。駅名のまま渡してよい。
            "viaList": f"{from_station}:{to_station}",
            "answerCount": 1,
        }
        if arrive_by is not None:
            # 時刻は指定できないが、日付は運賃改定やダイヤ改正の判定に効く。
            args["date"] = int(arrive_by.strftime("%Y%m%d"))
        result = await self._call_tool(self.TOOL, args)
        return _leg_from_course(from_station, to_station, result)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ MCP 下回り

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        await self._initialize()
        data = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = data["result"]
        if result.get("isError"):
            raise RuntimeError(f"駅すぱあと MCP がエラーを返しました: {_text_of(result)}")
        return json.loads(_text_of(result))

    async def _initialize(self) -> None:
        async with self._ready:
            if self._initialized:
                return
            await self._rpc(
                "initialize",
                {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "shiki-meguri", "version": "1.0"},
                },
            )
            await self._notify("notifications/initialized")
            self._initialized = True

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._rpc_id += 1
        body: dict[str, object] = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            body["params"] = params
        res = await self._client.post(self._url, json=body, headers=self._headers)
        res.raise_for_status()
        data = _decode(res)
        if "error" in data:
            raise RuntimeError(f"駅すぱあと MCP がエラーを返しました: {data['error']}")
        return data

    async def _notify(self, method: str) -> None:
        res = await self._client.post(
            self._url, json={"jsonrpc": "2.0", "method": method}, headers=self._headers
        )
        res.raise_for_status()


def _decode(res: httpx.Response) -> dict:
    """JSON か SSE のどちらで返ってきても、JSON-RPC の本体を取り出す。"""

    if "text/event-stream" not in res.headers.get("content-type", ""):
        return res.json()
    for line in res.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise RuntimeError("駅すぱあと MCP の応答を読み取れませんでした")


def _text_of(result: dict) -> str:
    for part in result.get("content", []):
        if part.get("type") == "text":
            return part["text"]
    raise RuntimeError("駅すぱあと MCP の応答に本文がありません")


def _first(value):
    """駅すぱあと API は要素が 1 つだと配列にせず素で返す。ここで均す。"""
    return value[0] if isinstance(value, list) else value


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _leg_from_course(from_station: str, to_station: str, body: dict) -> TransitLeg:
    """`/search/course/extreme` の応答を TransitLeg に写す。"""

    # 経路が無いときは ResultSet だけが返る（エラーにはならない）。
    if "Course" not in body.get("ResultSet", {}):
        raise LookupError(f"{from_station} から {to_station} への経路が見つかりませんでした")
    course = _first(body["ResultSet"]["Course"])
    route = course["Route"]
    # 所要時間は「乗車 + それ以外（待ち・徒歩・乗換）」。timeWalk は timeOther の内数。
    duration = int(route.get("timeOnBoard", 0)) + int(route.get("timeOther", 0))
    lines = [
        line["Name"]
        for line in _as_list(route.get("Line"))
        if line.get("Type") != "walk" and line.get("Name")
    ]
    return TransitLeg(
        from_station=from_station,
        to_station=to_station,
        duration_minutes=duration,
        fare_yen=_oneway_fare(course),
        transfers=int(route.get("transferCount", 0)),
        lines=lines,
    )


def _oneway_fare(course: dict) -> int:
    """片道の合計運賃。区間ごとの Fare ではなく FareSummary を採る。"""

    prices = _as_list(course.get("Price"))
    for kind in ("FareSummary", "Fare"):
        for price in prices:
            if price.get("kind") == kind:
                return int(price.get("Oneway", 0))
    return 0



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
    if settings.ekispert_mode == "live" and settings.ekispert_api_key:
        return EkispertMcpClient(settings.ekispert_api_key)
    return MockTransitClient()
