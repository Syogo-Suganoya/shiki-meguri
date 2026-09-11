from __future__ import annotations

from datetime import datetime

import pytest

from app.adapters.ekispert import MockTransitClient
from app.adapters.llm import StubLlmClient
from app.adapters.chat import ChatChannel
from app.adapters.rental import MockRentalClient
from app.agents.deps import Deps
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.infra.audit import AuditTrail
from app.infra.clock import JST, FrozenClock
from app.infra.repository import MemoryRepository

# 式当日の朝 8:00 に時刻を固定する。
NOW = datetime(2026, 10, 10, 8, 0, tzinfo=JST)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def deps(clock: FrozenClock) -> Deps:
    settings = Settings(db_driver="memory")
    repo = MemoryRepository()
    return Deps(
        settings=settings,
        clock=clock,
        repo=repo,
        audit=AuditTrail(repo, clock),
        transit=MockTransitClient(),
        rental=MockRentalClient(),
        llm=StubLlmClient(),
        chat=ChatChannel(repo, clock),
    )


@pytest.fixture
def orchestrator(deps: Deps) -> Orchestrator:
    return Orchestrator(deps)
