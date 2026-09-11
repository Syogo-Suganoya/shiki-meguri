"""監査ログ（設計書 §7-4）。

全エージェントの判断を `audit` コレクションに構造化記録する。
Cloud Logging へは同じ内容を JSON 1 行で吐き、デモの監査ビューと
本番のログ検索が同じスキーマで読めるようにする。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from app.domain.models import AuditLog
from app.infra.clock import Clock
from app.infra.repository import Repository

logger = logging.getLogger("shiki.audit")


class AuditTrail:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    async def record(
        self,
        *,
        agent: str,
        action: str,
        basis: str,
        event_id: str | None = None,
        consent_ref: str | None = None,
        payload: dict | None = None,
    ) -> AuditLog:
        log = AuditLog(
            log_id=uuid.uuid4().hex,
            ts=self._clock.now(),
            event_id=event_id,
            agent=agent,
            action=action,
            basis=basis,
            consent_ref=consent_ref,
            payload=payload or {},
        )
        await self._repo.append_audit(log)
        logger.info(json.dumps(log.model_dump(mode="json"), ensure_ascii=False))
        return log

    async def list(self, event_id: str | None = None) -> list[AuditLog]:
        return await self._repo.list_audit(event_id)
