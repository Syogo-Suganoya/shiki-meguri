# Orchestrator Agent（Cloud Run: agent）
# api と同じソースツリー・同じ依存を使い、起動する ASGI アプリだけが違う。
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[dev]"

COPY app ./app
COPY tests ./tests

ENV PORT=8081
EXPOSE 8081

CMD ["sh", "-c", "uvicorn app.agent.main:app --host 0.0.0.0 --port ${PORT}"]
