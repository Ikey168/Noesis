FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/oci/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN groupadd --gid 10001 noesis \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home noesis \
    && mkdir -p /data/logs /data/packs /models-cache \
    && chown -R noesis:noesis /app /data /models-cache

COPY --chown=noesis:noesis src/ ./src/
COPY --chown=noesis:noesis services/ ./services/
COPY --chown=noesis:noesis config/ ./config/
COPY --chown=noesis:noesis configs/ ./configs/
COPY --chown=noesis:noesis contracts/ ./contracts/
COPY --chown=noesis:noesis models/ ./models/
COPY --chown=noesis:noesis packs/ ./packs/
COPY --chown=noesis:noesis db/ ./db/

USER noesis
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
