# ABAP Dump Triage – MVP Plan

> Source: `AGENTS.md`. Principle: **keep it simple** – build the smallest thing that runs the full loop
> (dump → xMatters → AI triage → Slack → fix → review → TR release), then harden.
>
> **Chat channel:** AGENTS.md allows Google Chat **or** Slack. Google Chat apps require a Business/Enterprise Google
> Workspace account (not available – personal Gmail only), so the **MVP uses Slack**. All chat access goes through
> one small `ChatClient` interface, so a Google Chat implementation can be added later without touching the pipeline.
>
> **Private values are never written in markdown** (AGENTS.md UPDATE 2). `<PLACEHOLDER>`s refer to keys in the
> git-ignored `.env` (key list: `.env.example`): `<SAP_DEV_SID>`, `<SAP_CLIENT>`, `<SAP_USER>`, `<ADT_DESTINATION>`
> (`SAP_ADT_DESTINATION`), `<SAP_PACKAGE>`, `<PHASE1_TR>` (`SAP_PHASE1_TR`), `<UAT_SID>` (`SAP_UAT_SID`),
> `<GCP_PROJECT_ID>`, `<TF_STATE_BUCKET>`.

---

## 1. Architecture (MVP)

```
 SAP DEV                         xMatters                 GCP (Cloud Run, FastAPI)                     Slack
┌──────────────────────┐  HTTPS  ┌──────────┐  webhook  ┌──────────────────────────────────────┐  Web API   ┌────────────┐
│ ZTRIAGE_JOB_MONITOR  │───────▶│ Inbound  │─────────▶│ POST /webhooks/xmatters               │──────────▶│ #triage    │
│ (scheduled, reads    │         │ HTTP     │  (shared  │   └▶ Alert pipeline (LangGraph)       │            │ channel,   │
│  cancelled jobs+dump)│         │ trigger  │   secret) │        persist → classify → post      │            │ 1 thread   │
└──────────────────────┘         │ + Flow   │           │ POST /slack/events, /slack/interactions│◀──────────│ per alert  │
          ▲                      └──────────┘           │   └▶ Chat agent (create_agent)        │            │            │
          │                                             │        tools: get_alert, request_fix, │            │            │
          │                                             │               approve, reject         │            │            │
          │                                             │ Cloud SQL Postgres (alerts, status)   │            │            │
          │                                             │ LangSmith tracing                     │            │            │
          │                                             └──────────────────▲───────────────────┘            │            │
          │ ADT (HTTP)                                                     │ Worker API (HTTPS 443)         │            │
          │                                  Local workstation ┌───────────┴──────────────────┐  Web API   │ RCA + test │
          └──────────────────────── SAP ADT MCP ◀──────────────│ Fix worker: fix sub-agent    │──────────▶│ document   │
                                                               │ (create_agent + ADT MCP),    │            └────────────┘
                                                               │ TR release (Z_TRIAGE_TR_RELEASE)│
                                                               └──────────────────────────────┘
```

### How xMatters triggers the AI (answer to step 2)
Yes – **xMatters → FastAPI webhook** is the recommended approach:
- In xMatters, add an **HTTP Trigger** (inbound from SAP) and a **Flow Designer "HTTP Request" step** (or outbound
  webhook) that POSTs the alert JSON to `https://<cloud-run-url>/webhooks/xmatters`.
- FastAPI authenticates with a shared secret header (`X-Triage-Token`), validates the payload, stores it,
  returns **202 immediately**, and runs triage in the background (xMatters has short HTTP timeouts).
- Idempotent on `alert_id` (`<jobname>-<jobcount>`) so xMatters retries don't create duplicates.

### LangGraph vs `create_agent` (answer to the question in AGENTS.md)
Use **both, each where it fits** – `create_agent` (LangChain v1) is itself built on LangGraph, so this is one stack:

| Part | Choice | Why |
|---|---|---|
| Alert pipeline (receive → store → classify → post to Slack) | **LangGraph `StateGraph`** (small, linear) | Must be deterministic: fixed steps, LLM used only for one structured-output node (category/RCA/fix). Slack post is a plain node, not an LLM tool call. |
| Conversational agent in Slack | **`create_agent`** | Simple tool-calling loop over the DB (get alert, list alerts, request fix, approve, release). |
| Fix sub-agent | **`create_agent`** + SAP ADT MCP tools (`langchain-mcp-adapters`) | Open-ended tool use (read/edit/check/activate ABAP). |
| Approval / TR release | **Deterministic code gated by DB status** | Release must never be an LLM decision; the tool refuses unless status = `APPROVED` by a human. |

A full custom multi-agent LangGraph (supervisor, interrupts, etc.) is **not needed for the MVP**. Human approval is
modelled as DB status transitions driven by Slack messages/buttons, which is simpler than graph `interrupt()` across
days-long waits.

### Alert status lifecycle (persistent DB)
`NEW → TRIAGED → FIX_REQUESTED → FIX_IN_PROGRESS → FIX_READY → APPROVED → RELEASED`
(+ `FIX_FAILED`, `REJECTED`, `CLOSED`). Every transition is written to an `alert_events` table (who, when, what).

### Repo layout (target)
```
ABAP/                      # copies of every ABAP object created in SAP
app/
  main.py                  # FastAPI: /webhooks/xmatters, /slack/events, /slack/interactions, /health
  config.py
  db/ (models.py, repo.py) # SQLAlchemy; Postgres in GCP, SQLite for local tests
  pipeline/alert_graph.py  # LangGraph StateGraph
  agents/chat_agent.py     # create_agent + DB tools
  agents/fix_agent.py      # create_agent + SAP ADT MCP tools
  chat/base.py             # ChatClient interface: post_alert, reply_in_thread, upload_file
  chat/slack.py            # Slack implementation (slack_bolt + slack_sdk, Block Kit)
  chat/fake.py             # in-memory client for tests / local runs
  reports/test_doc.py      # builds RCA + before/after test document
worker/                    # local fix worker (runs on the workstation with sap-adt MCP, polls the worker API)
infra/terraform/           # ALL GCP services/resources (AGENTS.md UPDATE 1)
tests/
docs/ (xmatters-contract.md, slack-setup.md, slack-app-manifest.yaml, runbook.md)
```

---

## 2. Phase 0 – Foundations (prerequisites)

*Status checked 2026-11-18, updated 2026-11-21.*

- [x] SAP ADT MCP connection to DEV works for reads – destination `<ADT_DESTINATION>` logged on, repository search OK.
- [x] SAP ADT MCP write works (create/activate) – verified in Phase 1. Code execution only via ABAP Unit.
- [x] Package agreed: `<SAP_PACKAGE>` (exists in <SAP_DEV_SID>). No `ZTRIAGE*` objects yet, so no name clashes.
- [x] Workbench TR **<PHASE1_TR>** in `<SAP_PACKAGE>` created for Phase 1 objects.
- [x] SM59 destination `ZTRIAGE_XMATTERS` maintained (per user).
- [x] GCP project `<GCP_PROJECT_ID>` (asia-southeast1), gcloud logged in, Terraform written in `infra/terraform` and validated.
- [x] **Billing enabled on `<GCP_PROJECT_ID>`**.
- [x] `terraform apply` (Cloud SQL, Secret Manager, Artifact Registry, Cloud Run) – 2026-11-21; state in the versioned
      state bucket; `terraform plan` shows no changes afterwards.
- [x] LangSmith API key + project set in `.env` (`LANGSMITH_*`) – not yet validated with a trace.
- [x] `.env` has `OPENROUTER_API_KEY` and `MODEL`; `.env` is git-ignored.
- [x] OpenRouter smoke test (`scripts/smoke_openrouter.py`): plain, tool calling and structured output OK.
      `json_schema` mode classified correctly where `function_calling` did not → default `json_schema`.
- [x] Network path to SAP: resolved by running the fix worker locally with the `sap-adt` MCP (see Risk 3) – no VPC/VPN needed.
      The worker reaches the backend over HTTPS (worker API); the Cloud SQL Auth Proxy port 3307 is blocked by the corporate network.

**Success:** ADT read works from this workstation; GCP project exists; `terraform plan` is clean after apply
(no drift); no GCP service or resource created by hand except the state bucket and secret values.

---

## 3. Phase 1 – SAP Backend (ABAP)

All objects created in SAP (<SAP_DEV_SID>/<SAP_CLIENT>, package `<SAP_PACKAGE>`, TR **<PHASE1_TR>**) via ADT MCP **and** copied into `ABAP/`
(abapGit-style file names, see `ABAP/README.md`).

### 1.1 `ZTRIAGE_DEMO_BUG` – program with intentional bugs
Selection screen: `P_MODE` + data parameters, so one program produces all three triage categories:

| Mode / variant | Behaviour | Actual result in <SAP_DEV_SID> | Expected category |
|---|---|---|---|
| `C` / `ZBUG_DIV0` (qty 0) | `p_total / p_qty` without zero check | Dump `COMPUTE_INT_ZERODIVIDE`, line 55 | **Developer** (program bug – fixable) |
| `M` / `ZBUG_DATA` | Material `ZTRIAGE_NO_MAT` not in MARA → `MESSAGE TYPE 'E'` | Job cancelled "Material ... does not exist" | **Functional** (data issue) |
| `R` / `ZBUG_RFC` | `CALL FUNCTION 'RFC_PING' DESTINATION 'ZTRIAGE_DUMMY'` | Dump `CALL_FUNCTION_NO_DEST` | **Basis** (connection) |
| `C` / `ZOK` (qty 10) | Normal run | Finished, unit price 100 | – ("after fix" baseline) |

### 1.2 `ZTRIAGE_JOB_MONITOR` (+ class `ZCL_TRIAGE_JOB_MONITOR`) – cancelled-job collector → xMatters
- Reads `TBTCO` with `STATUS = 'A'` ended in the last `P_DAYS` days; skips jobs already sent. "Sent" list is kept in
  cluster table `INDX` (ID `ZTRIAGE_SENT`, 30 days) – ADT cannot create DDIC tables here, so no custom table.
- Per job: step program + **real variant** (`TBTCP`), times, user, job log (`BP_JOBLOG_READ`), short dump
  (`SNAP_BEG`: runtime error, program, include, line), first meaningful error message.
- Flat JSON (`/ui2/cl_json`) POSTed via SM59 `ZTRIAGE_XMATTERS`; marked as sent only on HTTP 2xx (else retried next run).
  Contract + real sample: `docs/xmatters-contract.md`.
- Excludes `ZTRIAGE_RUN_*` (fix-agent reproduce runs) and itself. `P_TEST` = list only, `P_JSON` = print payload.
- Scheduled in SM36 every 5 minutes.

### 1.3 `ZCL_TRIAGE_RUNNER` – reproduce runner
The ADT MCP has **no "run program" action**; the only way to execute code is **ABAP Unit**. So:
- `ZCL_TRIAGE_RUNNER->run_job( program, variant | params )` runs the program as a real background job (`JOB_SUBMIT`
  keeps the real variant name), waits, and returns status, job log, spool output and dump. Also `save_variant`.
- The fix agent (Phase 3.4) will execute it through an ABAP Unit test method and read the result from the test
  messages (before/after evidence). Default job name `ZTRIAGE_RUN_<program>` → ignored by the monitor.

### 1.4 `Z_TRIAGE_TR_RELEASE` – release TR only (RFC function module)
Input: TR number. Releases the open tasks first, then the request (`TR_RELEASE_REQUEST`). Returns release status and
messages (e.g. already released, lock/object errors). **No import** – the import into `<UAT_SID>` stays with the normal
TMS/Basis process. Needs `S_TRANSPRT` authorization for release (standard developer authorization).

### Phase 1 success checklist
*Status 2026-11-19. Tests: `ZCL_TRIAGE_RUNNER` 7/7 passed, `ZCL_TRIAGE_JOB_MONITOR` 9/9 passed (incl. dedup + retry).*
- [x] `ZTRIAGE_DEMO_BUG` + all objects activated in DEV; source copies in `ABAP/` (taken from the active version).
- [x] Each variant run as a real background job gives the expected result (3 cancelled, `ZOK` finished); dump name + line found.
- [x] Monitor (collect / test mode) reports the 3 cancelled jobs with correct program, real variant, times, error text, dump; `ZOK` not reported; `ZTRIAGE_RUN_*` excluded.
- [x] Report `ZTRIAGE_JOB_MONITOR` runs as background job in test mode and lists the jobs.
- [x] `ZCL_TRIAGE_RUNNER` returns dump for `ZBUG_DIV0` and normal output (spool "Unit price 100") for `ZOK`.
- [x] `Z_TRIAGE_TR_RELEASE` (RFC-enabled): invalid TR → error, task → error, released TR → "already released" (no dump).
- [x] All objects in package `<SAP_PACKAGE>`, one TR (<PHASE1_TR>); ATC: 0 errors, 1 warning (sequential DELETE on small hashed table), infos only.
- [x] **Real send:** monitor without test mode → alerts received in xMatters (confirmed by user 2026-11-18).
- [x] Second run sends nothing new (dedup) – unit test `ltc_dedup->second_run_sends_nothing` (HTTP response injected via
      test seam, separate INDX key; nothing reaches xMatters). Re-observe once the job is scheduled in SM36.
- [x] Failure to reach xMatters is logged and retried next run – unit test `ltc_dedup->failed_send_is_retried` (wrong
      destination name → send result not OK with message, job not marked sent, sent again on the next run).
- [x] `Z_TRIAGE_TR_RELEASE` releases a real TR – tested in Phase 3.5 (2026-11-19 locally, 2026-11-21 via GCP).
- [ ] Job `ZTRIAGE_JOB_MONITOR` scheduled in SM36 every 5 minutes (ideally under a batch user) – the xMatters flow to the
      backend is ready; until then the monitor is started manually (E2E test 2026-11-21: foreground run via WebGUI).

---

## 4. Phase 2 – Slack Integration (TODO list)

A **Slack app with a bot user** both sends (Web API) and receives (Events API + Interactivity) – works on the
free Slack plan with any account. Library: `slack_bolt` (FastAPI adapter) – it verifies request signatures,
acks within 3 s and handles Slack retries. Google Chat stays possible later behind the same `ChatClient` interface.

Slack app settings have no Terraform provider in scope, so they are created **once from an app manifest**
(`docs/slack-app-manifest.yaml`, committed, no secrets) and documented in `docs/slack-setup.md`.
The two Slack secrets are stored in Secret Manager (containers created by **Terraform**, values added by hand).

**User (manual, ~10 min):**
- [x] 1. Slack workspace (existing, or a new free one) where you can install apps.
- [x] 2. api.slack.com/apps → *Create New App* → *From a manifest* → paste `docs/slack-app-manifest.yaml`.
- [x] 3. *Install to Workspace*; copy **Bot User OAuth Token** (`xoxb-…`) and **Signing Secret** (Basic Information) into `.env`
       (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`).
- [x] 4. Create the triage channel (e.g. private `#abap-triage`), `/invite` the bot; copy the **channel ID** (`C…`) into `.env` (`SLACK_CHANNEL_ID`).
- [x] 5. *(Optional, local dev before Cloud Run exists)* App-level token with `connections:write` (`xapp-…`) → `.env` `SLACK_APP_TOKEN`
       to run in **Socket Mode** (no public URL needed).
- [x] 6. After `terraform apply`: set Event Subscriptions + Interactivity Request URLs to the Cloud Run URL (manifest placeholders), Slack verifies them.

**Me (code + Terraform):**
- [x] 7. **Terraform:** Secret Manager secrets `slack-bot-token`, `slack-signing-secret` + Cloud Run env `SLACK_CHANNEL_ID`.
- [x] 8. `ChatClient` interface + `SlackChatClient` (`chat.postMessage` with Block Kit; replies with `thread_ts`;
       file upload via `files.getUploadURLExternal` / `files.completeUploadExternal`) + `FakeChatClient` for tests.
- [x] 9. One **thread per alert**: parent message = alert card (category, RCA, recommendation, buttons
       `Fix it` / `Approve` / `Reject`); store `channel` + `ts` on the alert → thread ↔ `alert_id` mapping.
- [x] 10. `POST /slack/events`: `app_mention` and thread `message` events → chat agent; ignore bot's own messages; dedupe `event_id` (Slack retries).
- [x] 11. `POST /slack/interactions`: button clicks (`block_actions`) carry `alert_id` → status transitions; ack < 3 s, work async.

### Phase 2 success checklist
*Status 2026-11-18: local end-to-end in Socket Mode passed. 2026-11-21: re-checked in HTTP mode on Cloud Run (Socket Mode off).*
- [x] Requests with a wrong signature or a timestamp older than 5 min are rejected (401) – unit test, HTTP mode.
- [x] Slack Request URL verification (`url_verification` challenge) succeeds – unit test, HTTP mode.
- [x] Bot token valid (`auth.test` OK).
- [x] Backend posts an alert card to the channel (`python -m app.chat.slack --test` + 3 real cards from the pipeline).
- [x] Reply with `thread_ts` lands in the same thread; mentioning the bot in that thread reaches the backend with the right `alert_id`.
- [x] Button click (*Fix it*) reaches the backend with the `alert_id` → `FIX_REQUESTED`, actor = Slack user, reply in thread (Socket Mode 2026-11-18; HTTP mode via `/slack/interactions` on Cloud Run 2026-11-21).
- [x] Slack retry of the same event (`X-Slack-Retry-Num`) is processed only once – Bolt middleware acks and drops retries
      (unit test, HTTP mode).
- [x] File upload (test document + before/after screenshots) appears in the alert thread (2026-11-19 local, 2026-11-21 via GCP).

---

## 5. Phase 3 – Agentic AI (LangGraph + FastAPI)

### 3.1 Alert pipeline (LangGraph) – xMatters → DB → Slack
Nodes: `validate → persist(NEW) → classify → persist(TRIAGED) → post_to_chat → persist(Slack channel + thread ts)`.
The Slack post is deterministic code (`ChatClient`); if classification fails, still post the raw alert with "classification unavailable".

### 3.2 Classification + RCA + recommendation
`classify` node = one LLM call with **structured output**:
`{category: DEVELOPER|FUNCTIONAL|BASIS, confidence, root_cause, recommendation, fixable_by_agent: bool}`.
Prompt includes runtime error, exception, program/line, job log, and a short rules guide
(e.g. `ZERODIVIDE`, `ASSIGN`/`ITAB_LINE_NOT_FOUND` → Developer; missing master data / customizing → Functional;
`TSV_TNEW_PAGE_ALLOC_FAILED`, `TIME_OUT`, RFC/communication errors, DB space → Basis).
Optionally the node reads the source around the dump line via ADT to improve RCA (nice-to-have, not MVP-blocking).

### 3.3 Chat with the agent
`create_agent` with tools: `get_alert(id)`, `list_open_alerts()`, `get_alert_history(id)`, `request_fix(id, program, variant)`,
`approve_fix(id)`, `reject_fix(id, reason)`. Conversation memory per Slack thread (LangGraph checkpointer, Postgres;
thread id = `channel:thread_ts`). Triggered by mentioning the bot in an alert thread; the thread resolves the `alert_id`.
`request_fix` only allowed if category = DEVELOPER; asks for program + variant if missing (defaults from the alert).
The same actions are available as buttons on the alert card (`Fix it`, `Approve`, `Reject`).

### 3.4 Fix sub-agent (SAP ADT)
Runs in the **local fix worker** (picks up `FIX_REQUESTED` through the backend's worker API over HTTPS); status `FIX_IN_PROGRESS`.
1. Reproduce **before**: run `ZCL_TRIAGE_RUNNER->run_job( program, variant )` through an ABAP Unit test method
   (only way to execute code via ADT) → capture status, dump, job log, spool.
2. Read source (`abap_object_read_file`), locate faulty line, create/assign TR (`abap_transport_create_request`).
3. Edit (`abap_object_edit_file`), syntax check (`abap_object_check`), activate (`abap_object_activate`), ATC (`abap_atc_run_*`).
4. Reproduce **after** with the same variant → must succeed.
5. Produce **test document** (Markdown → PDF/HTML) containing: alert summary, RCA, code diff (`abap_transport_get_unified_diff`),
   before/after **real SAP screenshots** via SAP GUI for HTML (WebGUI) + headless Chromium: *before* = the short dump in
   ST22 (long text + source extract at the `>>>>>` line), *after* = the program's result screen; text-rendered images as
   fallback when WebGUI is not configured or fails. Plus ATC result and TR number.
6. Upload the test document (PDF/HTML) **directly into the alert's Slack thread** (no GCS bucket needed) with RCA +
   summary of the change; status `FIX_READY`; copy changed source to `ABAP/`.
Guardrails: only objects in allowed packages (`Z*`/`ZTRIAGE`), max N edit iterations, never release TR itself.

### 3.5 Review, approval, TR release
Any member of the triage channel clicks **Approve** (or tells the bot "approve") → status `APPROVED` (Slack user id
recorded) → the local fix worker picks it up and calls `Z_TRIAGE_TR_RELEASE` → status `RELEASED` → message in the thread
"TR <number> released – ready for import to <UAT_SID>".
**Reject** → `REJECTED` (optionally re-trigger fix with feedback).

Release goes through the RFC-enabled wrapper `Z_TRIAGE_TR_RELEASE` (Phase 1.4), called by the local fix worker.
The import into `<UAT_SID>` is **not** done by the agent; it follows the normal TMS/Basis import process.

### Phase 3 success checklist
*Status 2026-11-18: 3.1 + 3.2 built (`app/`), 40 unit tests + 3 live tests pass (`uv run pytest`, `uv run pytest -m live`).*
- [x] Unit tests (pytest) for DB repo, status transitions (illegal transitions rejected), payload validation, idempotency.
- [x] `POST /webhooks/xmatters` with a sample payload → row in DB, status `TRIAGED`, card in the Slack channel in 1–3 s
      (local end-to-end: 3 alerts, correct categories, *Fix it* only on the Developer card; duplicate → `200`, not re-posted).
- [x] Same payload posted twice → one alert, one chat message, classifier called once (`200 duplicate`).
- [x] Bad / missing / unconfigured token → 401; malformed payload → 422; LLM down → rule-based result, card marked
      "rule-based"; chat down → alert stays `TRIAGED` in DB.
- [x] Live classification of the 3 demo dumps: DEVELOPER / FUNCTIONAL / BASIS all correct (confidence 0.99).
- [x] Classification eval: 3 demo + 10 synthetic payloads (`tests/fixtures/eval_classification.json`), 13/13 correct
      (target ≥ 90 %); LangSmith dataset `abap-triage-classification` + experiment
      (`uv run python -m app.pipeline.evaluation [--langsmith]`, live test `test_live_eval_accuracy`).
- [x] In the alert's Slack thread: "@bot what is the root cause?" → answer grounded in stored alert data; "list open alerts" works
      (3.3 chat agent `app/agents/chat_agent.py`; 11 unit tests + 4 live tests).
- [x] "Fix it" on a Functional/Basis alert is refused with an explanation (button and chat).
- [x] Chat agent never approves on its own: "what changed?" on a `FIX_READY` alert leaves it `FIX_READY` (live test).
- [x] Conversation memory per Slack thread (checkpointer, `thread_id = <channel>:<thread_ts>`); other threads don't see it.
- [x] Agent/LLM failure → deterministic status answer instead of silence.
- [x] 3.4 fix worker built (`app/fix/`): ADT MCP gateway (Python → local Eclipse MCP, read-only smoke test OK),
      reproduce via ABAP Unit helper `ZCL_TRIAGE_EVIDENCE` (`$TMP`), guarded fix agent (only the alert's Z/Y program,
      worker-chosen TR, ≤5 edits, activate only after clean check), source diff, ATC, LLM RCA, HTML report + PNG
      before/after "screenshots"; 11 offline tests (happy path, no-repro, not activated, fix not helping, standard program).
- [x] **Live (2026-11-19):** "Fix it" on `ZBUG_DIV0` → fix agent changed `ZTRIAGE_DEMO_BUG` in DEV (zero-quantity guard,
      warning message, no job cancel); before = `COMPUTE_INT_ZERODIVIDE` line 55, after = FINISHED; ATC 0 errors/0 warnings;
      before/after PNGs + HTML test document + RCA uploaded to the thread; status `FIX_READY`; new fix TR recorded.
      Phase 1 TR was released first so the fix got its own TR.
      Lessons built in: MCP structured content, tool errors back to the agent, TR reuse on retry, "no E/A message in
      background jobs" rule, wait + retry for stale program load after activation, second attempt with after-run result.
- [x] 3.5 release code: worker releases the fix TR of `APPROVED` alerts via `Z_TRIAGE_TR_RELEASE` (ABAP Unit helper);
      failure keeps `APPROVED` + error in thread (4 offline tests). Phase 1 TR released with the same mechanism.
- [x] Release before approval is refused (status rules + tests); after **Approve** in Slack the fix TR was released
      by the worker (2026-11-19, TR status `O`, object no longer locked) and the alert is `RELEASED`.
- [ ] If release fails (e.g. object locked, ATC block), status stays `APPROVED` and the error is posted to the thread.
- [x] Release messages in the chat name only the root TR, not its tasks (AGENTS.md *Improvements* 1; unit tests).
- [ ] Every step visible as a trace in LangSmith; every status change in `alert_events`.
- [x] Any GCP resource added in Phase 3 is in Terraform; `terraform plan` shows no changes after apply (2026-11-21, also after pause).
- [x] Local end-to-end (2026-11-19): alert via webhook → Slack card → *Fix it* → fix in DEV → test document → *Approve* →
      TR released, no manual step other than the Slack actions.
- [x] Code ready for GCP (2026-11-20): `Dockerfile` (built by Cloud Build), Postgres via `psycopg` (URL from
      `DB_SOCKET`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` or `DATABASE_URL`), Postgres chat-memory checkpointer, Slack HTTP mode
      (`SLACK_MODE=http` in Terraform). Checked locally against a real (embedded) Postgres: tables, duplicate check,
      history, chat memory across restarts; production wiring in HTTP mode (signature check, `/health`, 401 webhook).
- [x] GCP deploy (2026-11-21): `terraform plan` → OK → `apply`, secret values, image build (Cloud Build) + deploy, Slack
      Request URLs, Socket Mode off, xMatters flow step "Send to ABAP Triage" (see `infra/terraform/README.md`).
- [x] Worker API (2026-11-21): fix worker ↔ backend over HTTPS with `X-Worker-Token` (port 3307 for the Cloud SQL Auth
      Proxy is blocked); worker may only set fix/release statuses and `fix_tr` (tests incl. a full fix run over the API).
- [x] Pause/resume (2026-11-21): `paused = true` stops Cloud SQL and scales Cloud Run to 0; the backend starts without the
      database and answers `503` (fixes a failed pause where the new revision could not start).
- [x] Real SAP screenshots via WebGUI (2026-11-21): after = list output of the variant run; before = ST22 dump long text +
      source extract (WebGUI shows no dump screen when it happens). Tested directly; not yet inside a full fix run.
- [x] **Full end-to-end test** (2026-11-21, via GCP): demo bug re-introduced (new TR) → real cancelled `ZBUG_DIV0` job →
      monitor (started manually via WebGUI, not yet SM36) → xMatters `202` → Cloud Run `202` → LLM triage → Slack card
      (~4 s) → *Fix it* → worker on the workstation via the worker API → fix + test document → *Approve* → TR released,
      alert `RELEASED`. Open: the *before* screenshot showed the selection screen (fixed afterwards with ST22, see above).

---

## 6. Risks / gaps to resolve early

1. **TR release** – the ADT MCP tools have **no release tool**, so this goes through the RFC wrapper in 1.4.
   The local fix worker calls it via `pyrfc` or an SAP HTTP wrapper (same workstation as the `sap-adt` MCP, Risk 3).
2. **Screenshots** – **resolved (2026-11-21):** real screens via SAP GUI for HTML (WebGUI) + Playwright, wherever the
   worker can reach WebGUI (password logon with `saml2=disabled`, since SSO via IAS/Okta can't be automated).
3. **Network GCP → SAP DEV** – the fix sub-agent needs ADT access to on-prem SAP (VPN / Interconnect / SAP Cloud Connector / a VM in the corporate network).
   **Decision:** the fix worker (fix sub-agent + TR release) runs on the workstation that has the `sap-adt` MCP.
   It polls the backend's worker API over HTTPS for `FIX_REQUESTED` / `APPROVED` alerts (the Cloud SQL Auth Proxy port
   3307 is blocked by the corporate network) and posts results to Slack itself.
   Cloud Run only hosts the webhooks, alert pipeline and chat agent.
4. **SAP → xMatters outbound** – needs firewall/proxy opening and SSL certificate in STRUST.
5. **Cloud Run background work** – done: min-instances = 1 + "CPU always allocated" while running (the fix job runs in
   the worker, not in Cloud Run).
6. **Safety** – agent restricted to DEV, allowed packages, and never releases without a recorded human approval.

## 7. Decisions (confirmed 2026-11-18)
1. **LLM: OpenRouter.** Key and model come from `.env`; nothing hard-coded. Use `langchain-openai` `ChatOpenAI` with
   `base_url=https://openrouter.ai/api/v1`. The chosen model must support tool calling + structured output
   (needed by the classifier and agents) – checked at startup with a smoke call.
   ```env
   OPENROUTER_API_KEY=...
   MODEL=...                       # OpenRouter model id (any tool-calling model from openrouter.ai/models)
   OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
   ```
   `.env` is git-ignored; in Cloud Run the same variables come from Secret Manager.
2. **Database: Cloud SQL Postgres** (SQLite only for local unit tests). Also used for the LangGraph checkpointer.
3. **Release TR only** – the agent releases the TR after approval; import into `<UAT_SID>` follows the normal TMS/Basis process (changed 2026-11-18, was "release + import").
4. **Package: `<SAP_PACKAGE>`** for all `ZTRIAGE*` objects.
5. **Approval: anyone in the triage Slack channel** for now (approver's Slack user id is still recorded on the alert).

6. **TR:** create a new workbench TR in `<SAP_PACKAGE>` for the Phase 1 objects.
7. **UAT target system: `<UAT_SID>`** – shown in the Slack message after release; the import itself is not automated.
8. **SM59 destination `ZTRIAGE_XMATTERS`** is already maintained (xMatters URL + auth) – `ZTRIAGE_JOB_MONITOR` uses it as-is.
9. **Terraform for all GCP services** (AGENTS.md UPDATE 1). Code in `infra/terraform`, state in `gs://<TF_STATE_BUCKET>`.
   Every new GCP need in later phases (APIs, IAM, buckets e.g. for test documents, Cloud Run settings/env vars)
   is added to Terraform, not created with `gcloud`/Console. Only exceptions: the state bucket (bootstrap),
   secret *values* (kept out of state), and the Slack app (created from the manifest in Slack's UI). Container image builds/deploys
   (`gcloud run deploy` / Cloud Build) update the image only; Terraform ignores image changes.
   Project `<GCP_PROJECT_ID>`, region `asia-southeast1`. Project id and state bucket live only in git-ignored
   `terraform.tfvars` / `backend.hcl` (committed: `*.example`).
   Cost control: `paused = true` stops Cloud SQL and removes the always-on Cloud Run instance (data/URL/tokens kept);
   `terraform destroy` for the end of the project. `sql_instance_suffix` (`abap-triage-pg-<suffix>`) lets the DB be
   recreated right after a destroy (deleted Cloud SQL names are blocked ~1 week). See `infra/terraform/README.md`.
10. **No private information in markdown files** (AGENTS.md UPDATE 2) – TR numbers, GCP project id, SAP system id /
    client / user, package, bucket names, URLs with keys. Use `<PLACEHOLDER>`s (legend at the top); real values only
    in git-ignored files (`.env`, `terraform.tfvars`, `backend.hcl`). Sample payloads are anonymised.
    Also applies to committed code where avoidable (e.g. no hard-coded TR numbers in ABAP tests).
11. **Chat channel: Slack for the MVP** (AGENTS.md now allows "google chat/slack"; Google Chat apps need a
    Business/Enterprise Workspace account, which isn't available). Slack app from manifest, bot in one triage channel,
    one thread per alert. Code talks to a `ChatClient` interface so Google Chat can be added later.
    Slack secrets: `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` (Secret Manager), `SLACK_CHANNEL_ID` (plain env);
    optional `SLACK_APP_TOKEN` for Socket Mode during local development.

## 8. Build order
Phase 0 → 1.1 → 1.3 → 1.2 (test against xMatters via `ZTRIAGE_XMATTERS`) → 1.4 → 3.1 + 3.2 with `FakeChatClient` → Phase 2 (Slack) → 3.3 → 3.4 → 3.5 → E2E.
(Phase 3.1 can be built before Phase 2 by using the fake chat client; Phase 2 then just swaps in `SlackChatClient`.)
