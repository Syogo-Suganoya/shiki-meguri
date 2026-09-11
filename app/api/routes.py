"""エージェント操作の HTTP 面。

api / agent どちらのサービスからも同じ router を載せられるようにしておく
（agent サービスが権威、api は薄いゲートウェイ）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from app.adapters.ekispert import STATION_COORDS
from app.agents.orchestrator import Orchestrator
from app.api.schemas import (
    ChatRequest,
    ConsentDecisionRequest,
    CreateEventRequest,
    ProposeOutfitsRequest,
    ProposeReservationRequest,
    RegisterUserRequest,
    SeedRequest,
    StartIntakeRequest,
)
from app.domain.models import EventType

# デモシナリオ（会場は「駅」だけ分かれば全機能が成立する）。
SEED_VENUES: dict[EventType, tuple[str, str]] = {
    EventType.WEDDING: ("ベイサイド迎賓館", "品川"),
    EventType.SEIJIN: ("市民ホール", "大宮"),
    EventType.TOURISM: ("祇園散策", "祇園四条"),
    EventType.FUNERAL: ("市営斎場", "上野"),
}


def build_router(orchestrator: Orchestrator) -> APIRouter:
    router = APIRouter()
    deps = orchestrator.deps

    # -------------------------------------------------------------- 利用者

    @router.post("/users")
    async def register_user(req: RegisterUserRequest):
        user = await orchestrator.register_user(
            req.uid,
            req.home_station,
            req.size,
            display_name=req.display_name,
        )
        return user

    @router.get("/users/{uid}")
    async def get_user(uid: str):
        user = await deps.repo.get_user(uid)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        return user

    @router.get("/stations")
    async def list_stations() -> list[str]:
        """入力フォームの駅の選択肢。会場も出発地もここから選ぶ。"""

        return list(STATION_COORDS)

    # -------------------------------------------------------------- イベント

    @router.post("/events/start")
    async def start_intake(req: StartIntakeRequest):
        """入力フォームからの受付。利用者登録から衣装候補までを一度に進める。"""

        for station in (req.venue_station, req.home_station):
            if station not in STATION_COORDS:
                raise HTTPException(400, f"対応していない駅です: {station}")
        return await _guard(
            orchestrator.start_from_form(
                uid=req.uid,
                home_station=req.home_station,
                size=req.size,
                event_type=req.type,
                ceremony_start_at=req.ceremony_start_at,
                venue_name=req.venue_name or f"{req.venue_station}の会場",
                venue_station=req.venue_station,
            )
        )

    @router.post("/events")
    async def create_event(req: CreateEventRequest):
        if req.type is not None:
            event_type, basis = req.type, "本人が明示"
        elif req.message:
            event_type, basis = orchestrator.detect_scene(req.message)
        else:
            raise HTTPException(400, "type か message のどちらかが必要です")

        return await orchestrator.create_event(
            uid=req.uid,
            event_type=event_type,
            ceremony_start_at=req.ceremony_start_at,
            ceremony_end_at=req.ceremony_end_at,
            venue_name=req.venue_name,
            venue_station=req.venue_station,
            basis=basis,
        )

    @router.get("/events")
    async def list_events(uid: str | None = None):
        return await deps.repo.list_events(uid)

    @router.get("/events/{event_id}")
    async def get_event(event_id: str):
        event = await deps.repo.get_event(event_id)
        if event is None:
            raise HTTPException(404, "イベントが見つかりません")
        return event

    # -------------------------------------------------------------- 衣装

    @router.post("/events/{event_id}/outfits")
    async def propose_outfits(event_id: str, req: ProposeOutfitsRequest):
        return await _guard(orchestrator.propose_outfits(event_id, req.limit))

    # -------------------------------------------------------------- 手配

    @router.post("/events/{event_id}/reservation")
    async def propose_reservation(event_id: str, req: ProposeReservationRequest):
        event, consent = await _guard(
            orchestrator.propose_reservation(
                event_id, req.outfit_id, pickup_id=req.pickup_id, replace=req.replace
            )
        )
        return {"event": event, "consent": consent}

    @router.get("/events/{event_id}/pickups")
    async def compare_pickups(event_id: str):
        event = await deps.repo.get_event(event_id)
        if event is None:
            raise HTTPException(404, "イベントが見つかりません")
        user = await deps.repo.get_user(event.uid)
        if user is None:
            raise HTTPException(404, "利用者が見つかりません")
        evaluations = await orchestrator.arrange.evaluate_pickups(event, user)
        return [
            {
                "option": e.option,
                "travel_minutes": e.travel_minutes,
                "fee_yen": e.fee_yen,
                "fare_yen": e.fare_yen,
                "score": e.score,
                "feasible": e.plan.feasible,
                "warning": e.plan.warning,
                "departure_at": e.plan.departure_at,
            }
            for e in evaluations
        ]

    # -------------------------------------------------------------- 同意ゲート

    @router.get("/events/{event_id}/consents")
    async def list_consents(event_id: str):
        event = await deps.repo.get_event(event_id)
        if event is None:
            raise HTTPException(404, "イベントが見つかりません")
        return event.consents

    @router.post("/events/{event_id}/consents/{consent_id}")
    async def decide_consent(
        event_id: str, consent_id: str, req: ConsentDecisionRequest
    ):
        return await _guard(
            orchestrator.decide_consent(event_id, consent_id, req.approved, req.note)
        )

    # -------------------------------------------------------------- 動線

    @router.post("/events/{event_id}/route")
    async def plan_route(event_id: str):
        return await _guard(orchestrator.plan_route(event_id))


    # -------------------------------------------------------------- 返却

    @router.post("/events/{event_id}/return/check")
    async def check_return(event_id: str):
        event, alert = await _guard(orchestrator.check_return(event_id))
        return {"event": event, "alert": alert}

    @router.post("/events/{event_id}/return/complete")
    async def complete_return(event_id: str):
        return await _guard(orchestrator.mark_returned(event_id))

    # -------------------------------------------------------------- 監査

    @router.get("/audit")
    async def list_audit(event_id: str | None = None):
        return await deps.audit.list(event_id)

    # -------------------------------------------------------------- チャット

    @router.post("/chat")
    async def chat(req: ChatRequest):
        """自前チャットの 1 往復。エージェントからの通知も同じ口に流れる。"""
        messages, event = await _guard(orchestrator.handle_message(req.uid, req.text))
        return {"messages": messages, "event": event}

    @router.get("/chat/{uid}")
    async def chat_history(uid: str, after: datetime | None = None):
        """`after` を渡すと、それ以降の発言だけを返す（画面のポーリング用）。

        エージェント側から始まる通知（返却期限の注意など）は利用者の操作を
        待たないため、画面はここを定期的に読みに来る。
        """
        messages = await deps.chat.history(uid)
        if after is not None:
            messages = [m for m in messages if m.created_at > after]
        return messages

    # -------------------------------------------------------------- 定期実行

    @router.post("/tasks/sweep")
    async def sweep():
        """Cloud Scheduler → Cloud Run（agent）。返却監視・TTL 削除。"""
        return await orchestrator.sweep()

    # -------------------------------------------------------------- デモ操作


    @router.post("/demo/seed")
    async def seed(req: SeedRequest):
        """1 リクエストで「利用者登録 → 式の登録 → 候補提示」まで進める。"""
        venue_name, venue_station = SEED_VENUES[req.scenario]
        home_station = "吉祥寺" if req.scenario is not EventType.TOURISM else "京都"
        await orchestrator.register_user(
            req.uid,
            home_station,
            size="M",
            display_name="デモ利用者",
        )
        start = deps.clock.now() + timedelta(hours=req.hours_until_ceremony)
        start = start.replace(minute=0, second=0, microsecond=0)
        event = await orchestrator.create_event(
            uid=req.uid,
            event_type=req.scenario,
            ceremony_start_at=start,
            ceremony_end_at=start + timedelta(hours=3),
            venue_name=venue_name,
            venue_station=venue_station,
            basis="デモシード",
        )
        event = await orchestrator.propose_outfits(event.event_id)
        return event

    return router


async def _guard(awaitable):
    try:
        return await awaitable
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
