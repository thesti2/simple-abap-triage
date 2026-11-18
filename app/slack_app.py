"""Slack Bolt app: button clicks + mentions. Works in Socket Mode (local) and HTTP mode (Cloud Run)."""

from __future__ import annotations

import logging
import re
from typing import Any

from slack_bolt import App

from app import actions
from app.chat.base import ThreadRef
from app.chat.slack_blocks import ACTION_APPROVE, ACTION_FIX, ACTION_REJECT
from app.db import AlertNotFound, Repository

log = logging.getLogger(__name__)


def handle_action(repo: Repository, action_id: str, alert_id: str, user_id: str) -> str:
    """Apply a button action to the alert. Returns the message to post in the thread (rules in app.actions)."""
    if action_id == ACTION_FIX:
        return actions.request_fix(repo, alert_id, user_id, note="via button").message
    if action_id == ACTION_APPROVE:
        return actions.approve_fix(repo, alert_id, user_id).message
    if action_id == ACTION_REJECT:
        return actions.reject_fix(repo, alert_id, user_id).message
    return f"Unknown action {action_id}"


def status_text(repo: Repository, channel: str, thread: str | None) -> str:
    """Deterministic answer to a mention until the chat agent (Phase 3.3) exists."""
    alert = repo.find_by_thread(channel, thread) if thread else None
    if alert is None:
        open_alerts = repo.list_alerts(limit=5)
        if not open_alerts:
            return "No alerts yet."
        lines = [f"• `{a.alert_id}` - {a.category or '?'} - *{a.status}*" for a in open_alerts]
        return "Latest alerts (mention me inside an alert thread for details):\n" + "\n".join(lines)
    return (f"Alert `{alert.alert_id}` is *{alert.status}* ({alert.category}).\n"
            f"*Root cause:* {alert.root_cause}\n*Recommendation:* {alert.recommendation}")


_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


def answer_mention(repo: Repository, chat: Any, chat_agent: Any, event: dict[str, Any]) -> str:
    """Resolve the thread's alert and let the chat agent answer (fallback: deterministic status text)."""
    channel, thread = event["channel"], event.get("thread_ts")
    alert = repo.find_by_thread(channel, thread) if thread else None
    text = _MENTION.sub("", event.get("text", "")).strip() or "status?"
    if chat_agent is None:
        return status_text(repo, channel, thread)

    def on_change(alert_id: str) -> None:  # refresh the card after a status change from chat
        try:
            a = repo.get(alert_id)
            if a.chat_channel and a.chat_thread and hasattr(chat, "update_alert"):
                chat.update_alert(ThreadRef(a.chat_channel, a.chat_thread), a.to_dict())
        except Exception:  # noqa: BLE001 - card refresh is cosmetic
            log.exception("card refresh failed for %s", alert_id)

    from app.agents.chat_agent import ChatContext

    ctx = ChatContext(user_id=event.get("user", "unknown"), channel=channel, thread=thread,
                      alert_id=alert.alert_id if alert else None)
    try:
        return chat_agent.answer(ctx, text, on_change=on_change)
    except Exception:  # noqa: BLE001 - never leave the user without an answer
        log.exception("chat agent failed")
        return ":warning: The AI assistant is unavailable right now. " + status_text(repo, channel, thread)


def build_slack_app(bot_token: str, signing_secret: str, repo: Repository, chat: Any, chat_agent: Any = None,
                    token_verification_enabled: bool = True) -> App:
    app = App(token=bot_token, signing_secret=signing_secret, process_before_response=False,
              token_verification_enabled=token_verification_enabled)

    def _on_action(ack, body, action):  # noqa: ANN001 - Bolt injects arguments by name
        ack()  # must be < 3 s; the rest runs after the ack
        user = body["user"]["id"]
        alert_id = action["value"]
        text = handle_action(repo, action["action_id"], alert_id, user)
        try:
            alert = repo.get(alert_id)
            if alert.chat_channel and alert.chat_thread:
                ref = ThreadRef(alert.chat_channel, alert.chat_thread)
                chat.reply(ref, text)
                if hasattr(chat, "update_alert"):
                    chat.update_alert(ref, alert.to_dict())
        except AlertNotFound:
            log.warning("action for unknown alert %s", alert_id)

    for action_id in (ACTION_FIX, ACTION_APPROVE, ACTION_REJECT):
        app.action(action_id)(_on_action)

    @app.event("app_mention")
    def _on_mention(event, say):  # noqa: ANN001 - Bolt acks events before running this listener
        say(text=answer_mention(repo, chat, chat_agent, event), thread_ts=event.get("thread_ts") or event["ts"])

    @app.event("message")
    def _on_message(event):  # noqa: ANN001 - thread messages: handled by the chat agent in Phase 3.3
        return None

    return app
