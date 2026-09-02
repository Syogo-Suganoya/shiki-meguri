# アーキテクチャ図の生成環境（mingrammer/diagrams + Graphviz）。
# 日本語ラベルを描くため CJK フォントを入れる。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip && pip install "diagrams>=0.23"

WORKDIR /srv
CMD ["python", "docs/architecture.py"]
