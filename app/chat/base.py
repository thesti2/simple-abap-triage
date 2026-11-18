"""Chat abstraction. The pipeline only talks to this interface (Slack now, Google Chat possible later)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ThreadRef:
    channel: str
    thread: str  # Slack: ts of the parent message


class ChatClient(Protocol):
    def post_alert(self, alert: dict[str, Any]) -> ThreadRef:
        """Post the alert card as a new parent message; return the thread reference."""
        ...

    def reply(self, ref: ThreadRef, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        """Post a message into the alert's thread."""
        ...

    def upload_file(self, ref: ThreadRef, path: Path, title: str, comment: str = "") -> None:
        """Upload a file (e.g. test document) into the alert's thread."""
        ...
