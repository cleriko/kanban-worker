# ---------------------------------------------------------------------------
# The API. Single stage, one job — Dokploy builds this repo and gets the HTTP
# server, no Build Stage field required.
#
# Transcription and meeting analysis live in the companion repo (kanban-agent),
# which runs as its own container against the same database and object store.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/src

WORKDIR /srv

# curl is for the healthcheck. No ffmpeg here — the API never touches audio
# beyond streaming it into object storage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 workconsole \
    && mkdir -p /var/lib/workconsole/objects \
    && chown -R workconsole:workconsole /var/lib/workconsole /srv

USER workconsole
EXPOSE 8080

# /health returns 503 when Postgres is unreachable, so this reflects whether the
# service can actually do its job.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
