# Runbook (local development)

Prerequisites: `uv` (installs Python 3.12 and all packages), filled `.env` (keys: `.env.example`).

```bash
uv sync                                      # install / update dependencies
uv run pytest                                # unit tests (no external calls)
uv run pytest -m live -s                     # live: real OpenRouter classification of the 3 demo alerts
uv run python scripts/smoke_openrouter.py    # model supports plain / tool calling / structured output?
uv run python -m app.chat.slack --test       # post a test card + thread reply to SLACK_CHANNEL_ID
```

## Run the backend locally (Slack Socket Mode)
```bash
uv run uvicorn app.main:app --port 8000
```
- Connects to Slack via Socket Mode (`SLACK_MODE=socket`, needs `SLACK_APP_TOKEN`) - no public URL needed.
- Local database: `triage.db` (SQLite, git-ignored). Delete it to start fresh.
- LangSmith traces go to `LANGSMITH_PROJECT` when `LANGSMITH_TRACING=true`.

In a second terminal, send an alert like xMatters would:
```bash
uv run python scripts/send_alert.py div0 --new-id    # Developer -> card with "Fix it" button
uv run python scripts/send_alert.py data --new-id    # Functional
uv run python scripts/send_alert.py rfc  --new-id    # Basis
```
Then in Slack: click **Fix it** on the Developer card (status -> `FIX_REQUESTED`), or talk to the chat agent by
mentioning `@abap-triage` in an alert thread, e.g. "what is the root cause?", "show the job log", "who requested the
fix?", "please fix it", "list open alerts" (outside a thread). Memory is per thread (`triage_chat.db`, git-ignored).

## Webhook contract
`POST /webhooks/xmatters`, header `X-Triage-Token: <XMATTERS_WEBHOOK_TOKEN>`, body = `docs/xmatters-contract.md`.
Responses: `202 accepted` (processed in background), `200 duplicate` (same `alert_id` already received),
`401` (missing/wrong token), `422` (invalid payload).
