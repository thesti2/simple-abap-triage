from app.chat.fake import FakeChatClient
from app.main import run_pipeline
from app.pipeline.alert_graph import build_alert_graph
from tests.conftest import TOKEN, load


def test_pipeline_triages_posts_and_saves_thread(services, repo, chat):
    p = load("div0")
    out = run_pipeline(services, p)
    assert out["posted"] is True and not out.get("duplicate")
    a = repo.get(p["alert_id"])
    assert a.status == "TRIAGED" and a.category == "DEVELOPER" and a.fixable_by_agent is True
    assert (a.chat_channel, a.chat_thread) == ("CFAKE", "1000.000001")
    assert len(chat.posted) == 1 and chat.posted[0]["root_cause"]


def test_pipeline_duplicate_is_ignored(services, chat, classifier):
    p = load("div0")
    run_pipeline(services, p)
    out = run_pipeline(services, p)
    assert out["duplicate"] is True
    assert len(chat.posted) == 1 and classifier.calls == 1


def test_chat_failure_keeps_alert_triaged(repo, classifier):
    graph = build_alert_graph(repo, classifier, FakeChatClient(fail=True))
    p = load("rfc")
    out = graph.invoke({"payload": p})
    assert out["posted"] is False and "chat post failed" in out["error"]
    a = repo.get(p["alert_id"])
    assert a.status == "TRIAGED" and a.chat_thread is None


# ---------------------------------------------------------------- webhook
def test_webhook_rejects_missing_or_wrong_token(client):
    assert client.post("/webhooks/xmatters", json=load("div0")).status_code == 401
    assert client.post("/webhooks/xmatters", json=load("div0"), headers={"X-Triage-Token": "wrong"}).status_code == 401


def test_webhook_rejects_malformed_payload(client):
    r = client.post("/webhooks/xmatters", json={"job_name": "no id"}, headers={"X-Triage-Token": TOKEN})
    assert r.status_code == 422


def test_webhook_accepts_and_processes(client, repo, chat):
    p = load("data")
    r = client.post("/webhooks/xmatters", json=p, headers={"X-Triage-Token": TOKEN})
    assert r.status_code == 202 and r.json() == {"alert_id": p["alert_id"], "result": "accepted"}
    # TestClient runs background tasks before returning
    a = repo.get(p["alert_id"])
    assert a.status == "TRIAGED" and a.category == "FUNCTIONAL"
    assert len(chat.posted) == 1


def test_webhook_duplicate_returns_200(client, chat):
    p = load("div0")
    client.post("/webhooks/xmatters", json=p, headers={"X-Triage-Token": TOKEN})
    r = client.post("/webhooks/xmatters", json=p, headers={"X-Triage-Token": TOKEN})
    assert r.status_code == 200 and r.json()["result"] == "duplicate" and r.json()["status"] == "TRIAGED"
    assert len(chat.posted) == 1


def test_webhook_fails_closed_without_configured_token(services, client):
    services.settings.xmatters_webhook_token = ""
    r = client.post("/webhooks/xmatters", json=load("div0"), headers={"X-Triage-Token": ""})
    assert r.status_code == 401


def test_extra_fields_from_xmatters_are_tolerated(client, repo):
    p = load("rfc") | {"xm_event_id": "abc", "priority": "HIGH"}
    assert client.post("/webhooks/xmatters", json=p, headers={"X-Triage-Token": TOKEN}).status_code == 202
    assert repo.get(p["alert_id"]).payload["priority"] == "HIGH"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
