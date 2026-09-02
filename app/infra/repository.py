"""永続化（設計書 §6）。

memory ドライバはデモ・テスト用、firestore ドライバは本番用。
`events` は TTL フィールドを持ち、Firestore の TTL ポリシーで自動削除される。
"""

from __future__ import annotations

import abc
from datetime import datetime

from app.config import Settings
from app.domain.models import AuditLog, ChatMessage, Event, UserProfile


class Repository(abc.ABC):
    @abc.abstractmethod
    async def append_message(self, message: ChatMessage) -> ChatMessage: ...

    @abc.abstractmethod
    async def list_messages(self, uid: str) -> list[ChatMessage]: ...

    @abc.abstractmethod
    async def save_user(self, user: UserProfile) -> UserProfile: ...

    @abc.abstractmethod
    async def get_user(self, uid: str) -> UserProfile | None: ...

    @abc.abstractmethod
    async def save_event(self, event: Event) -> Event: ...

    @abc.abstractmethod
    async def get_event(self, event_id: str) -> Event | None: ...

    @abc.abstractmethod
    async def list_events(self, uid: str | None = None) -> list[Event]: ...

    @abc.abstractmethod
    async def append_audit(self, log: AuditLog) -> AuditLog: ...

    @abc.abstractmethod
    async def list_audit(self, event_id: str | None = None) -> list[AuditLog]: ...

    @abc.abstractmethod
    async def purge_expired(self, now: datetime) -> list[str]:
        """TTL 超過の events を削除し、削除した ID を返す。"""


class MemoryRepository(Repository):
    def __init__(self) -> None:
        self._users: dict[str, UserProfile] = {}
        self._events: dict[str, Event] = {}
        self._audit: list[AuditLog] = []
        self._messages: list[ChatMessage] = []

    async def append_message(self, message: ChatMessage) -> ChatMessage:
        self._messages.append(message)
        return message

    async def list_messages(self, uid: str) -> list[ChatMessage]:
        return [m for m in self._messages if m.uid == uid]

    async def save_user(self, user: UserProfile) -> UserProfile:
        self._users[user.uid] = user
        return user

    async def get_user(self, uid: str) -> UserProfile | None:
        return self._users.get(uid)

    async def save_event(self, event: Event) -> Event:
        self._events[event.event_id] = event
        return event

    async def get_event(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    async def list_events(self, uid: str | None = None) -> list[Event]:
        events = list(self._events.values())
        if uid is not None:
            events = [e for e in events if e.uid == uid]
        return sorted(events, key=lambda e: e.created_at)

    async def append_audit(self, log: AuditLog) -> AuditLog:
        # 追記専用。更新・削除の口は用意しない。
        self._audit.append(log)
        return log

    async def list_audit(self, event_id: str | None = None) -> list[AuditLog]:
        logs = self._audit
        if event_id is not None:
            logs = [log for log in logs if log.event_id == event_id]
        return sorted(logs, key=lambda log: log.ts)

    async def purge_expired(self, now: datetime) -> list[str]:
        expired = [
            eid
            for eid, ev in self._events.items()
            if ev.ttl_at is not None and ev.ttl_at <= now
        ]
        for eid in expired:
            del self._events[eid]
        return expired


class FirestoreRepository(Repository):
    """開発（エミュレータ）・本番の既定。

    `FIRESTORE_EMULATOR_HOST` が設定されていればクライアントが自動でそちらを向く。
    """

    def __init__(self, project: str) -> None:
        from google.cloud import firestore  # noqa: PLC0415

        self._db = firestore.AsyncClient(project=project)

    @staticmethod
    def _eq(field: str, value):
        from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: PLC0415

        return FieldFilter(field, "==", value)

    async def append_message(self, message: ChatMessage) -> ChatMessage:
        await self._db.collection("messages").document(message.message_id).set(
            message.model_dump(mode="json")
        )
        return message

    async def list_messages(self, uid: str) -> list[ChatMessage]:
        query = self._db.collection("messages").where(filter=self._eq("uid", uid))
        messages = [ChatMessage(**doc.to_dict()) async for doc in query.stream()]
        return sorted(messages, key=lambda m: m.created_at)

    async def save_user(self, user: UserProfile) -> UserProfile:
        await self._db.collection("users").document(user.uid).set(
            user.model_dump(mode="json")
        )
        return user

    async def get_user(self, uid: str) -> UserProfile | None:
        snap = await self._db.collection("users").document(uid).get()
        return UserProfile(**snap.to_dict()) if snap.exists else None

    async def save_event(self, event: Event) -> Event:
        data = event.model_dump(mode="json")
        # Firestore の TTL ポリシーは Timestamp 型のフィールドしか見ない。
        # ISO 文字列のままだと自動削除が静かに効かなくなるので、ここだけ戻す。
        data["ttl_at"] = event.ttl_at
        await self._db.collection("events").document(event.event_id).set(data)
        return event

    async def get_event(self, event_id: str) -> Event | None:
        snap = await self._db.collection("events").document(event_id).get()
        return Event(**snap.to_dict()) if snap.exists else None

    async def list_events(self, uid: str | None = None) -> list[Event]:
        query = self._db.collection("events")
        if uid is not None:
            query = query.where(filter=self._eq("uid", uid))
        events = [Event(**doc.to_dict()) async for doc in query.stream()]
        return sorted(events, key=lambda e: e.created_at)

    async def append_audit(self, log: AuditLog) -> AuditLog:
        await self._db.collection("audit").document(log.log_id).set(
            log.model_dump(mode="json")
        )
        return log

    async def list_audit(self, event_id: str | None = None) -> list[AuditLog]:
        query = self._db.collection("audit")
        if event_id is not None:
            query = query.where(filter=self._eq("event_id", event_id))
        logs = [AuditLog(**doc.to_dict()) async for doc in query.stream()]
        return sorted(logs, key=lambda log: log.ts)

    async def purge_expired(self, now: datetime) -> list[str]:
        """TTL 超過の events を削除する。

        本番では Firestore の TTL ポリシーも同じものを消すが、削除は冪等なので
        重ねて構わない。エミュレータには TTL ポリシーが無いため、こちらが要る。
        """
        from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: PLC0415

        query = self._db.collection("events").where(
            filter=FieldFilter("ttl_at", "<=", now)
        )
        expired = [doc.id async for doc in query.stream()]
        for event_id in expired:
            await self._db.collection("events").document(event_id).delete()
        return expired


def build_repository(settings: Settings) -> Repository:
    if settings.db_driver == "firestore":
        return FirestoreRepository(settings.google_cloud_project)
    # memory はテスト専用。プロセスが落ちると消える。
    return MemoryRepository()
