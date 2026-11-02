# kuma-agent をコンテナで動かすためのイメージ定義。
# データ（会話・記憶など）は /data（ボリューム）に置くので、
# イメージを作り直してもデータは残る。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    KUMA_DATA_DIR=/data

WORKDIR /app

# 実行に必要なものだけを入れる（テストや .env は含めない）
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY assets/ ./assets/

RUN pip install --no-cache-dir .

# root 以外の専用ユーザーで動かす。/data はそのユーザーのものにする
RUN useradd --create-home kuma \
    && mkdir -p /data \
    && chown kuma:kuma /data
USER kuma

VOLUME ["/data"]
EXPOSE 8765

# slim イメージには curl が無いので、Python 標準機能で生存確認する
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3)"

CMD ["kuma-agent", "web", "--host", "0.0.0.0", "--port", "8765"]
