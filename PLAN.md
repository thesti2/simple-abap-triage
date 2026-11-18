# ABAP Dump Triage – MVP Plan

> Source: `AGENTS.md`. Principle: **keep it simple** – build the smallest thing that runs the full loop
> (dump → xMatters → AI triage → Google Chat → fix → review → TR release), then harden.

---

## 1. Architecture (MVP)

```
 SAP DEV                         xMatters                 GCP (Cloud Run, FastAPI)                     Google Chat
┌──────────────────────┐  HTTPS  ┌──────────┐  webhook  ┌──────────────────────────────────────┐  Chat API  ┌────────────┐
│ ZTRIAGE_JOB_MONITOR  │───────▶│ Inbound  │─────────▶│ POST /webhooks/xmatters               │──────────▶│ Triage     │
│ (scheduled, reads    │         │ HTTP     │  (shared  │   └▶ Alert pipeline (LangGraph)       │            │ space      │
│  cancelled jobs+dump)│         │ trigger  │   secret) │        persist → classify → post      │            │            │
└──────────────────────┘         │ + Flow   │           │ POST /chat/events  ◀──────────────────│◀──────────│ user msgs  │
          ▲                      └──────────┘           │   └▶ Chat agent (create_agent)        │            │            │
          │ ADT (HTTP)                                  │        tools: get_alert, request_fix, │            │            │
          │                                             │               approve, release        │            │            │
          └─────────────────── SAP ADT MCP ◀────────────│   └▶ Fix sub-agent (create_agent +    │──────────▶│ RCA + test │
                                                        │        SAP ADT MCP tools)             │            │ doc        │
                                                        │ Postgres (alerts, status, history)    │            └────────────┘
                                                        │ LangSmith tracing                     │
                                                        └──────────────────────────────────────┘
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
| Alert pipeline (receive → store → classify → post to Chat) | **LangGraph `StateGraph`** (small, linear) | Must be deterministic: fixed steps, LLM used only for one structured-output node (category/RCA/fix). Chat post is a plain node, not an LLM tool call. |
| Conversational agent in Google Chat | **`create_agent`** | Simple tool-calling loop over the DB (get alert, list alerts, request fix, approve, release). |
| Fix sub-agent | **`create_agent`** + SAP ADT MCP tools (`langchain-mcp-adapters`) | Open-ended tool use (read/edit/check/activate ABAP). |
| Approval / TR release | **Deterministic code gated by DB status** | Release must never be an LLM decision; the tool refuses unless status = `APPROVED` by a human. |

A full custom multi-agent LangGraph (supervisor, interrupts, etc.) is **not needed for the MVP**. Human approval is
modelled as DB status transitions driven by Chat messages/buttons, which is simpler than graph `interrupt()` across
days-long waits.

### Alert status lifecycle (persistent DB)
`NEW → TRIAGED → FIX_REQUESTED → FIX_IN_PROGRESS → FIX_READY → APPROVED → RELEASED`
(+ `FIX_FAILED`, `REJECTED`, `CLOSED`). Every transition is written to an `alert_events` table (who, when, what).

### Repo layout (target)
```
ABAP/                      # copies of every ABAP object created in SAP
app/
  main.py                  # FastAPI: /webhooks/xmatters, /chat/events, /health
  config.py
  db/ (models.py, repo.py) # SQLAlchemy; Postgres in GCP, SQLite for local tests
  pipeline/alert_graph.py  # LangGraph StateGraph
  agents/chat_agent.py     # create_agent + DB tools
  agents/fix_agent.py      # create_agent + SAP ADT MCP tools
  chat/client.py           # Google Chat API wrapper (send, thread reply, cards)
  reports/test_doc.py      # builds RCA + before/after test document
tests/
docs/ (xmatters-contract.md, google-chat-setup.md, runbook.md)
```

---

## 2. Phase 0 – Foundations (prerequisites)

- [ ] Confirm SAP ADT MCP connection to the DEV system works (read an object, create in `$TMP` or a package).
- [ ] Agree on package (e.g. `ZTRIAGE`) and a **workbench TR** for Phase 1 objects.
- [ ] GCP project, Cloud Run, Secret Manager, Cloud SQL (Postgres) – or SQLite on a volume for the very first demo.
- [ ] LangSmith API key + project (`LANGSMITH_TRACING=true`).
- [ ] LLM provider decision (see Open Questions).
- [ ] Network path **GCP → SAP DEV** for the fix sub-agent (see Risks).

**Success:** ADT read works from this workstation; GCP project exists; secrets defined.

---

## 3. Phase 1 – SAP Backend (ABAP)

All objects created in SAP via ADT MCP **and** copied into `ABAP/` (one file per object, e.g. `ABAP/ZTRIAGE_DEMO_BUG.prog.abap`).

### 1.1 `ZTRIAGE_DEMO_BUG` – program with intentional bugs
Selection screen: `P_MODE` + a couple of data parameters, so one program produces all three triage categories:

| Mode | Behaviour | Expected dump / message | Expected category |
|---|---|---|---|
| `1` | Divides by a value that is 0 for certain input (e.g. quantity = 0) | `COMPUTE_INT_ZERODIVIDE` | **Developer** (program bug – fixable) |
| `2` | Reads a material/customer that doesn't exist, then `MESSAGE ... TYPE 'A'` / unhandled exception | Job cancelled with data-not-found message | **Functional** (data issue) |
| `3` | `CALL FUNCTION ... DESTINATION 'ZTRIAGE_DUMMY'` (non-existent / unreachable RFC) | `CALL_FUNCTION_DESTINATION_NO_T` / `COMMUNICATION_FAILURE` | **Basis** (connection) |
| `0` | Normal run | Success, writes list output | – (used as "after fix" baseline) |

Plus variants `ZBUG_DIV0`, `ZBUG_DATA`, `ZBUG_RFC` so the agent can be given "program + variant" to reproduce.

### 1.2 `ZTRIAGE_JOB_MONITOR` – cancelled-job collector → xMatters
- Reads `TBTCO` where `STATUS = 'A'` and end time > last-run watermark (watermark in `TVARVC` or small table `ZTRIAGE_SENT` keyed by jobname+jobcount → dedup).
- For each job: jobname, jobcount, step program + variant (`TBTCP`), start/end time, user, job log (`BP_JOBLOG_READ`), and if a short dump exists: runtime error, exception, program/include/line (`SNAP_BEG` / ST22 data).
- Builds JSON (`/ui2/cl_json`) and POSTs to xMatters inbound URL via SM59 destination type G (`cl_http_client=>create_by_destination`); auth via xMatters API key/basic auth stored in the destination.
- Logs result per job; marks as sent only on HTTP 2xx. Test mode parameter `P_TEST` = print payload, don't send.
- Scheduled in SM36 every 5 minutes.

### 1.3 (Recommended helper) `ZTRIAGE_RUN` – reproduce runner
The ADT MCP can edit/activate code but **cannot press F8 in SAP GUI**. To let the fix agent reproduce "before/after", add a tiny
runner (class with `IF_OO_ADT_CLASSRUN`, or ABAP Unit test class) that `SUBMIT`s the program with a variant
`EXPORTING LIST TO MEMORY`, catches the dump, and returns output/error text. This is the evidence source for Phase 3.4.

### Phase 1 success checklist
- [ ] `ZTRIAGE_DEMO_BUG` activated in DEV; source copy in `ABAP/` matches SAP (diff = empty).
- [ ] Running each variant as a background job gives the expected result (3 cancelled jobs, 1 finished); dumps visible in ST22.
- [ ] `ZTRIAGE_JOB_MONITOR` in test mode lists exactly the 3 cancelled jobs with correct program, variant, times, error text, dump name.
- [ ] Second run sends nothing new (dedup works).
- [ ] With xMatters URL (or a temporary request-bin / local FastAPI mock) configured, payload arrives with HTTP 2xx and matches `docs/xmatters-contract.md`.
- [ ] Failure to reach xMatters is logged and the job is retried on the next run (not marked sent).
- [ ] Job scheduled in SM36 and runs periodically.
- [ ] `ZTRIAGE_RUN` returns the dump text for `ZBUG_DIV0` and normal output for mode `0`.
- [ ] All objects in one TR, ATC check clean (except intentional bugs).

---

## 4. Phase 2 – Google Chat Integration (TODO list)

**Sending + receiving** requires a **Google Chat app** (an incoming webhook can only send, not receive).

- [ ] 1. In GCP project: enable **Google Chat API**.
- [ ] 2. Create a **service account** for the app (used to post messages via Chat API, scope `https://www.googleapis.com/auth/chat.bot`); store the key in Secret Manager (or use Cloud Run's attached SA).
- [ ] 3. Configure the Chat app (APIs & Services → Google Chat API → Configuration): name, avatar, description; enable "Receive 1:1 messages" + "Join spaces and group conversations".
- [ ] 4. Connection settings: **HTTP endpoint URL** = `https://<cloud-run-url>/chat/events`; authentication audience = endpoint URL (or project number).
- [ ] 5. Visibility: restrict to the support team / Google group (Workspace admin may need to allow the app).
- [ ] 6. Create the **special Chat space** (e.g. "ABAP Triage"), add the app; record the space id `spaces/XXXX` in config.
- [ ] 7. FastAPI `/chat/events`: verify the Google-signed bearer JWT (issuer `chat@system.gserviceaccount.com`), handle `MESSAGE`, `ADDED_TO_SPACE`, `CARD_CLICKED`; reply synchronously if < 30 s, otherwise ack and reply async via Chat API.
- [ ] 8. Outbound: `spaces.messages.create` – one **thread per alert** (`thread.threadKey = alert_id`) so conversation per alert stays together; card with category, RCA, recommendation and buttons (`Ask`, `Fix it`, `Approve`, `Reject`).
- [ ] 9. Map chat thread → `alert_id` so free-text questions in a thread resolve the alert automatically.
- [ ] 10. Workspace admin approval if the domain restricts third-party/internal Chat apps.

### Phase 2 success checklist
- [ ] `POST /chat/events` rejects requests without a valid Google token (401).
- [ ] App can be added to the triage space; "hello" in the space gets a reply.
- [ ] Backend can post a card to the space via Chat API (script `python -m app.chat.client --test`).
- [ ] Reply into an existing thread by `threadKey` lands in the same thread.
- [ ] Button click (`CARD_CLICKED`) reaches the backend with the alert id.
- [ ] Async reply works for slow (> 30 s) operations.

---

## 5. Phase 3 – Agentic AI (LangGraph + FastAPI)

### 3.1 Alert pipeline (LangGraph) – xMatters → DB → Google Chat
Nodes: `validate → persist(NEW) → classify → persist(TRIAGED) → post_to_chat → persist(chat thread id)`.
Chat post is deterministic code; if classification fails, still post the raw alert with "classification unavailable".

### 3.2 Classification + RCA + recommendation
`classify` node = one LLM call with **structured output**:
`{category: DEVELOPER|FUNCTIONAL|BASIS, confidence, root_cause, recommendation, fixable_by_agent: bool}`.
Prompt includes runtime error, exception, program/line, job log, and a short rules guide
(e.g. `ZERODIVIDE`, `ASSIGN`/`ITAB_LINE_NOT_FOUND` → Developer; missing master data / customizing → Functional;
`TSV_TNEW_PAGE_ALLOC_FAILED`, `TIME_OUT`, RFC/communication errors, DB space → Basis).
Optionally the node reads the source around the dump line via ADT to improve RCA (nice-to-have, not MVP-blocking).

### 3.3 Chat with the agent
`create_agent` with tools: `get_alert(id)`, `list_open_alerts()`, `get_alert_history(id)`, `request_fix(id, program, variant)`,
`approve_fix(id)`, `reject_fix(id, reason)`, `release_tr(id)`. Conversation memory per Chat thread (LangGraph checkpointer, Postgres).
`request_fix` only allowed if category = DEVELOPER; asks for program + variant if missing.

### 3.4 Fix sub-agent (SAP ADT)
Runs as a background task; status `FIX_IN_PROGRESS`.
1. Reproduce **before**: run `ZTRIAGE_RUN` with program + variant → capture dump/output.
2. Read source (`abap_object_read_file`), locate faulty line, create/assign TR (`abap_transport_create_request`).
3. Edit (`abap_object_edit_file`), syntax check (`abap_object_check`), activate (`abap_object_activate`), ATC (`abap_atc_run_*`).
4. Reproduce **after** with the same variant → must succeed.
5. Produce **test document** (Markdown → PDF/HTML) containing: alert summary, RCA, code diff (`abap_transport_get_unified_diff`),
   before/after outputs rendered as images ("screenshots"), ATC result, TR number.
6. Post to the alert's Chat thread with the document link (GCS signed URL); status `FIX_READY`; copy changed source to `ABAP/`.
Guardrails: only objects in allowed packages (`Z*`/`ZTRIAGE`), max N edit iterations, never release TR itself.

### 3.5 Review, approval, TR release
User clicks **Approve** (or says "approve") → status `APPROVED` (user id recorded) → `release_tr` releases the TR →
status `RELEASED` → message in thread. **Reject** → `REJECTED` (optionally re-trigger fix with feedback).
Import into UAT is done by the normal TMS import (out of MVP scope unless auto-import is configured).

### Phase 3 success checklist
- [ ] Unit tests (pytest) for DB repo, status transitions (illegal transitions rejected), payload validation, idempotency.
- [ ] `POST /webhooks/xmatters` with a sample payload → row in DB, status `TRIAGED`, card in Chat space within 60 s.
- [ ] Same payload posted twice → one alert, one Chat message.
- [ ] Bad token → 401; malformed payload → 422; LLM down → raw alert still posted.
- [ ] Classification eval: the 3 demo dumps + ~10 synthetic payloads → correct category ≥ 90 %; results tracked as a LangSmith dataset.
- [ ] In Chat thread: "what is the root cause?" → answer grounded in stored alert data; "list open alerts" works.
- [ ] "Fix it" on a Functional/Basis alert is refused with an explanation.
- [ ] "Fix it" on `ZBUG_DIV0` → sub-agent fixes `ZTRIAGE_DEMO_BUG` in DEV, before = dump, after = success, ATC clean,
      test doc + RCA + diff posted to the thread, status `FIX_READY`, TR number recorded.
- [ ] `release_tr` before approval is refused; after **Approve** the TR is released (visible in SE09) and status `RELEASED`.
- [ ] Every step visible as a trace in LangSmith; every status change in `alert_events`.
- [ ] **End-to-end test:** schedule `ZBUG_DIV0` job → monitor → xMatters → Chat card → fix → approve → TR released, with no manual step other than the Chat actions.

---

## 6. Risks / gaps to resolve early

1. **TR release via ADT MCP** – the available ADT MCP tools create/read transports and diffs but have **no explicit "release" tool**.
   Options: `abap_object_run_action` if it exposes release, the ADT transport REST endpoint directly, or an RFC (`TR_RELEASE_REQUEST`) wrapper. Verify in Phase 0.
2. **Screenshots** – a cloud agent can't take SAP GUI screenshots. MVP: render before/after program output and dump text as images in the test doc.
   Real GUI screenshots (SAP GUI scripting / Playwright on Fiori) = later phase.
3. **Network GCP → SAP DEV** – the fix sub-agent needs ADT access to on-prem SAP (VPN / Interconnect / SAP Cloud Connector / a VM in the corporate network).
   Fallback for MVP: run the fix worker on an on-prem host that polls the DB/queue.
4. **SAP → xMatters outbound** – needs firewall/proxy opening and SSL certificate in STRUST.
5. **Cloud Run background work** – use min-instances = 1 + "CPU always allocated", or Cloud Tasks for the fix job (fix runs can take minutes).
6. **Safety** – agent restricted to DEV, allowed packages, and never releases without a recorded human approval.

## 7. Open questions
1. LLM provider: Claude via Anthropic API / Vertex AI, or Gemini on Vertex? (affects `langchain-*` package and secrets)
2. Database: Cloud SQL Postgres (recommended) or Firestore?
3. Is "release TR" enough, or should the agent also trigger the UAT import?
4. Which SAP package / TR naming to use, and is creating objects under `$TMP` acceptable for early testing?
5. Who is allowed to approve (any member of the Chat space, or a specific group)?

## 8. Build order
Phase 0 → 1.1 → 1.3 → 1.2 (test with local FastAPI mock) → 3.1 with a fake Chat client → Phase 2 → 3.2 → 3.3 → 3.4 → 3.5 → E2E.
(Phase 3.1 can be built before Phase 2 by stubbing the Chat client; Phase 2 then just swaps in the real one.)
