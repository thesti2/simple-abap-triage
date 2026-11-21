import asyncio

import pytest
from fastapi.testclient import TestClient

from app.chat.fake import FakeChatClient
from app.config import Settings
from app.db import AlertNotFound
from app.main import Services, create_app
from app.pipeline.alert_graph import build_alert_graph
from app.status import IllegalTransition
from app.worker_api import RemoteRepository
from tests.conftest import triaged
from tests.test_fix_worker import FakeAdt, _fix_script, _worker

WTOKEN = "worker-token-456"


@pytest.fixture
def api(repo, chat, classifier):
    settings = Settings(_env_file=None, xmatters_webhook_token="x", worker_api_token=WTOKEN, slack_mode="off",
                        openrouter_api_key="", model="")
    svc = Services(settings, repo, classifier, chat, build_alert_graph(repo, classifier, chat))
    with TestClient(create_app(svc, start_socket_mode=False)) as c:
        yield c


@pytest.fixture
def remote(api):
    return RemoteRepository("http://testserver", WTOKEN, client=api)


def test_worker_api_requires_token(api, repo):
    triaged(repo, "div0")
    assert api.get("/worker/alerts").status_code == 401
    assert api.get("/worker/alerts", headers={"X-Worker-Token": "wrong"}).status_code == 401
    assert api.get("/worker/alerts", headers={"X-Worker-Token": WTOKEN}).status_code == 200


def test_worker_api_closed_without_configured_token(repo, chat, classifier):
    settings = Settings(_env_file=None, worker_api_token="", slack_mode="off", openrouter_api_key="", model="")
    svc = Services(settings, repo, classifier, chat, build_alert_graph(repo, classifier, chat))
    with TestClient(create_app(svc, start_socket_mode=False)) as c:
        assert c.get("/worker/alerts", headers={"X-Worker-Token": ""}).status_code == 401


def test_remote_repository_round_trip(remote, repo):
    aid = triaged(repo, "div0")
    repo.transition(aid, "FIX_REQUESTED", actor="slack:U1")
    assert [a.alert_id for a in remote.list_alerts(statuses=["FIX_REQUESTED"], limit=5)] == [aid]
    assert remote.list_alerts(statuses=["APPROVED"]) == []
    a = remote.get(aid)
    assert a.program == "ZTRIAGE_DEMO_BUG" and a.payload["variant"] == "ZBUG_DIV0" and a.to_dict()["status"] == "FIX_REQUESTED"
    remote.transition(aid, "FIX_IN_PROGRESS", actor="fix-worker", note="started")
    assert remote.update_fields(aid, fix_tr="XYZK900001").fix_tr == "XYZK900001"
    assert repo.get(aid).status == "FIX_IN_PROGRESS" and repo.history(aid)[-1].note == "started"


def test_remote_repository_errors_and_least_privilege(remote, api, repo):
    aid = triaged(repo, "div0")
    with pytest.raises(AlertNotFound):
        remote.get("NOPE")
    with pytest.raises(IllegalTransition):
        remote.transition(aid, "FIX_READY")                      # TRIAGED -> FIX_READY not allowed
    h = {"X-Worker-Token": WTOKEN}
    assert api.post(f"/worker/alerts/{aid}/transition", json={"status": "APPROVED"}, headers=h).status_code == 403
    assert api.patch(f"/worker/alerts/{aid}", json={"fields": {"category": "BASIS"}}, headers=h).status_code == 403
    assert repo.get(aid).status == "TRIAGED" and repo.get(aid).category == "DEVELOPER"


def test_alert_id_with_slash(remote, repo):
    from tests.conftest import load

    p = load("div0") | {"alert_id": "XYZ-100-JOB/WITH/SLASH-1"}
    repo.create_alert(p)
    assert remote.get("XYZ-100-JOB/WITH/SLASH-1").alert_id == "XYZ-100-JOB/WITH/SLASH-1"


def test_fix_worker_end_to_end_over_worker_api(remote, repo, tmp_path):
    aid = triaged(repo, "div0")
    repo.update_fields(aid, chat_channel="C1", chat_thread="1.1")
    repo.transition(aid, "FIX_REQUESTED", actor="slack:U1")
    chat = FakeChatClient()
    w = _worker(remote, chat, FakeAdt(), _fix_script(), tmp_path)
    assert asyncio.run(w.process(aid)) == "FIX_READY"
    a = repo.get(aid)
    assert a.status == "FIX_READY" and a.fix_tr == "XYZK900001"
    assert [e.to_status for e in repo.history(aid)][-2:] == ["FIX_IN_PROGRESS", "FIX_READY"]
    assert "Fix ready for review" in chat.replies[-1][1]
