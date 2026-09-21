"""駅すぱあと API MCPサーバー アダプタの検証。

実サーバーは叩かず、MCPサーバーが実際に返した形をそのまま写した応答を
`httpx.MockTransport` で返して、送る側・読む側の両方を固定する。
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.adapters.ekispert import EKISPERT_CODES, STATION_COORDS, EkispertMcpClient
from app.config import Settings
from app.infra.clock import JST

URL = "https://api-mcp.ekispert.jp/mcp"

# 東京 → 品川。MCPサーバーの応答から、読み取りに関わる部分だけを残したもの。
COURSE = {
    "ResultSet": {
        "apiVersion": "1.27.0.0",
        "Course": {
            "searchType": "plain",
            "Price": [
                {"kind": "Fare", "Oneway": "210", "Round": "420"},
                {"kind": "FareSummary", "Oneway": "210", "Round": "420"},
                {"kind": "Teiki1Summary", "Oneway": "6240"},
            ],
            "Route": {
                "timeOther": "4",
                "timeOnBoard": "12",
                "timeWalk": "0",
                "distance": "68",
                "transferCount": "0",
                # 要素が 1 つのときは配列にならない。
                "Line": {"Name": "ＪＲ山手線外回り", "Type": "train", "timeOnBoard": "12"},
                "Point": [
                    {"Station": {"code": "22828", "Name": "東京"}},
                    {"Station": {"code": "22709", "Name": "品川"}},
                ],
            },
        },
    }
}


def _server(sent: list[dict], *, sse: bool = False) -> httpx.MockTransport:
    """initialize → notifications/initialized → tools/call を受ける最小の MCPサーバー。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append({"headers": dict(request.headers), "body": body})
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(COURSE)}]}
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"event: message\ndata: {json.dumps(payload)}\n\n",
            )
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _client(sent: list[dict], *, sse: bool = False) -> EkispertMcpClient:
    client = EkispertMcpClient("test-key", URL)
    client._client = httpx.AsyncClient(transport=_server(sent, sse=sse))
    return client


@pytest.mark.asyncio
async def test_経路探索の結果を移動区間に写す():
    sent: list[dict] = []
    leg = await _client(sent).search("東京", "品川")

    # 所要時間は「乗車 + それ以外」。timeWalk を足して二重に数えない。
    assert leg.duration_minutes == 16
    assert leg.fare_yen == 210  # 区間ごとの Fare ではなく合計の FareSummary
    assert leg.transfers == 0
    assert leg.lines == ["ＪＲ山手線外回り"]


@pytest.mark.asyncio
async def test_認証は専用ヘッダで送り_出発と目的地はコロンで並べる():
    sent: list[dict] = []
    await _client(sent).search("東京", "品川")

    assert [s["body"]["method"] for s in sent] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    call = sent[-1]
    # Authorization: Bearer ではない。
    assert call["headers"]["ekispert-api-access-key"] == "test-key"
    assert "authorization" not in call["headers"]
    assert call["body"]["params"]["name"] == "ekispert_api_search_routes"
    # 同名の駅と取り違えないよう、駅名ではなく駅コードで渡す。
    assert call["body"]["params"]["arguments"]["viaList"] == "22828:22709"


@pytest.mark.asyncio
async def test_式の日付を渡し_時刻は渡さない():
    """ダイヤ探索は専用キーが要るので、いまは日付だけ渡す。"""

    sent: list[dict] = []
    await _client(sent).search("東京", "品川", arrive_by=datetime(2026, 10, 10, 13, 0, tzinfo=JST))

    args = sent[-1]["body"]["params"]["arguments"]
    assert args["date"] == 20261010
    assert "time" not in args
    assert "searchType" not in args


@pytest.mark.asyncio
async def test_接続は一度だけ初期化して使い回す():
    sent: list[dict] = []
    client = _client(sent)
    await client.search("東京", "品川")
    await client.search("品川", "渋谷")

    assert [s["body"]["method"] for s in sent].count("initialize") == 1


@pytest.mark.asyncio
async def test_SSEで返ってきても読める():
    sent: list[dict] = []
    leg = await _client(sent, sse=True).search("東京", "品川")
    assert leg.duration_minutes == 16


@pytest.mark.asyncio
async def test_エラー応答は例外にする():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "viaList は必須です"}],
                },
            },
        )

    client = EkispertMcpClient("test-key", URL)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="viaList"):
        await client.search("東京", "品川")


@pytest.mark.asyncio
async def test_同じ駅なら探索せずに空の区間を返す():
    """受取場所が会場の最寄りと同じとき。呼べばアクセス数が加算される。"""

    sent: list[dict] = []
    leg = await _client(sent).search("品川", "品川")

    assert sent == []
    assert (leg.duration_minutes, leg.fare_yen, leg.lines) == (0, 0, [])


@pytest.mark.asyncio
async def test_経路が無いときは読める例外にする():
    """経路なしはエラー応答ではなく、Course の無い ResultSet で返ってくる。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {}
            if body["method"] == "initialize"
            else {
                "content": [
                    {"type": "text", "text": json.dumps({"ResultSet": {"apiVersion": "1.27.0.0"}})}
                ]
            }
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    client = EkispertMcpClient("test-key", URL)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LookupError, match="経路が見つかりません"):
        await client.search("東京", "嵐山")


@pytest.mark.asyncio
async def test_特急料金も運賃に足す():
    """新幹線の経路は運賃（620円）だけでなく特急料金（1,090円）もかかる。実データの値。"""

    shinkansen = json.loads(json.dumps(COURSE))
    shinkansen["ResultSet"]["Course"]["Price"] = [
        {"kind": "ChargeSummary", "Oneway": "1090"},
        {"kind": "Charge", "Oneway": "2810", "Name": "指定席"},
        {"kind": "Charge", "Oneway": "1090", "Name": "自由席"},
        {"kind": "Fare", "Oneway": "620"},
        {"kind": "FareSummary", "Oneway": "620"},
        {"kind": "Teiki1Summary", "Oneway": "57370"},
    ]
    client, _ = _answer(json.dumps(shinkansen))
    leg = await client.search("東京", "大宮")
    assert leg.fare_yen == 1710


def test_選べる駅はすべて駅コードを持つ():
    """一覧に駅を足したら、コードも足さないと名前で探索されて取り違える。"""

    assert set(STATION_COORDS) <= set(EKISPERT_CODES)


@pytest.mark.asyncio
async def test_同名の駅がある駅も一つに決まる():
    sent: list[dict] = []
    await _client(sent).search("大宮", "嵐山")

    via = sent[-1]["body"]["params"]["arguments"]["viaList"]
    assert via == "21987:25591"  # 大宮(埼玉県) と 嵐山(京福線)


@pytest.mark.asyncio
async def test_一覧にない駅は名前のまま渡す():
    sent: list[dict] = []
    await _client(sent).search("東京", "高円寺")
    assert sent[-1]["body"]["params"]["arguments"]["viaList"] == "22828:高円寺"


def _answer(tool_text: str, *, session: str | None = None):
    """initialize には応じ、tools/call には決めた本文を返すサーバー。"""

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append({"headers": dict(request.headers), "body": body})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        headers = {"mcp-session-id": session} if session and body["method"] == "initialize" else {}
        result = {} if body["method"] == "initialize" else {"content": [{"type": "text", "text": tool_text}]}
        return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    client = EkispertMcpClient(" test-key\n", URL)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, sent


@pytest.mark.asyncio
async def test_駅すぱあとのエラーは本文の内容で伝える():
    """API 側のエラーは isError ではなく {status, message} の本文で返る。"""

    client, _ = _answer(json.dumps({"status": 400, "message": "駅が見つかりません。(E101)"}))
    with pytest.raises(RuntimeError, match="駅が見つかりません"):
        await client.search("東京", "品川")


@pytest.mark.asyncio
async def test_キーの前後の空白と改行は落として送る():
    client, sent = _answer(json.dumps(COURSE))
    await client.search("東京", "品川")
    assert sent[-1]["headers"]["ekispert-api-access-key"] == "test-key"


@pytest.mark.asyncio
async def test_セッションが発行されたら以後の要求に付ける():
    client, sent = _answer(json.dumps(COURSE), session="sess-123")
    await client.search("東京", "品川")

    assert "mcp-session-id" not in sent[0]["headers"]  # initialize の時点ではまだ無い
    assert sent[-1]["headers"]["mcp-session-id"] == "sess-123"


def test_キーが無いうちは実接続しない():
    from app.adapters.ekispert import MockTransitClient, build_transit_client

    assert isinstance(build_transit_client(Settings(ekispert_mode="live")), MockTransitClient)
    live = build_transit_client(Settings(ekispert_mode="live", ekispert_api_key="k"))
    assert isinstance(live, EkispertMcpClient)
