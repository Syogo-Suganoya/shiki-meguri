# API Gateway（Cloud Run: api）
# ローカル・CI・本番が同一 Dockerfile を参照する。
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 依存レイヤを分離してビルドキャッシュを効かせる。
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[dev]"

COPY app ./app
COPY web ./web
COPY tests ./tests

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT}"]
