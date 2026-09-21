"""API Gateway（Cloud Run: api）。

Web PWA（自前チャット UI）の受け口。`AGENT_TRANSPORT=inproc` ならエージェントを同一
プロセスで動かし（ローカル・単体デプロイ）、`http` なら agent サービスへ
そのまま中継する（2 サービス構成）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
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
    return {
        "status": "ok",
        "service": "api",
        "transport": settings.agent_transport,
        # 実際に使われる接続先。live 指定でもキーが無ければ mock に落ちるので、そこまで見て返す。
        # デプロイ後の疎通確認で、本番が mock のまま出ていないかを検出するのに使う。
        "modes": {
            "gemini": _effective(settings.gemini_mode, settings.gemini_api_key),
            "ekispert": _effective(settings.ekispert_mode, settings.ekispert_api_key),
        },
    }


def _effective(mode: str, key: str) -> str:
    return "live" if mode == "live" and key else "mock"


# 画面はルートから配る。/api・/health などのルートは上で先に登録してあるので、
# ここに落ちてくるのはそれ以外のパスだけ。マウントは必ず最後に置く。
if WEB_ROOT.is_dir():
    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
