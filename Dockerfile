# Cloud Run 用（Gateway・Web/API・Agent で共通のイメージ）。どれを動かすかは環境変数 SERVICE（gateway / web / agent）で決める。
# デバイスのデーモンはこのイメージを使わない（Pi では venv で動かす）。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MUGEMUGE_SCHEMA_DIR=/app/schemas PYTHONPATH=/app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY schemas ./schemas
RUN pip install --no-cache-dir '.[cloud,agent]'
COPY services ./services
# デバイスのインストーラ（Web/API の /install.sh と /install/agentd.tar.gz が配る）
COPY deploy/install.sh deploy/mugemuge-agentd.service ./deploy/

RUN useradd --system --uid 10001 app
USER app

# Cloud Run は PORT を渡す。WebSocket の1フレームは最大 64KiB（PROTOCOL.md 2.1節）
CMD ["sh", "-c", "exec uvicorn --factory services.${SERVICE:?set SERVICE to gateway, web or agent}.app:app_from_env --host 0.0.0.0 --port ${PORT:-8080} --ws-max-size 65536 --proxy-headers --forwarded-allow-ips '*' --no-server-header"]
