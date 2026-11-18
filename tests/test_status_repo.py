import pytest

from app.db import AlertNotFound, DuplicateAlert
from app.status import IllegalTransition, Status, check_transition
from tests.conftest import load


def test_happy_path_transitions_allowed():
    path = ["NEW", "TRIAGED", "FIX_REQUESTED", "FIX_IN_PROGRESS", "FIX_READY", "APPROVED", "RELEASED", "CLOSED"]
    for cur, nxt in zip(path, path[1:]):
        check_transition(cur, nxt)


@pytest.mark.parametrize("cur,nxt", [("NEW", "APPROVED"), ("TRIAGED", "RELEASED"), ("FIX_REQUESTED", "APPROVED"),
                                     ("RELEASED", "TRIAGED"), ("CLOSED", "NEW")])
def test_illegal_transitions(cur, nxt):
    with pytest.raises(IllegalTransition):
        check_transition(cur, nxt)


def test_create_and_duplicate(repo):
    p = load("div0")
    a = repo.create_alert(p)
    assert a.status == Status.NEW and a.program == "ZTRIAGE_DEMO_BUG" and a.payload["error_line"] == "55"
    with pytest.raises(DuplicateAlert):
        repo.create_alert(p)
    assert len(repo.list_alerts()) == 1


def test_transition_is_logged_and_validated(repo):
    p = load("div0")
    repo.create_alert(p)
    repo.transition(p["alert_id"], "TRIAGED", actor="triage-agent", category="DEVELOPER")
    with pytest.raises(IllegalTransition):
        repo.transition(p["alert_id"], "RELEASED", actor="x")
    a = repo.get(p["alert_id"])
    assert a.status == "TRIAGED" and a.category == "DEVELOPER"
    events = repo.history(p["alert_id"])
    assert [(e.from_status, e.to_status) for e in events] == [(None, "NEW"), ("NEW", "TRIAGED")]
    assert events[1].actor == "triage-agent"


def test_update_fields_cannot_change_status(repo):
    p = load("div0")
    repo.create_alert(p)
    with pytest.raises(ValueError):
        repo.update_fields(p["alert_id"], status="RELEASED")


def test_unknown_alert(repo):
    with pytest.raises(AlertNotFound):
        repo.get("nope")


def test_find_by_thread(repo):
    p = load("div0")
    repo.create_alert(p)
    repo.update_fields(p["alert_id"], chat_channel="C1", chat_thread="1.2")
    assert repo.find_by_thread("C1", "1.2").alert_id == p["alert_id"]
    assert repo.find_by_thread("C1", "9.9") is None
