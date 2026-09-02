"""自前チャット（設計書 §5 通知）。

外部メッセージング基盤には依存しない。エージェントからの通知も利用者の発言も
同じ `messages` コレクションに時系列で積み、UI はそれを読むだけにする。

外部サービスを挟まないため、慶弔という繊細な情報が第三者のサーバーを
経由しない（設計書 §7-2 のデータ最小化と同じ方針）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.domain.models import ChatMessage
from app.infra.clock import Clock
from app.infra.repository import Repository

Kind = str


class ChatChannel:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    async def say(
        self,
        uid: str,
        text: str,
        *,
        kind: Kind = "info",
        event_id: str | None = None,
        at: datetime | None = None,
    ) -> ChatMessage:
        """エージェントからの発言。"""
        return await self._append("agent", uid, text, kind, event_id, at)

    async def hear(
        self, uid: str, text: str, *, event_id: str | None = None
    ) -> ChatMessage:
        """利用者からの発言。"""
        return await self._append("user", uid, text, "info", event_id, None)

    async def history(self, uid: str) -> list[ChatMessage]:
        return await self._repo.list_messages(uid)

    async def _append(
        self,
        role: str,
        uid: str,
        text: str,
        kind: Kind,
        event_id: str | None,
        at: datetime | None,
    ) -> ChatMessage:
        message = ChatMessage(
            message_id=uuid.uuid4().hex[:12],
            uid=uid,
            role=role,  # type: ignore[arg-type]
            text=text,
            kind=kind,  # type: ignore[arg-type]
            event_id=event_id,
            created_at=at or self._clock.now(),
        )
        return await self._repo.append_message(message)
