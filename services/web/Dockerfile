# フロント（PWA）の開発用サーバー。
# 本番では api サービスが /ui として同じ静的ファイルを配信するため、
# このコンテナは開発時のみ使う（compose の profile: web）。
FROM python:3.12-slim

WORKDIR /srv/web
COPY web ./

EXPOSE 5173
CMD ["python", "-m", "http.server", "5173", "--bind", "0.0.0.0"]
