"""Payload and triage result models. Alert payload contract: docs/xmatters-contract.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["DEVELOPER", "FUNCTIONAL", "BASIS"]


class AlertPayload(BaseModel):
    """Flat JSON sent by ZTRIAGE_JOB_MONITOR via xMatters."""

    model_config = ConfigDict(extra="allow")  # tolerate extra fields added by xMatters

    alert_id: str = Field(min_length=1)
    source: str = "SAP"
    system: str = ""
    client: str = ""
    job_name: str = ""
    job_count: str = ""
    job_status: str = ""
    job_user: str = ""
    scheduled_by: str = ""
    start_time: str = ""
    end_time: str = ""
    timezone: str = ""
    program: str = ""
    variant: str = ""
    error_type: str = ""  # DUMP | MESSAGE | UNKNOWN
    runtime_error: str = ""
    error_program: str = ""
    error_include: str = ""
    error_line: str = ""
    error_message: str = ""
    joblog_text: str = ""
    joblog: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """Structured output of the classifier."""

    category: Category = Field(
        description="DEVELOPER = program bug in custom code; FUNCTIONAL = master/transaction data or customizing; "
        "BASIS = system, resources, connections, authorizations, infrastructure"
    )
    confidence: float = Field(ge=0, le=1, description="0..1")
    summary: str = Field(description="One-line summary of what happened, for the alert title")
    root_cause: str = Field(description="Most likely root cause, 1-3 sentences, specific to this dump/job")
    recommendation: str = Field(description="Concrete next steps to fix, short bullet-style sentences")
    fixable_by_agent: bool = Field(
        description="True only if DEVELOPER and the fix is a code change in a custom (Z/Y) program"
    )
