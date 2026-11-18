"""Slack implementation of ChatClient (Web API). Run `python -m app.chat.slack --test` for a live check."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from slack_sdk import WebClient

from app.chat.base import ThreadRef
from app.chat.slack_blocks import alert_blocks, alert_fallback_text


class SlackChatClient:
    def __init__(self, bot_token: str, channel_id: str, client: WebClient | None = None) -> None:
        if not channel_id:
            raise RuntimeError("SLACK_CHANNEL_ID is not set")
        self.client = client or WebClient(token=bot_token)
        self.channel = channel_id

    def post_alert(self, alert: dict[str, Any]) -> ThreadRef:
        r = self.client.chat_postMessage(
            channel=self.channel,
            text=alert_fallback_text(alert),
            blocks=alert_blocks(alert),
            unfurl_links=False,
        )
        return ThreadRef(r["channel"], r["ts"])

    def update_alert(self, ref: ThreadRef, alert: dict[str, Any]) -> None:
        """Refresh the parent card (e.g. after a status change)."""
        self.client.chat_update(channel=ref.channel, ts=ref.thread, text=alert_fallback_text(alert), blocks=alert_blocks(alert))

    def reply(self, ref: ThreadRef, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        self.client.chat_postMessage(channel=ref.channel, thread_ts=ref.thread, text=text, blocks=blocks, unfurl_links=False)

    def upload_file(self, ref: ThreadRef, path: Path, title: str, comment: str = "") -> None:
        # files_upload_v2 uses files.getUploadURLExternal + files.completeUploadExternal
        self.client.files_upload_v2(
            channel=ref.channel, thread_ts=ref.thread, file=str(path), title=title, initial_comment=comment or None
        )


def _live_test() -> None:
    from app.config import get_settings

    s = get_settings()
    chat = SlackChatClient(s.slack_bot_token, s.slack_channel_id)
    auth = chat.client.auth_test()
    print(f"auth ok: bot user {auth['user']} in team {auth['team']}")
    demo = {
        "alert_id": "TEST-0001",
        "status": "TRIAGED",
        "job_name": "ZTRIAGE_TEST_DIV0",
        "system": "DEV",
        "program": "ZTRIAGE_DEMO_BUG",
        "variant": "ZBUG_DIV0",
        "runtime_error": "COMPUTE_INT_ZERODIVIDE",
        "category": "DEVELOPER",
        "confidence": 0.95,
        "summary": "*Test message* from the ABAP Triage backend (Phase 2 check) - safe to ignore.",
        "root_cause": "Division by zero (test data).",
        "recommendation": "No action - this is a connectivity test.",
        "fixable_by_agent": False,
        "payload": {"error_program": "ZTRIAGE_DEMO_BUG", "error_line": "55"},
    }
    ref = chat.post_alert(demo)
    chat.reply(ref, "Thread reply test :white_check_mark:")
    print(f"posted test card + thread reply (ts {ref.thread})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="post a test card + thread reply to SLACK_CHANNEL_ID")
    if parser.parse_args().test:
        _live_test()
