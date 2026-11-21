from fastapi.testclient import TestClient

import app.main as main
from tests.conftest import TOKEN, load


def test_app_starts_without_database_and_recovers(services, monkeypatch):
    """GCP paused / Cloud SQL stopped: the container must still start (Cloud Run startup probe), requests get 503,
    and once the database is back the services are built on the next request."""
    calls = {"n": 0}

    def factory(_settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection refused (database stopped)")
        return services

    monkeypatch.setattr(main, "default_services", factory)
    monkeypatch.setattr(main, "get_settings", lambda: None)
    with TestClient(main.create_app(start_socket_mode=False)) as c:
        assert c.get("/health").status_code == 200
        holder = c.app.state.services
        r = c.post("/webhooks/xmatters", json=load("div0"), headers={"X-Triage-Token": TOKEN})
        assert r.status_code == 503 and calls["n"] == 1               # retry back-off: not rebuilt immediately
        assert c.get("/worker/alerts", headers={"X-Worker-Token": "x"}).status_code == 503
        holder._next_try = 0                                          # back-off over, database is up again
        r = c.post("/webhooks/xmatters", json=load("div0"), headers={"X-Triage-Token": TOKEN})
        assert r.status_code == 202 and calls["n"] == 2
