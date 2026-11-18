# ABAP Dump Triage – MVP Plan

> Source: `AGENTS.md`. Principle: **keep it simple** – build the smallest thing that runs the full loop
> (dump → xMatters → AI triage → Google Chat → fix → review → TR release), then harden.
>
> **Private values are never written in markdown** (AGENTS.md UPDATE 2). `<PLACEHOLDER>`s refer to keys in the
> git-ignored `.env` (key list: `.env.example`): `<SAP_DEV_SID>`, `<SAP_CLIENT>`, `<SAP_USER>`, `<ADT_DESTINATION>`
> (`SAP_ADT_DESTINATION`), `<SAP_PACKAGE>`, `<PHASE1_TR>` (`SAP_PHASE1_TR`), `<UAT_SID>` (`SAP_UAT_SID`),
> `<GCP_PROJECT_ID>`, `<TF_STATE_BUCKET>`.

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
worker/                    # local fix worker (runs on the workstation with sap-adt MCP, polls Cloud SQL)
infra/terraform/           # ALL GCP services/resources (AGENTS.md UPDATE 1)
tests/
docs/ (xmatters-contract.md, google-chat-setup.md, runbook.md)
```

---

## 2. Phase 0 – Foundations (prerequisites)

*Status checked 2026-11-18.*

- [x] SAP ADT MCP connection to DEV works for reads – destination `<ADT_DESTINATION>` logged on, repository search OK.
- [x] SAP ADT MCP write works (create/activate) – verified in Phase 1. Code execution only via ABAP Unit.
- [x] Package agreed: `<SAP_PACKAGE>` (exists in <SAP_DEV_SID>). No `ZTRIAGE*` objects yet, so no name clashes.
- [x] Workbench TR **<PHASE1_TR>** in `<SAP_PACKAGE>` created for Phase 1 objects.
- [x] SM59 destination `ZTRIAGE_XMATTERS` maintained (per user).
- [x] GCP project `<GCP_PROJECT_ID>` (asia-southeast1), gcloud logged in, Terraform written in `infra/terraform` and validated.
- [ ] **Billing enabled on `<GCP_PROJECT_ID>`** (currently disabled – blocks all resource creation).
- [ ] `terraform apply` (Cloud SQL, Secret Manager, Artifact Registry, Cloud Run).
- [x] LangSmith API key + project set in `.env` (`LANGSMITH_*`) – not yet validated with a trace.
- [x] `.env` has `OPENROUTER_API_KEY` and `MODEL`; `.env` is git-ignored.
- [ ] OpenRouter smoke test: the `MODEL` does tool calling + structured output.
- [x] Network path to SAP: resolved by running the fix worker locally with the `sap-adt` MCP (see Risk 3) – no VPC/VPN needed.

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
*Status 2026-11-18. Tests: `ZCL_TRIAGE_RUNNER` 7/7 passed, `ZCL_TRIAGE_JOB_MONITOR` 7/7 passed.*
- [x] `ZTRIAGE_DEMO_BUG` + all objects activated in DEV; source copies in `ABAP/` (taken from the active version).
- [x] Each variant run as a real background job gives the expected result (3 cancelled, `ZOK` finished); dump name + line found.
- [x] Monitor (collect / test mode) reports the 3 cancelled jobs with correct program, real variant, times, error text, dump; `ZOK` not reported; `ZTRIAGE_RUN_*` excluded.
- [x] Report `ZTRIAGE_JOB_MONITOR` runs as background job in test mode and lists the jobs.
- [x] `ZCL_TRIAGE_RUNNER` returns dump for `ZBUG_DIV0` and normal output (spool "Unit price 100") for `ZOK`.
- [x] `Z_TRIAGE_TR_RELEASE` (RFC-enabled): invalid TR → error, task → error, released TR → "already released" (no dump).
- [x] All objects in package `<SAP_PACKAGE>`, one TR (<PHASE1_TR>); ATC: 0 errors, 1 warning (sequential DELETE on small hashed table), infos only.
- [x] **Real send:** monitor without test mode → alerts received in xMatters (confirmed by user 2026-11-18).
- [ ] Second run sends nothing new (dedup) – re-check when the job is scheduled.
- [ ] Failure to reach xMatters is logged and retried next run (test with a wrong destination name).
- [ ] *(Deferred)* `Z_TRIAGE_TR_RELEASE` releases a real TR – tested in Phase 3.5 instead of a dummy TR.
- [ ] *(Deferred)* Job `ZTRIAGE_JOB_MONITOR` scheduled in SM36 every 5 minutes – after the xMatters flow to the backend is ready.

---

## 4. Phase 2 – Google Chat Integration (TODO list)

**Sending + receiving** requires a **Google Chat app** (an incoming webhook can only send, not receive).

GCP-side items (1–2) are done **in Terraform** (`infra/terraform`). Items 3–5 are Chat app settings that have no
Terraform/API support – set once in the Console and documented in `docs/google-chat-setup.md`.

- [ ] 1. **Terraform:** enable **Google Chat API** (`google_project_service`) – already in `main.tf`.
- [ ] 2. **Terraform:** Cloud Run runtime **service account** posts messages via Chat API (scope `https://www.googleapis.com/auth/chat.bot`) using its attached identity – no key file. Already in `main.tf`.
- [ ] 3. *(Console, manual)* Configure the Chat app (APIs & Services → Google Chat API → Configuration): name, avatar, description; enable "Receive 1:1 messages" + "Join spaces and group conversations".
- [ ] 4. *(Console, manual)* Connection settings: **HTTP endpoint URL** = `https://<cloud-run-url>/chat/events`; authentication audience = endpoint URL (or project number).
- [ ] 5. *(Console, manual)* Visibility: restrict to the support team / Google group (Workspace admin may need to allow the app).
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
Any member of the triage space clicks **Approve** (or says "approve") → status `APPROVED` (user id recorded) → `release_tr`
releases the TR → status `RELEASED` → message in thread "TR <number> released – ready for import to <UAT_SID>".
**Reject** → `REJECTED` (optionally re-trigger fix with feedback).

Release goes through the RFC-enabled wrapper `Z_TRIAGE_TR_RELEASE` (Phase 1.4), called by the local fix worker.
The import into `<UAT_SID>` is **not** done by the agent; it follows the normal TMS/Basis import process.

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
- [ ] If release fails (e.g. object locked, ATC block), status stays `APPROVED` and the error is posted to the thread.
- [ ] Every step visible as a trace in LangSmith; every status change in `alert_events`.
- [ ] Any GCP resource added in Phase 3 (e.g. GCS bucket for test documents + signed-URL IAM) is in Terraform; `terraform plan` shows no changes after apply.
- [ ] **End-to-end test:** schedule `ZBUG_DIV0` job → monitor → xMatters → Chat card → fix → approve → TR released, with no manual step other than the Chat actions.

---

## 6. Risks / gaps to resolve early

1. **TR release** – the ADT MCP tools have **no release tool**, so this goes through the RFC wrapper in 1.4.
   The local fix worker calls it via `pyrfc` or an SAP HTTP wrapper (same workstation as the `sap-adt` MCP, Risk 3).
2. **Screenshots** – a cloud agent can't take SAP GUI screenshots. MVP: render before/after program output and dump text as images in the test doc.
   Real GUI screenshots (SAP GUI scripting / Playwright on Fiori) = later phase.
3. **Network GCP → SAP DEV** – the fix sub-agent needs ADT access to on-prem SAP (VPN / Interconnect / SAP Cloud Connector / a VM in the corporate network).
   **Decision:** the fix worker (fix sub-agent + TR release) runs on the workstation that has the `sap-adt` MCP.
   It polls Cloud SQL (via Cloud SQL Auth Proxy) for `FIX_REQUESTED` alerts and posts results to Chat itself.
   Cloud Run only hosts the webhooks, alert pipeline and chat agent.
4. **SAP → xMatters outbound** – needs firewall/proxy opening and SSL certificate in STRUST.
5. **Cloud Run background work** – use min-instances = 1 + "CPU always allocated", or Cloud Tasks for the fix job (fix runs can take minutes).
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
5. **Approval: anyone in the triage Chat space** for now (approver's Chat user id is still recorded on the alert).

6. **TR:** create a new workbench TR in `<SAP_PACKAGE>` for the Phase 1 objects.
7. **UAT target system: `<UAT_SID>`** – shown in the Chat message after release; the import itself is not automated.
8. **SM59 destination `ZTRIAGE_XMATTERS`** is already maintained (xMatters URL + auth) – `ZTRIAGE_JOB_MONITOR` uses it as-is.
9. **Terraform for all GCP services** (AGENTS.md UPDATE 1). Code in `infra/terraform`, state in `gs://<TF_STATE_BUCKET>`.
   Every new GCP need in later phases (APIs, IAM, buckets e.g. for test documents, Cloud Run settings/env vars)
   is added to Terraform, not created with `gcloud`/Console. Only exceptions: the state bucket (bootstrap),
   secret *values* (kept out of state), and Chat app Console settings (no API). Container image builds/deploys
   (`gcloud run deploy` / Cloud Build) update the image only; Terraform ignores image changes.
   Project `<GCP_PROJECT_ID>`, region `asia-southeast1`. Project id and state bucket live only in git-ignored
   `terraform.tfvars` / `backend.hcl` (committed: `*.example`).
10. **No private information in markdown files** (AGENTS.md UPDATE 2) – TR numbers, GCP project id, SAP system id /
    client / user, package, bucket names, URLs with keys. Use `<PLACEHOLDER>`s (legend at the top); real values only
    in git-ignored files (`.env`, `terraform.tfvars`, `backend.hcl`). Sample payloads are anonymised.
    Also applies to committed code where avoidable (e.g. no hard-coded TR numbers in ABAP tests).

## 8. Build order
Phase 0 → 1.1 → 1.3 → 1.2 (test against xMatters via `ZTRIAGE_XMATTERS`) → 1.4 → 3.1 with a fake Chat client → Phase 2 → 3.2 → 3.3 → 3.4 → 3.5 → E2E.
(Phase 3.1 can be built before Phase 2 by stubbing the Chat client; Phase 2 then just swaps in the real one.)
