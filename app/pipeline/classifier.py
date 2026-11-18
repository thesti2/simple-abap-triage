"""Triage classifier: one structured-output LLM call, with a deterministic rule-based fallback."""

from __future__ import annotations

from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.schemas import AlertPayload, TriageResult

SYSTEM_PROMPT = """You are an SAP ABAP support triage expert. A background job was cancelled in an SAP system.
Decide which team must handle it, explain the root cause and recommend a fix.

Routing rules (follow them):
- DEVELOPER: program bug in custom code (Z*/Y* programs, classes, function modules). Typical runtime errors:
  COMPUTE_*_ZERODIVIDE, CONVT_NO_NUMBER, CONVT_OVERFLOW, ITAB_LINE_NOT_FOUND, GETWA_NOT_ASSIGNED,
  OBJECTS_OBJREF_NOT_ASSIGNED, UNCAUGHT_EXCEPTION / CX_* from custom code, MESSAGE_TYPE_X raised by custom code,
  SAPSQL_* syntax/logic errors in custom code, missing input validation (e.g. a divisor that can be 0).
- FUNCTIONAL: the program is fine but data or configuration is wrong or missing: material/customer/vendor master
  missing, wrong customizing, period not open, invalid variant values, job cancelled by an error MESSAGE about data.
- BASIS: system, resources, connectivity or authorization: TSV_TNEW_PAGE_ALLOC_FAILED, MEMORY_*, TIME_OUT,
  DBSQL_* / DBIF_* database errors, tablespace full, CALL_FUNCTION_NO_DEST, CALL_FUNCTION_*_FAILURE,
  COMMUNICATION_FAILURE, SYSTEM_FAILURE, RFC destination missing/unreachable, certificate/SSL, missing authorization,
  work process / spool / lock table issues.
- The location of the dump alone does not decide: an RFC destination error in a Z program is still BASIS,
  a missing master-data record is FUNCTIONAL even when the message comes from a Z program.
- fixable_by_agent = true only for DEVELOPER issues fixable by changing the custom program's code.
Be specific to the given program, line and error. Keep root_cause and recommendation short and practical."""


def build_prompt(alert: AlertPayload) -> str:
    joblog = "\n".join(alert.joblog[-30:]) or alert.joblog_text
    return (
        f"System: {alert.system} client {alert.client}\n"
        f"Job: {alert.job_name} ({alert.job_count}), user {alert.job_user}, {alert.start_time} - {alert.end_time}\n"
        f"Program: {alert.program}  Variant: {alert.variant}\n"
        f"Error type: {alert.error_type}\n"
        f"Runtime error: {alert.runtime_error or '-'}\n"
        f"Dump location: program {alert.error_program or '-'}, include {alert.error_include or '-'}, "
        f"line {alert.error_line or '-'}\n"
        f"Error message: {alert.error_message or '-'}\n"
        f"Job log:\n{joblog or '-'}"
    )


class Classifier(Protocol):
    def __call__(self, alert: AlertPayload) -> tuple[TriageResult, str]: ...


class LLMClassifier:
    """Returns (result, source) where source = 'llm' or 'rules' (fallback when the LLM fails)."""

    def __init__(self, llm: BaseChatModel, method: str = "json_schema") -> None:
        self._structured = llm.with_structured_output(TriageResult, method=method)

    def __call__(self, alert: AlertPayload) -> tuple[TriageResult, str]:
        try:
            result = self._structured.invoke(
                [SystemMessage(SYSTEM_PROMPT), HumanMessage(build_prompt(alert))],
                config={"run_name": "triage_classify", "metadata": {"alert_id": alert.alert_id}},
            )
            if not isinstance(result, TriageResult):
                result = TriageResult.model_validate(result)
            if result.category != "DEVELOPER":
                result.fixable_by_agent = False
            return result, "llm"
        except Exception:  # noqa: BLE001 - never lose an alert because the LLM failed
            return rule_based(alert), "rules"


# ---------------------------------------------------------------- deterministic fallback
_BASIS = ("TSV_", "MEMORY_", "TIME_OUT", "DBSQL_", "DBIF_", "CALL_FUNCTION_NO_DEST", "CALL_FUNCTION_REMOTE_ERROR",
          "CALL_FUNCTION_SYSTEM_FAILURE", "CALL_FUNCTION_COMMUNICATION", "COMMUNICATION_FAILURE", "SYSTEM_FAILURE",
          "RFC_", "SYSTEM_CORE_DUMPED", "SPOOL_")
_DEVELOPER = ("COMPUTE_", "CONVT_", "ITAB_", "GETWA_", "OBJECTS_", "UNCAUGHT_EXCEPTION", "MESSAGE_TYPE_X",
              "SAPSQL_", "ASSIGN_", "STRING_OFFSET", "RAISE_EXCEPTION")


def rule_based(alert: AlertPayload) -> TriageResult:
    err = (alert.runtime_error or "").upper()
    custom = (alert.error_program or alert.program or "").upper().startswith(("Z", "Y"))
    if err.startswith(_BASIS):
        cat, rc = "BASIS", f"Runtime error {err} points to a system/connection/resource problem."
        rec = "Check the RFC destination / system resources in SM59, ST22 and SM21; involve Basis."
    elif err.startswith(_DEVELOPER):
        cat, rc = "DEVELOPER", f"Runtime error {err} at {alert.error_program} line {alert.error_line} indicates a program bug."
        rec = f"Analyse {alert.error_program} around line {alert.error_line} (ST22) and add the missing check/handling."
    elif alert.error_type == "MESSAGE":
        cat, rc = "FUNCTIONAL", f"Job cancelled by an error message: {alert.error_message}"
        rec = "Check the master/transaction data or variant values mentioned in the message."
    else:
        cat, rc = "BASIS", "No dump or error message could be identified."
        rec = "Check job log (SM37), ST22 and SM21 for the job's time window."
    return TriageResult(
        category=cat,  # type: ignore[arg-type]
        confidence=0.4,
        summary=f"{alert.job_name}: {err or alert.error_message or 'job cancelled'}",
        root_cause=rc,
        recommendation=rec,
        fixable_by_agent=cat == "DEVELOPER" and custom,
    )
