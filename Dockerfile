# ABAP triage backend for Cloud Run (built by Cloud Build - no local Docker needed):
#   gcloud builds submit --tag <REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<REPO>/abap-triage:<TAG>
# Only the backend runs here (webhook, pipeline, Slack, chat agent). The fix worker stays next to Eclipse ADT.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# dependencies first (cached layer), exactly as pinned in uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    SLACK_MODE=http
EXPOSE 8080
# Cloud Run sets PORT; one worker process (Slack Bolt + background tasks live in it)
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
