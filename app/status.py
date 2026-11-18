"""Alert status lifecycle. Every change goes through `check_transition` (enforced in the repository)."""

from enum import StrEnum


class Status(StrEnum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    FIX_REQUESTED = "FIX_REQUESTED"
    FIX_IN_PROGRESS = "FIX_IN_PROGRESS"
    FIX_READY = "FIX_READY"
    FIX_FAILED = "FIX_FAILED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RELEASED = "RELEASED"
    CLOSED = "CLOSED"


ALLOWED: dict[Status, set[Status]] = {
    Status.NEW: {Status.TRIAGED, Status.CLOSED},
    Status.TRIAGED: {Status.FIX_REQUESTED, Status.CLOSED},
    Status.FIX_REQUESTED: {Status.FIX_IN_PROGRESS, Status.FIX_FAILED, Status.CLOSED},
    Status.FIX_IN_PROGRESS: {Status.FIX_READY, Status.FIX_FAILED},
    Status.FIX_READY: {Status.APPROVED, Status.REJECTED},
    Status.FIX_FAILED: {Status.FIX_REQUESTED, Status.CLOSED},
    Status.REJECTED: {Status.FIX_REQUESTED, Status.CLOSED},
    Status.APPROVED: {Status.RELEASED},  # release failure keeps APPROVED (error posted to thread)
    Status.RELEASED: {Status.CLOSED},
    Status.CLOSED: set(),
}


class IllegalTransition(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    cur, nxt = Status(current), Status(new)
    if nxt not in ALLOWED[cur]:
        raise IllegalTransition(f"{cur} -> {nxt} is not allowed")
