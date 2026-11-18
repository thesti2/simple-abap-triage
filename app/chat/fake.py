"""In-memory chat client for tests and offline local runs."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from app.chat.base import ThreadRef


class FakeChatClient:
    def __init__(self, channel: str = "CFAKE", fail: bool = False) -> None:
        self.channel = channel
        self.fail = fail
        self.posted: list[dict[str, Any]] = []
        self.replies: list[tuple[ThreadRef, str]] = []
        self.files: list[tuple[ThreadRef, str]] = []
        self._ts = itertools.count(1)

    def post_alert(self, alert: dict[str, Any]) -> ThreadRef:
        if self.fail:
            raise RuntimeError("chat unavailable")
        self.posted.append(alert)
        return ThreadRef(self.channel, f"1000.{next(self._ts):06d}")

    def reply(self, ref: ThreadRef, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        self.replies.append((ref, text))

    def upload_file(self, ref: ThreadRef, path: Path, title: str, comment: str = "") -> None:
        self.files.append((ref, title))
