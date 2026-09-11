"""API Gateway（Cloud Run: api）。

Web PWA（自前チャット UI）の受け口。`AGENT_TRANSPORT=inproc` ならエージェントを同一
プロセスで動かし（ローカル・単体デプロイ）、`http` なら agent サービスへ
そのまま中継する（設計書 §4 の 2 サービス構成）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.agents.deps import build_deps
from app.agents.orchestrator import Orchestrator
from app.api.routes import build_router
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(message)s")

settings = get_settings()
app = FastAPI(title="シキめぐり api", version="0.1.0")

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"

if settings.agent_transport == "inproc":
    orchestrator = Orchestrator(build_deps(settings))
    app.include_router(build_router(orchestrator), prefix="/api")
else:
    PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

    @app.api_route("/api/{path:path}", methods=PROXY_METHODS)
    async def proxy(path: str, request: Request) -> Response:
        url = f"{settings.agent_base_url.rstrip('/')}/{path}"
        async with httpx.AsyncClient(timeout=60) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=await request.body(),
                headers={
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in {"host", "content-length"}
                },
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )


# Cloud Run では `/healthz` が Google Front End に横取りされ、コンテナまで届かない
# （GFE が自前の 404 を返す）。疎通確認に使う口なので、取られない名前にする。
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "api", "transport": settings.agent_transport}


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


if WEB_ROOT.is_dir():
    app.mount("/ui", StaticFiles(directory=WEB_ROOT, html=True), name="ui")
