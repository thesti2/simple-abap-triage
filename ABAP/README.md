# ABAP objects (copies of SAP DEV `<SAP_DEV_SID>` client `<SAP_CLIENT>`)

Package `<SAP_PACKAGE>`, transport request **<PHASE1_TR>** ("ABAP Triage MVP - Phase 1 objects").
Files are verbatim copies of the active sources in SAP (abapGit-style names).

| Object | Type | File | Purpose |
|---|---|---|---|
| `ZTRIAGE_DEMO_BUG` | Program | `ztriage_demo_bug.prog.abap` | Demo program with intentional errors (modes C / M / R) |
| `ZTRIAGE_JOB_MONITOR` | Program | `ztriage_job_monitor.prog.abap` | Report to schedule in SM36: sends cancelled jobs to xMatters |
| `ZCL_TRIAGE_JOB_MONITOR` | Class | `zcl_triage_job_monitor.clas*.abap` | Collect cancelled jobs + job log + dump, build JSON, HTTP POST via SM59 `ZTRIAGE_XMATTERS` |
| `ZCL_TRIAGE_RUNNER` | Class | `zcl_triage_runner.clas*.abap` | Run program + variant as background job, return status / job log / spool / dump (reproduce tool for the fix agent) |
| `ZTRIAGE` / `Z_TRIAGE_TR_RELEASE` | Function group / RFC function module | `ztriage.fugr.z_triage_tr_release.func.abap` | Release a TR (tasks first, then request). Release only, no import. |

## Variants of `ZTRIAGE_DEMO_BUG`
Created by the `ZCL_TRIAGE_RUNNER` unit test `class_setup`:

| Variant | Parameters | Result | Expected triage category |
|---|---|---|---|
| `ZOK` | mode C, total 1000, qty 10 | Finished, unit price 100 | – |
| `ZBUG_DIV0` | mode C, total 1000, qty 0 | Dump `COMPUTE_INT_ZERODIVIDE`, line 55 | Developer |
| `ZBUG_DATA` | mode M, material `ZTRIAGE_NO_MAT` | Error message "Material ... does not exist", job cancelled | Functional |
| `ZBUG_RFC` | mode R, destination `ZTRIAGE_DUMMY` | Dump `CALL_FUNCTION_NO_DEST` | Basis |

## How tests are executed
The ADT MCP has no "run program" action, so code is executed through **ABAP Unit**:
- `ZCL_TRIAGE_RUNNER` tests: create the variants, run the 4 variants as real background jobs
  (job names `ZTRIAGE_TEST_*`), check status / dump / variant name; plus 3 safe `Z_TRIAGE_TR_RELEASE` cases.
- `ZCL_TRIAGE_JOB_MONITOR` tests: collect-only (no HTTP), payload content, and the report in test mode.
  Run the runner tests first (they create the `ZTRIAGE_TEST_*` jobs).

Details of each test are reported as *tolerable* messages (hidden when the run passes).

## Job monitor notes
- Sent jobs are remembered in cluster table `INDX` (`RELID ZT`, `ID ZTRIAGE_SENT`), 30 days, so each job is sent once;
  failed sends are retried on the next run.
- Unit tests `ltc_dedup` check both (second run sends nothing; wrong destination → retried next run) using test
  seams `sent_store` (separate INDX key `ZTRIAGE_SENT_UT`) and `http_send` (injected HTTP 202) - nothing reaches xMatters.
- Jobs `ZTRIAGE_RUN_*` (fix-agent reproduce runs) and `ZTRIAGE_JOB_MONITOR` itself are never reported.
- Payload contract: `docs/xmatters-contract.md`.
