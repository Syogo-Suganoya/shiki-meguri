"""API の疎通確認。デモ動線（シード → 起案 → 承認 → 監査）が通ることを見る。"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest

from app.api.main import app
from app.infra.clock import JST


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_疎通確認の口が生きている(client: httpx.AsyncClient):
    # Cloud Run では /healthz が Google Front End に横取りされるので使わない。
    res = await client.get("/health")
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
    """エージェント起点の発言が `after` で拾えること（画面のポーリング）。

    式が済んだ手配を作り、定期実行が返却期限に気づいて声をかけるところまで見る。
    """

    uid = "u-poll"
    # 返却期限が迫った状態を作るため、式そのものは過去に置く。
    started = (datetime.now(JST) - timedelta(days=2)).isoformat()
    event = (
        await client.post(
            "/api/events/start",
            json={
                "uid": uid,
                "type": "wedding",
                "ceremony_start_at": started,
                "venue_station": "品川",
                "home_station": "吉祥寺",
            },
        )
    ).json()
    proposed = (
        await client.post(
            f"/api/events/{event['event_id']}/reservation",
            json={"outfit_id": event["candidates"][0]["outfit_id"]},
        )
    ).json()
    await client.post(
        f"/api/events/{event['event_id']}/consents/{proposed['consent']['consent_id']}",
        json={"approved": True},
    )

    mark = (await client.get(f"/api/chat/{uid}")).json()[-1]["created_at"]
    assert (await client.get(f"/api/chat/{uid}", params={"after": mark})).json() == []

    # ここから先は利用者を一切操作させない。
    swept = (await client.post("/api/tasks/sweep")).json()
    assert event["event_id"] in swept["alerted"]

    fresh = (await client.get(f"/api/chat/{uid}", params={"after": mark})).json()
    assert [m["kind"] for m in fresh] == ["return"]
    assert fresh[0]["role"] == "agent"


async def test_存在しないイベントは404(client: httpx.AsyncClient):
    res = await client.post("/api/events/unknown/route")
    assert res.status_code == 404


async def test_フォームからの受付は衣装候補まで進む(client: httpx.AsyncClient):
    """入力フォームは会話の解釈を通さず、構造化された値をそのまま登録する。"""

    body = {
        "uid": "u-form",
        "type": "funeral",
        # 画面は UTC の ISO 文字列で送る。文面は日本時間で読ませる。
        "ceremony_start_at": "2026-10-11T04:00:00Z",
        "venue_station": "上野",
        "home_station": "吉祥寺",
        "size": "L",
    }
    event = (await client.post("/api/events/start", json=body)).json()
    assert event["type"] == "funeral"
    assert event["status"] == "outfit_proposed"
    assert event["schedule"]["venue_station"] == "上野"
    # 会場名は空でも受け取れる（駅から補う）。
    assert event["schedule"]["venue_name"] == "上野の会場"

    user = (await client.get("/api/users/u-form")).json()
    assert (user["home_station"], user["size"]) == ("吉祥寺", "L")

    # 以降はチャットからそのまま続けられるよう、受付の記録が会話にも残る。
    messages = (await client.get("/api/chat/u-form")).json()
    assert messages[0]["role"] == "user"
    assert "上野" in messages[0]["text"]
    assert "13:00" in messages[0]["text"]  # 06:00 と書かれたら時差を落としている
    assert any("衣装の候補" in m["text"] for m in messages)


async def test_知らない駅はフォームで弾く(client: httpx.AsyncClient):
    res = await client.post(
        "/api/events/start",
        json={
            "uid": "u-form-ng",
            "type": "wedding",
            "ceremony_start_at": "2026-10-11T13:00:00+09:00",
            "venue_station": "架空駅",
            "home_station": "東京",
        },
    )
    assert res.status_code == 400
    assert "架空駅" in res.json()["detail"]
