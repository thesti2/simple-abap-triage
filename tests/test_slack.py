import hashlib
import hmac
import json
import time

from app.chat.slack_blocks import ACTION_APPROVE, ACTION_FIX, ACTION_REJECT, alert_blocks, approval_blocks
from app.slack_app import build_slack_app, handle_action, status_text
from tests.conftest import triaged


def _alert(**kw):
    base = {"alert_id": "A1", "status": "TRIAGED", "job_name": "J", "program": "ZP", "variant": "V",
            "runtime_error": "COMPUTE_INT_ZERODIVIDE", "category": "DEVELOPER", "confidence": 0.9,
            "summary": "s", "root_cause": "rc", "recommendation": "rec", "fixable_by_agent": True,
            "payload": {"error_program": "ZP", "error_line": "55"}}
    return base | kw


def _action_ids(blocks):
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_card_has_fix_button_only_when_fixable():
    assert _action_ids(alert_blocks(_alert())) == [ACTION_FIX]
    assert _action_ids(alert_blocks(_alert(fixable_by_agent=False, category="BASIS"))) == []


def test_card_limits_and_content():
    blocks = alert_blocks(_alert(root_cause="x" * 5000))
    assert len(blocks) <= 50
    text = json.dumps(blocks)
    assert "line 55" in text and "Developer" in text and "A1" in text
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section" and "text" in b)


def test_rules_fallback_is_visible_on_card():
    assert "rule-based" in json.dumps(alert_blocks(_alert(classification_source="rules")))


def test_approval_blocks():
    assert _action_ids(approval_blocks("A1", "Fix ready")) == [ACTION_APPROVE, ACTION_REJECT]


# ---------------------------------------------------------------- button actions (DB logic)
def test_fix_button_requests_fix(repo):
    aid = triaged(repo, "div0")
    msg = handle_action(repo, ACTION_FIX, aid, "U1")
    assert "requested a fix" in msg
    a = repo.get(aid)
    assert a.status == "FIX_REQUESTED"
    assert repo.history(aid)[-1].actor == "slack:U1"


def test_fix_refused_for_functional(repo):
    aid = triaged(repo, "data", category="FUNCTIONAL", fixable=False)
    assert "only handles program bugs" in handle_action(repo, ACTION_FIX, aid, "U1")
    assert repo.get(aid).status == "TRIAGED"


def test_fix_twice_is_not_possible(repo):
    aid = triaged(repo, "div0")
    handle_action(repo, ACTION_FIX, aid, "U1")
    assert "Not possible now" in handle_action(repo, ACTION_FIX, aid, "U2")


def test_approve_requires_fix_ready(repo):
    aid = triaged(repo, "div0")
    assert "Not possible now" in handle_action(repo, ACTION_APPROVE, aid, "U1")
    repo.transition(aid, "FIX_REQUESTED")
    repo.transition(aid, "FIX_IN_PROGRESS")
    repo.transition(aid, "FIX_READY")
    assert "approved" in handle_action(repo, ACTION_APPROVE, aid, "U9")
    a = repo.get(aid)
    assert a.status == "APPROVED" and a.approved_by == "U9"


def test_reject(repo):
    aid = triaged(repo, "div0")
    for s in ("FIX_REQUESTED", "FIX_IN_PROGRESS", "FIX_READY"):
        repo.transition(aid, s)
    assert "rejected" in handle_action(repo, ACTION_REJECT, aid, "U1")
    assert repo.get(aid).status == "REJECTED"


def test_unknown_alert_action(repo):
    assert "not found" in handle_action(repo, ACTION_FIX, "missing", "U1")


def test_status_text_in_thread_and_outside(repo):
    aid = triaged(repo, "div0")
    repo.update_fields(aid, chat_channel="C1", chat_thread="1.1")
    assert "is *TRIAGED*" in status_text(repo, "C1", "1.1")
    assert aid in status_text(repo, "C1", None)


# ---------------------------------------------------------------- Slack HTTP mode: signature check
def _signed(secret: str, body: str, ts: int | None = None) -> dict[str, str]:
    ts = ts or int(time.time())
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig, "Content-Type": "application/json"}


def _http_client(services, repo, chat):
    from fastapi.testclient import TestClient

    from app.main import create_app

    services.settings.slack_mode = "http"
    services.slack_app = build_slack_app("xoxb-test", "secret123", repo, chat, token_verification_enabled=False)
    return TestClient(create_app(services, start_socket_mode=False))


def test_slack_http_rejects_bad_signature(services, repo, chat):
    body = json.dumps({"type": "url_verification", "challenge": "abc"})
    with _http_client(services, repo, chat) as c:
        assert c.post("/slack/events", content=body, headers=_signed("wrong", body)).status_code == 401
        old = _signed("secret123", body, ts=int(time.time()) - 600)  # older than 5 minutes
        assert c.post("/slack/events", content=body, headers=old).status_code == 401


def test_slack_http_url_verification(services, repo, chat):
    body = json.dumps({"type": "url_verification", "challenge": "abc"})
    with _http_client(services, repo, chat) as c:
        r = c.post("/slack/events", content=body, headers=_signed("secret123", body))
        assert r.status_code == 200 and "abc" in r.text
