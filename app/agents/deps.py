"""エージェント共通の依存コンテナ。

ADK のエージェントは「道具（tool）」として外部クライアントを受け取る。
差し替え可能な形でまとめておくことで、テストでは全て mock、
本番では Secret Manager 由来のキーで live に切り替わる。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.chat import ChatChannel
from app.adapters.ekispert import TransitClient, build_transit_client
from app.adapters.llm import LlmClient, build_llm_client
from app.adapters.rental import RentalClient, build_rental_client
from app.adapters.youcam import TryOnClient, build_tryon_client
from app.config import Settings, get_settings
from app.infra.audit import AuditTrail
from app.infra.clock import Clock
from app.infra.repository import Repository, build_repository


@dataclass
class Deps:
    settings: Settings
    clock: Clock
    repo: Repository
    audit: AuditTrail
    transit: TransitClient
    tryon: TryOnClient
    rental: RentalClient
    llm: LlmClient
    chat: ChatChannel


def build_deps(settings: Settings | None = None, clock: Clock | None = None) -> Deps:
    settings = settings or get_settings()
    clock = clock or Clock()
    repo = build_repository(settings)
    return Deps(
        settings=settings,
        clock=clock,
        repo=repo,
        audit=AuditTrail(repo, clock),
        transit=build_transit_client(settings),
        tryon=build_tryon_client(settings),
        rental=build_rental_client(settings),
        llm=build_llm_client(settings),
        chat=ChatChannel(repo, clock),
    )
