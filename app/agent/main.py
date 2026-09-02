"""Orchestrator Agent サービス（Cloud Run: agent）。

エージェントの権威。Gemini・YouCam・駅すぱあと MCP への接続を担い、
Cloud Scheduler からの `/tasks/sweep` もここで受ける。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.agents.deps import build_deps
from app.agents.orchestrator import Orchestrator
from app.api.routes import build_router
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(message)s")

settings = get_settings()
orchestrator = Orchestrator(build_deps(settings))

app = FastAPI(title="シキめぐり agent", version="0.1.0")
app.include_router(build_router(orchestrator))


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": "agent",
        "modes": {
            "gemini": settings.gemini_mode,
            "youcam": settings.youcam_mode,
            "ekispert": settings.ekispert_mode,
            "rental": settings.rental_mode,
            "gmi": settings.gmi_mode,
            "db": settings.db_driver,
        },
    }
