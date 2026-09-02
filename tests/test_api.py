"""API の疎通確認。デモ動線（シード → 起案 → 承認 → 監査）が通ることを見る。"""

from __future__ import annotations

import httpx
import pytest

from app.api.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client: httpx.AsyncClient):
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["service"] == "api"


async def test_デモ動線が最後まで通る(client: httpx.AsyncClient):
    seeded = (await client.post("/api/demo/seed", json={"scenario": "wedding"})).json()
    event_id = seeded["event_id"]
    assert seeded["status"] == "outfit_proposed"
    assert seeded["candidates"]

    pickups = (await client.get(f"/api/events/{event_id}/pickups")).json()
    assert len(pickups) >= 2

    proposed = (
        await client.post(
            f"/api/events/{event_id}/reservation",
            json={"outfit_id": seeded["candidates"][0]["outfit_id"]},
        )
    ).json()
    consent_id = proposed["consent"]["consent_id"]
    assert proposed["consent"]["status"] == "pending"
    assert proposed["event"]["outfit"]["reservation_id"] is None

    approved = (
        await client.post(
            f"/api/events/{event_id}/consents/{consent_id}", json={"approved": True}
        )
    ).json()
    assert approved["outfit"]["state"] == "reserved"
    assert approved["route"]["steps"]

    logs = (await client.get(f"/api/audit?event_id={event_id}")).json()
    actions = [log["action"] for log in logs]
    assert "propose_reservation" in actions
    assert "reservation_confirmed" in actions
    assert "build_timeline" in actions


async def test_利用者の操作なしに届く通知を後から取得できる(client: httpx.AsyncClient):
    """エージェント起点の発言が `after` で拾えること（画面のポーリング）。"""

    uid = "u-poll"
    for text in ["最寄りは吉祥寺です", "明日16時、品川の結婚式", "1番", "承認"]:
        res = (await client.post("/api/chat", json={"uid": uid, "text": text})).json()
    event = res["event"]

    mark = (await client.get(f"/api/chat/{uid}")).json()[-1]["created_at"]
    assert (await client.get(f"/api/chat/{uid}", params={"after": mark})).json() == []

    # ここから先は利用者を一切操作させない。
    line = event["route"]["legs"][0]["lines"][0]
    await client.post("/api/demo/disrupt", json={"line": line, "delay_minutes": 18})
    swept = (await client.post("/api/tasks/sweep")).json()
    # 同じプロセスの他イベントも巻き込まれるので、含まれることだけ見る。
    assert event["event_id"] in swept["recalculated"]

    fresh = (await client.get(f"/api/chat/{uid}", params={"after": mark})).json()
    assert [m["kind"] for m in fresh] == ["delay"]
    assert fresh[0]["role"] == "agent"


async def test_存在しないイベントは404(client: httpx.AsyncClient):
    res = await client.post("/api/events/unknown/route")
    assert res.status_code == 404
