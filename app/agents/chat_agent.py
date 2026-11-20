"""Chat agent (Phase 3.3): answers questions about alerts in Slack threads and triggers actions.

`create_agent` + DB tools. Memory per Slack thread via a LangGraph checkpointer (thread_id = "<channel>:<thread_ts>").
All state changes go through app.actions (same rules as the buttons). The agent never releases a TR.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, tool

from app import actions
from app.db import AlertNotFound, Repository

SYSTEM_PROMPT = """You are *abap-triage*, the assistant of an SAP ABAP support team in Slack.
Alerts are cancelled SAP background jobs. Each alert was classified as DEVELOPER (program bug), FUNCTIONAL
(data issue) or BASIS (system/connection issue) with a root cause and recommendation.

Rules:
- Answer only from tool results. Always call a tool to get alert data - never guess fields, statuses or numbers.
- {alert_context}
- Status lifecycle: NEW -> TRIAGED -> FIX_REQUESTED -> FIX_IN_PROGRESS -> FIX_READY -> APPROVED -> RELEASED
  (also FIX_FAILED, REJECTED, CLOSED).
- request_fix: only when the user clearly asks to fix the alert. Only DEVELOPER alerts can be fixed by the fix agent;
  it needs program + variant (default: the ones from the alert). Explain refusals from the tool to the user.
- approve_fix / reject_fix: ONLY when the user explicitly says to approve / reject (e.g. "approve", "release it",
  "reject"). Approval is only possible when the fix is FIX_READY. Never approve on your own initiative.
- You cannot release transports yourself; after approval the fix worker releases the TR automatically.
- Reply in Slack mrkdwn: short, concrete, use *bold* and `code` for program/variant/error names. No headings."""


@dataclass
class ChatContext:
    user_id: str
    channel: str
    thread: str | None
    alert_id: str | None  # alert of the current thread, if any

    @property
    def thread_id(self) -> str:
        return f"{self.channel}:{self.thread or 'main'}"


def _alert_json(repo: Repository, alert_id: str, with_joblog: bool = True) -> str:
    a = repo.get(alert_id)
    p = a.payload or {}
    data = {
        "alert_id": a.alert_id, "status": a.status, "category": a.category, "confidence": a.confidence,
        "summary": a.summary, "root_cause": a.root_cause, "recommendation": a.recommendation,
        "fixable_by_agent": a.fixable_by_agent, "classification_source": a.classification_source,
        "system": a.system, "job_name": a.job_name, "job_count": p.get("job_count"),
        "start_time": p.get("start_time"), "end_time": p.get("end_time"),
        "program": a.program, "variant": a.variant, "error_type": a.error_type, "runtime_error": a.runtime_error,
        "error_program": p.get("error_program"), "error_line": p.get("error_line"), "error_message": a.error_message,
        "fix_tr": a.fix_tr, "approved_by": a.approved_by,
    }
    if with_joblog:
        data["joblog"] = (p.get("joblog") or [])[-30:]
    return json.dumps(data, default=str)


def build_tools(repo: Repository, ctx: ChatContext, on_change: Callable[[str], None] | None = None) -> list[BaseTool]:
    """Tools bound to the current Slack user/thread. on_change(alert_id) is called after a status change."""

    def _resolve(alert_id: str | None) -> str | None:
        return (alert_id or "").strip() or ctx.alert_id

    def _changed(result: actions.ActionResult, alert_id: str) -> str:
        if result.ok and on_change:
            on_change(alert_id)
        return json.dumps({"ok": result.ok, "message": result.message})

    @tool
    def get_alert(alert_id: str = "") -> str:
        """Get full details of an alert (status, category, root cause, recommendation, error, job log).
        Leave alert_id empty to use the alert of the current thread."""
        aid = _resolve(alert_id)
        if not aid:
            return "No alert in this thread - ask the user for an alert id or use list_alerts."
        try:
            return _alert_json(repo, aid)
        except AlertNotFound:
            return f"Alert {aid} not found."

    @tool
    def list_alerts(status: str = "", limit: int = 10) -> str:
        """List the latest alerts, optionally filtered by status (e.g. TRIAGED, FIX_REQUESTED, FIX_READY).
        Use status 'OPEN' for all alerts that are not RELEASED or CLOSED."""
        st = status.strip().upper()
        if st == "OPEN":
            statuses = ["NEW", "TRIAGED", "FIX_REQUESTED", "FIX_IN_PROGRESS", "FIX_READY", "FIX_FAILED", "APPROVED", "REJECTED"]
        else:
            statuses = [st] if st else None
        rows = repo.list_alerts(statuses=statuses, limit=max(1, min(limit, 50)))
        return json.dumps([{"alert_id": a.alert_id, "status": a.status, "category": a.category, "job_name": a.job_name,
                            "program": a.program, "error": a.runtime_error or a.error_message,
                            "received_at": a.received_at} for a in rows], default=str)

    @tool
    def get_alert_history(alert_id: str = "") -> str:
        """Status history of an alert (who changed what and when). Empty alert_id = current thread's alert."""
        aid = _resolve(alert_id)
        if not aid:
            return "No alert in this thread."
        return json.dumps([{"at": e.at, "actor": e.actor, "from": e.from_status, "to": e.to_status, "note": e.note}
                           for e in repo.history(aid)], default=str)

    @tool
    def request_fix(alert_id: str = "", program: str = "", variant: str = "") -> str:
        """Ask the fix agent to reproduce and fix a DEVELOPER alert in DEV. Only call when the user asks for a fix.
        program/variant default to the alert's values."""
        aid = _resolve(alert_id)
        if not aid:
            return "No alert in this thread - ask the user which alert to fix."
        return _changed(actions.request_fix(repo, aid, ctx.user_id, program or None, variant or None, note="via chat"), aid)

    @tool
    def approve_fix(alert_id: str = "") -> str:
        """Approve a FIX_READY fix so the TR gets released. Only call when the user explicitly approves."""
        aid = _resolve(alert_id)
        if not aid:
            return "No alert in this thread."
        return _changed(actions.approve_fix(repo, aid, ctx.user_id), aid)

    @tool
    def reject_fix(alert_id: str = "", reason: str = "") -> str:
        """Reject a FIX_READY fix. Only call when the user explicitly rejects; pass their reason."""
        aid = _resolve(alert_id)
        if not aid:
            return "No alert in this thread."
        return _changed(actions.reject_fix(repo, aid, ctx.user_id, reason), aid)

    return [get_alert, list_alerts, get_alert_history, request_fix, approve_fix, reject_fix]


class ChatAgent:
    def __init__(self, llm: BaseChatModel, repo: Repository, checkpointer: Any) -> None:
        self.llm, self.repo, self.checkpointer = llm, repo, checkpointer

    def answer(self, ctx: ChatContext, text: str, on_change: Callable[[str], None] | None = None) -> str:
        alert_context = (f"This conversation is the Slack thread of alert `{ctx.alert_id}`; tools default to it."
                         if ctx.alert_id else "This message is not in an alert thread; use list_alerts or ask for an id.")
        agent = create_agent(
            self.llm,
            build_tools(self.repo, ctx, on_change),
            system_prompt=SYSTEM_PROMPT.format(alert_context=alert_context),
            checkpointer=self.checkpointer,
            name="chat_agent",
        )
        result = agent.invoke(
            {"messages": [HumanMessage(f"<@{ctx.user_id}>: {text}")]},
            config={"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 12,
                    "run_name": "chat_agent", "metadata": {"alert_id": ctx.alert_id, "slack_user": ctx.user_id}},
        )
        last = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage) and m.content), None)
        return str(last.content) if last else "Sorry, I have no answer."


def make_checkpointer(database_url: str) -> Any:
    """Chat memory store: SQLite file next to the local DB, or the same Postgres database (Cloud SQL)."""
    if database_url.startswith("sqlite:///"):
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        path = database_url.removeprefix("sqlite:///")
        conn = sqlite3.connect(path.replace(".db", "_chat.db") if path.endswith(".db") else path + "_chat",
                               check_same_thread=False)
        return SqliteSaver(conn)
    if database_url.startswith("postgresql"):
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(pg_conninfo(database_url), min_size=1, max_size=5, open=True,
                              check=ConnectionPool.check_connection,
                              kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(pool)  # type: ignore[arg-type]
        saver.setup()  # creates/migrates the checkpoint tables (idempotent)
        return saver
    raise ValueError(f"unsupported database URL scheme for chat memory: {database_url.split(':', 1)[0]}")


def pg_conninfo(database_url: str) -> str:
    """SQLAlchemy URL (postgresql+psycopg://...) -> libpq URI for psycopg."""
    from sqlalchemy.engine import make_url

    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
