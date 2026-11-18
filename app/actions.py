"""Alert actions shared by Slack buttons and the chat agent's tools. All rules live here."""

from __future__ import annotations

from dataclasses import dataclass

from app.db import AlertNotFound, Repository
from app.status import IllegalTransition, Status


@dataclass
class ActionResult:
    ok: bool
    message: str  # Slack mrkdwn, ready to post in the thread


def request_fix(repo: Repository, alert_id: str, user_id: str, program: str | None = None,
                variant: str | None = None, note: str = "") -> ActionResult:
    try:
        alert = repo.get(alert_id)
    except AlertNotFound:
        return ActionResult(False, f":warning: Alert `{alert_id}` not found.")
    if not alert.fixable_by_agent:
        return ActionResult(False, f":no_entry: Alert `{alert_id}` is *{alert.category}* - the fix agent only handles "
                                   "program bugs in custom code.")
    program = (program or alert.program or "").strip().upper()
    variant = (variant or alert.variant or "").strip().upper()
    if not program or not variant:
        return ActionResult(False, ":question: I need the program name and the variant to reproduce the issue.")
    try:
        repo.transition(alert_id, Status.FIX_REQUESTED, actor=f"slack:{user_id}",
                        note=f"fix requested for {program}/{variant}. {note}".strip(), program=program, variant=variant)
    except IllegalTransition:
        return ActionResult(False, f":information_source: Not possible now - alert `{alert_id}` is *{alert.status}*.")
    return ActionResult(True, f":hammer_and_wrench: <@{user_id}> requested a fix. The fix agent will reproduce "
                              f"`{program}` / `{variant}` in DEV and report back here.")


def approve_fix(repo: Repository, alert_id: str, user_id: str) -> ActionResult:
    try:
        alert = repo.get(alert_id)
        repo.transition(alert_id, Status.APPROVED, actor=f"slack:{user_id}", note="fix approved", approved_by=user_id)
    except AlertNotFound:
        return ActionResult(False, f":warning: Alert `{alert_id}` not found.")
    except IllegalTransition:
        return ActionResult(False, f":information_source: Not possible now - alert `{alert_id}` is *{alert.status}*; "
                                   "a fix can only be approved when it is *FIX_READY*.")
    return ActionResult(True, f":white_check_mark: <@{user_id}> approved the fix. The TR will be released and reported here.")


def reject_fix(repo: Repository, alert_id: str, user_id: str, reason: str = "") -> ActionResult:
    try:
        alert = repo.get(alert_id)
        repo.transition(alert_id, Status.REJECTED, actor=f"slack:{user_id}", note=f"fix rejected. {reason}".strip())
    except AlertNotFound:
        return ActionResult(False, f":warning: Alert `{alert_id}` not found.")
    except IllegalTransition:
        return ActionResult(False, f":information_source: Not possible now - alert `{alert_id}` is *{alert.status}*.")
    suffix = f" Reason: {reason}" if reason else " Tell me in this thread what should change."
    return ActionResult(True, f":x: <@{user_id}> rejected the fix.{suffix}")
