# SAP → xMatters → Triage backend: alert payload

Sent by `ZTRIAGE_JOB_MONITOR` (class `ZCL_TRIAGE_JOB_MONITOR`) as `POST` with `Content-Type: application/json`
to SM59 destination `ZTRIAGE_XMATTERS` (xMatters inbound HTTP trigger). One request per cancelled job.
The xMatters flow should forward the same JSON (unchanged) to the backend webhook `POST /webhooks/xmatters`.

All fields are strings except `joblog` (array of strings). Flat structure so xMatters can map properties directly.

| Field | Example | Notes |
|---|---|---|
| `alert_id` | `XYZ-100-ZTRIAGE_TEST_DIV0-22245801` | `<SID>-<client>-<jobname>-<jobcount>`; unique, idempotency key |
| `source` | `SAP` | |
| `system` / `client` | `XYZ` / `100` | |
| `job_name` / `job_count` | `ZTRIAGE_TEST_DIV0` / `22245801` | |
| `job_status` | `CANCELLED` | |
| `job_user` | `JOBUSER` | step user (authorization user) |
| `scheduled_by` | `JOBUSER` | |
| `start_time` / `end_time` | `2026-11-18T22:24:58` | system local time |
| `timezone` | `UTC+8` | system time zone (TTZCU) |
| `program` / `variant` | `ZTRIAGE_DEMO_BUG` / `ZBUG_DIV0` | the step that failed (used by the fix agent to reproduce) |
| `error_type` | `DUMP` | `DUMP` (short dump found), `MESSAGE` (job cancelled by error message), `UNKNOWN` |
| `runtime_error` | `COMPUTE_INT_ZERODIVIDE` | empty unless `DUMP` |
| `error_program` / `error_include` / `error_line` | `ZTRIAGE_DEMO_BUG` / `ZTRIAGE_DEMO_BUG` / `55` | dump location |
| `error_message` | `Internal session terminated with a runtime error ...` | first meaningful E/A/X job log message |
| `joblog_text` | multi-line string | job log joined with `\n` (for display) |
| `joblog` | `["2026-11-18 22:24:58 S 00516 Job started", ...]` | job log lines: `date time type msgid+no text` |

## Real sample (captured from the DEV system 2026-11-18, system id / client / user anonymised)
```json
{
  "alert_id": "XYZ-100-ZTRIAGE_TEST_DIV0-22245801",
  "source": "SAP",
  "system": "XYZ",
  "client": "100",
  "job_name": "ZTRIAGE_TEST_DIV0",
  "job_count": "22245801",
  "job_status": "CANCELLED",
  "job_user": "JOBUSER",
  "scheduled_by": "JOBUSER",
  "start_time": "2026-11-18T22:24:58",
  "end_time": "2026-11-18T22:24:59",
  "timezone": "UTC+8",
  "program": "ZTRIAGE_DEMO_BUG",
  "variant": "ZBUG_DIV0",
  "error_type": "DUMP",
  "runtime_error": "COMPUTE_INT_ZERODIVIDE",
  "error_program": "ZTRIAGE_DEMO_BUG",
  "error_include": "ZTRIAGE_DEMO_BUG",
  "error_line": "55",
  "error_message": "Internal session terminated with a runtime error COMPUTE_INT_ZERODIVIDE (see ST22)",
  "joblog_text": "2026-11-18 22:24:58 S 00516 Job started\n2026-11-18 22:24:59 S 00550 Step 001 started (program ZTRIAGE_DEMO_BUG, variant ZBUG_DIV0, user ID JOBUSER)\n2026-11-18 22:24:59 A 00671 Internal session terminated with a runtime error COMPUTE_INT_ZERODIVIDE (see ST22)\n2026-11-18 22:24:59 A 00518 Job canceled",
  "joblog": [
    "2026-11-18 22:24:58 S 00516 Job started",
    "2026-11-18 22:24:59 S 00550 Step 001 started (program ZTRIAGE_DEMO_BUG, variant ZBUG_DIV0, user ID JOBUSER)",
    "2026-11-18 22:24:59 A 00671 Internal session terminated with a runtime error COMPUTE_INT_ZERODIVIDE (see ST22)",
    "2026-11-18 22:24:59 A 00518 Job canceled"
  ]
}
```
