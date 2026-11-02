# kuma-agent をコンテナで動かすためのイメージ定義。
# データ（会話・記憶など）は /data（ボリューム）に置くので、
# イメージを作り直してもデータは残る。
# MCP サーバー（ブラウザ操作・Google カレンダー）も使えるよう、
# Node.js と Chromium も入れてある（その分イメージは大きい）。
FROM node:22-slim AS node
FROM python:3.12-slim

# Node.js 一式（node / npm / npx とグローバル導入の置き場）を
# node 公式イメージからそのまま持ってくる
COPY --from=node /usr/local /usr/local

ENV PYTHONUNBUFFERED=1 \
    KUMA_DATA_DIR=/data \
    HOME=/data/home

WORKDIR /app

# ブラウザ操作（Playwright MCP）用の Chromium と、日本語ページ
# 表示用のフォント。MCP サーバー 2 つはグローバルに導入し、
# npx の初回取得を待たずコマンド名で直接起動できるようにする
# （playwright-mcp / google-calendar-mcp）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @playwright/mcp@latest @cocal/google-calendar-mcp@latest

# 実行に必要なものだけを入れる（テストや .env は含めない）
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY assets/ ./assets/

RUN pip install --no-cache-dir .

# root 以外の専用ユーザーで動かす。/data はそのユーザーのものにする
# （HOME=/data/home なので、Google カレンダーのトークンのような
# ~/.config 配下の状態もデータボリュームに残る）
RUN useradd --create-home kuma \
    && mkdir -p /data/home \
    && chown -R kuma:kuma /data
USER kuma

VOLUME ["/data"]
EXPOSE 8765

# slim イメージには curl が無いので、Python 標準機能で生存確認する
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3)"

CMD ["kuma-agent", "web", "--host", "0.0.0.0", "--port", "8765"]
