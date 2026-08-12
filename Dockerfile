FROM rclone/rclone:1.75.0 AS rclone

FROM python:3.12-slim-bookworm

WORKDIR /app

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --create-home --home-dir /home/bridge bridge \
    && mkdir -p /data /appdata /real_fit \
    && chown bridge:bridge /data /appdata /real_fit \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

RUN pip install --no-cache-dir -e . \
    && chmod +x /app/scripts/*.sh

ENV PYTHONPATH=/app

USER bridge

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
