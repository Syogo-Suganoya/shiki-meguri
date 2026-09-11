"""Orchestrator（設計書 §4）。

会話の受付、シーン判定（慶／弔）、子エージェントへの委譲を担う。
ADK の SequentialAgent 相当の役割を、外部依存なしで実装している
（`app.agents.adk` で ADK のツールとして公開する）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.agents import conversation
from app.agents.arrange import ArrangeAgent
from app.agents.deps import Deps
from app.agents.monitor import EXTENSION_MINUTES, ReturnMonitorAgent
from app.agents.route import RouteAgent
from app.agents.outfit import OutfitAgent
from app.domain.models import (
    ChatMessage,
    ConsentRequest,
    ConsentStatus,
    Event,
    EventStatus,
    EventType,
    ReturnAlert,
    Schedule,
    UserProfile,
)

AGENT = "orchestrator"

# チャットのみで始めた利用者の既定値。「最寄り駅は◯◯」で変更できる。
DEFAULT_HOME_STATION = "東京"

CLOSED_STATUSES = {EventStatus.COMPLETED, EventStatus.CANCELLED}

# シーン判定のキーワード。判定根拠は監査ログに残す。
SCENE_KEYWORDS: dict[EventType, tuple[str, ...]] = {
    EventType.FUNERAL: ("葬儀", "通夜", "告別式", "喪服", "訃報", "斎場"),
    EventType.SEIJIN: ("成人式", "卒業式", "振袖", "袴"),
    EventType.TOURISM: ("観光", "散策", "着物で", "レンタル着物", "小紋"),
    EventType.WEDDING: ("結婚式", "披露宴", "お呼ばれ", "二次会", "ドレス"),
}


class Orchestrator:
    def __init__(self, deps: Deps) -> None:
        self._d = deps
        self.deps = deps
        self.outfit = OutfitAgent(deps)
        self.arrange = ArrangeAgent(deps)
        self.route = RouteAgent(deps)
        self.monitor = ReturnMonitorAgent(deps)

    # ------------------------------------------------------------ 受付

    def detect_scene(self, text: str) -> tuple[EventType, str]:
        """本文からシーンを判定する。弔事を最優先で拾う。"""

        for event_type, keywords in SCENE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    return event_type, f"キーワード「{keyword}」を検出"
        return EventType.WEDDING, "該当キーワードなし。既定の結婚式として扱う"

    async def handle_message(self, uid: str, text: str) -> tuple[list[ChatMessage], Event | None]:
        """チャット 1 往復。返すのは「この往復で増えた発言」と対象イベント。

        会話の途中経過は保存せず、発言履歴から毎回組み立て直す
        （`app.agents.conversation`）。
        """

        history = await self._d.chat.history(uid)
        mark = len(history)
        await self._d.chat.hear(uid, text)

        user = await self._d.repo.get_user(uid)
        slots = conversation.parse_history(
            [m.text for m in history if m.role == "user"] + [text],
            self._d.clock.now(),
            self.detect_scene,
        )

        if user is None:
            user = await self.register_user(
                uid, slots.home_station or DEFAULT_HOME_STATION
            )
            if slots.home_station is None:
                await self._say(
                    uid,
                    f"はじめまして。最寄り駅を{user.home_station}として進めます"
                    "（「最寄り駅は◯◯」で変更できます）。",
                )
        elif slots.home_station and slots.home_station != user.home_station:
            user = user.model_copy(update={"home_station": slots.home_station})
            await self._d.repo.save_user(user)
            await self._say(uid, f"最寄り駅を{user.home_station}に変更しました。")

        event = await self._active_event(uid)
        try:
            event = await self._advance(user, event, text, slots)
        except (LookupError, ValueError) as exc:
            await self._say(uid, f"うまく進められませんでした：{exc}", kind="error")

        messages = (await self._d.chat.history(uid))[mark:]
        return messages, event

    async def _advance(
        self, user: UserProfile, event: Event | None, text: str, slots: conversation.Slots
    ) -> Event | None:
        """いまのイベントの状態から、次にやることを決めて子エージェントへ渡す。"""

        uid = user.uid

        if event is None:
            if not slots.ready:
                await self._say(
                    uid, "、".join(slots.missing()) + "を教えてください。"
                    "（例：「明後日15時、品川の結婚式」）"
                )
                return None
            event = await self.create_event(
                uid=uid,
                event_type=slots.scene or EventType.WEDDING,
                ceremony_start_at=slots.start_at,
                ceremony_end_at=slots.start_at + timedelta(hours=3),
                venue_name=slots.venue_name or f"{slots.venue_station}の会場",
                venue_station=slots.venue_station,
                basis=slots.scene_basis or "既定",
            )
            await self._say(
                uid,
                f"{event.type.label}として、{event.schedule.ceremony_start_at:%-m月%-d日 %H:%M}／"
                f"{event.schedule.venue_name}で承りました。衣装の候補を出します。",
            )
            event = await self.propose_outfits(event.event_id)
            await self._say(uid, _candidate_list(event), kind="info", event_id=event.event_id)
            return event

        if event.status is EventStatus.OUTFIT_PROPOSED:
            index = conversation.parse_choice(text, len(event.candidates))
            if index is None:
                await self._say(
                    uid, "番号でお選びください。\n" + _candidate_list(event),
                    event_id=event.event_id,
                )
                return event
            event, _ = await self.propose_reservation(
                event.event_id, event.candidates[index].outfit_id
            )
            return event

        if event.status is EventStatus.AWAITING_CONSENT:
            consent = event.pending_consent()
            if consent is None:
                return event
            if conversation.contains(text, conversation.APPROVE_WORDS):
                # 確定とタイムラインの通知は手配・動線エージェントが自分で行う。
                return await self.decide_consent(event.event_id, consent.consent_id, True)
            if conversation.contains(text, conversation.REJECT_WORDS):
                event = await self.decide_consent(event.event_id, consent.consent_id, False)
                await self._say(
                    uid, "起案を取り下げました。別の衣装をお選びください。\n"
                    + _candidate_list(event),
                    event_id=event.event_id,
                )
                return event
            await self._say(
                uid,
                f"{consent.summary} 合計{consent.amount_yen:,}円です。"
                "「承認」または「却下」とお送りください（承認まで確定しません）。",
                kind="consent",
                event_id=event.event_id,
            )
            return event

        if conversation.contains(text, conversation.RETURN_WORDS):
            event, alert = await self.check_return(event.event_id)
            if alert is None:
                await self._say(
                    uid, "返却期限まで余裕があります。近づいたらこちらからお知らせします。",
                    kind="return",
                    event_id=event.event_id,
                )
            return event

        await self._say(uid, _status_summary(event), event_id=event.event_id)
        return event

    async def _active_event(self, uid: str) -> Event | None:
        events = [e for e in await self._d.repo.list_events(uid) if e.status not in CLOSED_STATUSES]
        return events[-1] if events else None

    async def _say(self, uid: str, text: str, *, kind: str = "info", event_id: str | None = None):
        return await self._d.chat.say(uid, text, kind=kind, event_id=event_id)

    async def register_user(
        self, uid: str, home_station: str, size: str = "M", **extra
    ) -> UserProfile:
        # 既存の利用者なら上書きせず差分だけ当てる。最寄り駅を変えただけで
        # 表示名やサイズを巻き添えに消してはいけない。
        current = await self._d.repo.get_user(uid)
        update = {"home_station": home_station, "size": size, **extra}
        user = (
            current.model_copy(update=update)
            if current
            else UserProfile(uid=uid, **update)
        )
        await self._d.repo.save_user(user)
        await self._d.audit.record(
            agent=AGENT,
            action="register_user",
            basis="最寄り駅・サイズのみ登録（住所・氏名は収集しない）",
            payload={"uid": uid, "collected": ["home_station", "size"]},
        )
        return user

    async def start_from_form(
        self,
        *,
        uid: str,
        home_station: str,
        size: str,
        event_type: EventType,
        ceremony_start_at: datetime,
        venue_name: str,
        venue_station: str,
    ) -> Event:
        """入力フォームからの受付。会話で始めたときと同じところまで一度に進める。

        フォームは値が構造化されているので、文章に組み立て直して読み解かせない
        （`conversation` の規則解釈を通さない）。会話の記録には残すので、
        以降のやり取りはチャットからそのまま続けられる。
        """

        user = await self.register_user(uid, home_station, size)
        await self._d.chat.hear(
            uid,
            f"{event_type.label}／{ceremony_start_at:%-m月%-d日 %H:%M}／{venue_name}"
            f"（{venue_station}）／出発は{user.home_station}",
        )
        event = await self.create_event(
            uid=uid,
            event_type=event_type,
            ceremony_start_at=ceremony_start_at,
            ceremony_end_at=ceremony_start_at + timedelta(hours=3),
            venue_name=venue_name,
            venue_station=venue_station,
            basis="本人がフォームで入力",
        )
        await self._say(
            uid,
            f"{event.type.label}として、{event.schedule.ceremony_start_at:%-m月%-d日 %H:%M}／"
            f"{event.schedule.venue_name}で承りました。衣装の候補を出します。",
            event_id=event.event_id,
        )
        event = await self.propose_outfits(event.event_id)
        await self._say(uid, _candidate_list(event), event_id=event.event_id)
        return event

    async def create_event(
        self,
        *,
        uid: str,
        event_type: EventType,
        ceremony_start_at: datetime,
        venue_name: str,
        venue_station: str,
        ceremony_end_at: datetime | None = None,
        basis: str = "本人入力",
    ) -> Event:
        now = self._d.clock.now()
        schedule = Schedule(
            ceremony_start_at=ceremony_start_at,
            ceremony_end_at=ceremony_end_at,
            venue_name=venue_name,
            venue_station=venue_station,
        )
        event = Event(
            event_id=uuid.uuid4().hex[:12],
            uid=uid,
            type=event_type,
            status=EventStatus.DRAFT,
            schedule=schedule,
            created_at=now,
            updated_at=now,
            ttl_at=schedule.effective_end_at
            + timedelta(days=self._d.settings.event_ttl_days),
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="create_event",
            basis=f"シーン判定={event_type.label}（{basis}）",
            event_id=event.event_id,
            payload={
                "collected": ["日時", "会場", "服装区分"],
                "not_collected": ["誰の式か", "続柄", "故人情報"],
                "ttl_at": event.ttl_at.isoformat() if event.ttl_at else None,
            },
        )
        return event

    # ------------------------------------------------------------ 委譲

    async def propose_outfits(self, event_id: str, limit: int = 3) -> Event:
        event, user = await self._load(event_id)
        return await self.outfit.propose(event, user, limit=limit)

    async def propose_reservation(
        self,
        event_id: str,
        outfit_id: str,
        pickup_id: str | None = None,
        replace: bool = False,
    ) -> tuple[Event, ConsentRequest]:
        event, user = await self._load(event_id)
        return await self.arrange.propose(
            event, user, outfit_id, pickup_id=pickup_id, replace=replace
        )

    async def decide_consent(
        self, event_id: str, consent_id: str, approved: bool, note: str | None = None
    ) -> Event:
        event, user = await self._load(event_id)
        consent = event.find_consent(consent_id)
        if consent is None:
            raise ValueError(f"同意リクエストが見つかりません: {consent_id}")

        if consent.action == "reserve":
            event = await self.arrange.decide(event, consent_id, approved, note)
            if approved:
                event = await self.route.plan(event, user)
            return event
        if consent.action == "extend":
            return await self._apply_extension(event, consent, approved, note)
        raise ValueError(f"未対応の同意アクションです: {consent.action}")

    async def plan_route(self, event_id: str) -> Event:
        event, user = await self._load(event_id)
        return await self.route.plan(event, user)

    async def check_return(self, event_id: str) -> tuple[Event, ReturnAlert | None]:
        event, user = await self._load(event_id)
        return await self.monitor.check(event, user)

    async def mark_returned(self, event_id: str) -> Event:
        event, _ = await self._load(event_id)
        return await self.monitor.mark_returned(event)

    # ------------------------------------------------------------ 定期実行

    async def sweep(self) -> dict:
        """Cloud Scheduler から叩く定期ジョブ（設計書 §4 返却監視）。

        1) 返却期限の監視 2) TTL 超過の削除。
        """

        now = self._d.clock.now()
        alerted: list[str] = []

        for event in await self._d.repo.list_events():
            if event.status in {EventStatus.COMPLETED, EventStatus.CANCELLED}:
                continue
            user = await self._d.repo.get_user(event.uid)
            if user is None:
                continue

            if event.return_plan is not None:
                _, alert = await self.monitor.check(event, user)
                if alert is not None:
                    alerted.append(event.event_id)

        purged = await self._d.repo.purge_expired(now)
        if purged:
            await self._d.audit.record(
                agent=AGENT,
                action="purge_expired_events",
                basis=f"TTL（式終了+{self._d.settings.event_ttl_days}日）超過",
                payload={"event_ids": purged},
            )
        return {
            "swept_at": now.isoformat(),
            "alerted": alerted,
            "purged": purged,
        }

    # ------------------------------------------------------------ 内部

    async def _apply_extension(
        self, event: Event, consent: ConsentRequest, approved: bool, note: str | None
    ) -> Event:
        now = self._d.clock.now()
        decided = consent.model_copy(
            update={
                "status": ConsentStatus.APPROVED if approved else ConsentStatus.REJECTED,
                "decided_at": now,
                "note": note,
            }
        )
        consents = [
            decided if c.consent_id == consent.consent_id else c for c in event.consents
        ]
        return_plan = event.return_plan
        if approved and return_plan is not None:
            minutes = EXTENSION_MINUTES
            return_plan = return_plan.model_copy(
                update={
                    "due_at": return_plan.due_at + timedelta(minutes=minutes),
                    "extension_proposed_minutes": minutes,
                    "extension_fee_yen": consent.amount_yen,
                }
            )
        event = event.model_copy(
            update={"consents": consents, "return_plan": return_plan, "updated_at": now}
        )
        await self._d.repo.save_event(event)
        await self._d.audit.record(
            agent=AGENT,
            action="extension_approved" if approved else "extension_rejected",
            basis=f"本人{'承認' if approved else '却下'}（{consent.amount_yen}円）",
            event_id=event.event_id,
            consent_ref=consent.consent_id,
        )
        return event

    async def _load(self, event_id: str) -> tuple[Event, UserProfile]:
        event = await self._d.repo.get_event(event_id)
        if event is None:
            raise LookupError(f"イベントが見つかりません: {event_id}")
        user = await self._d.repo.get_user(event.uid)
        if user is None:
            raise LookupError(f"利用者が見つかりません: {event.uid}")
        return event, user


def _candidate_list(event: Event) -> str:
    lines = [
        f"{i}. {c.name}（{c.color}／{c.rental_fee_yen:,}円）"
        for i, c in enumerate(event.candidates, start=1)
    ]
    return "衣装の候補です。番号でお選びください。\n" + "\n".join(lines)


def _status_summary(event: Event) -> str:
    parts = [f"いまの状態は「{event.status.value}」です。"]
    if event.route and event.route.departure_at:
        parts.append(f"出発は{event.route.departure_at:%H:%M}の予定です。")
    if event.return_plan:
        parts.append(f"返却期限は{event.return_plan.due_at:%-m月%-d日 %H:%M}です。")
    parts.append("「返却」などとお送りいただければ確認します。")
    return " ".join(parts)
