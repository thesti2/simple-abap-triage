"""Slack Block Kit layouts (pure functions - unit tested)."""

from __future__ import annotations

from typing import Any

ACTION_FIX = "triage_fix"
ACTION_APPROVE = "triage_approve"
ACTION_REJECT = "triage_reject"

_CATEGORY = {
    "DEVELOPER": ":hammer_and_wrench: Developer (program bug)",
    "FUNCTIONAL": ":clipboard: Functional (data issue)",
    "BASIS": ":gear: Basis (system / connection)",
}


def _trim(text: str | None, limit: int = 2900) -> str:
    text = (text or "-").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def alert_fallback_text(a: dict[str, Any]) -> str:
    """Plain text for notifications / clients without blocks."""
    cat = a.get("category") or "UNCLASSIFIED"
    return f"[{cat}] {a.get('job_name')} cancelled: {a.get('runtime_error') or a.get('error_message') or 'see details'}"


def alert_blocks(a: dict[str, Any]) -> list[dict[str, Any]]:
    cat = a.get("category")
    conf = a.get("confidence")
    cat_text = _CATEGORY.get(cat or "", ":grey_question: Not classified")
    if conf is not None:
        cat_text += f"  ·  confidence {conf:.0%}"
    if a.get("classification_source") == "rules":
        cat_text += "  ·  _rule-based (AI unavailable)_"

    error = a.get("runtime_error") or a.get("error_message") or "-"
    p = a.get("payload") or {}
    where = ""
    if p.get("error_program"):
        where = f" in `{p.get('error_program')}` line {p.get('error_line') or '?'}"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _trim(f"SAP job cancelled: {a.get('job_name')}", 150)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(a.get("summary") or alert_fallback_text(a))}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Handled by*\n{cat_text}"},
                {"type": "mrkdwn", "text": f"*Error*\n`{_trim(error, 200)}`{where}"},
                {"type": "mrkdwn", "text": f"*Program / variant*\n`{a.get('program') or '-'}` / `{a.get('variant') or '-'}`"},
                {"type": "mrkdwn", "text": f"*System / job*\n{a.get('system') or '-'} · {p.get('job_count') or '-'} · {p.get('end_time') or '-'}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause*\n{_trim(a.get('root_cause'))}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Recommendation*\n{_trim(a.get('recommendation'))}"}},
    ]
    if a.get("fixable_by_agent"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ACTION_FIX,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Fix it"},
                        "value": a["alert_id"],
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Start the fix agent?"},
                            "text": {"type": "mrkdwn", "text": f"The agent will reproduce and fix `{a.get('program')}` in DEV."},
                            "confirm": {"type": "plain_text", "text": "Fix it"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    }
                ],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Alert `{a['alert_id']}` · status *{a.get('status')}* · ask me in this thread: `@abap-triage …`"}],
        }
    )
    return blocks


def approval_blocks(alert_id: str, text: str) -> list[dict[str, Any]]:
    """Message after a fix is ready: Approve (release TR) / Reject."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(text)}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": ACTION_APPROVE, "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve and release TR"}, "value": alert_id},
                {"type": "button", "action_id": ACTION_REJECT, "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"}, "value": alert_id},
            ],
        },
    ]
