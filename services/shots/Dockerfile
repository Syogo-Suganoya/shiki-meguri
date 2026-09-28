# 紹介ページに載せる画面の写しと、デモ動画を撮るためだけのコンテナ。
# 実行中の api にブラウザで繋ぎ、利用者と同じ順に操作して撮る。
FROM zenika/alpine-chrome:with-puppeteer

USER root
# 日本語のフォントが無いと全部豆腐になる。
# ffmpeg は動画の録画（page.screencast）と mp4 への変換に使う。
RUN apk add --no-cache font-noto-cjk ffmpeg
