import json
import os
from pathlib import Path

import pytest

# Unit tests never send traces or call external services.
os.environ["LANGSMITH_TRACING"] = "false"

from app.chat.fake import FakeChatClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Repository  # noqa: E402
from app.main import Services, create_app  # noqa: E402
from app.pipeline.alert_graph import build_alert_graph  # noqa: E402
from app.pipeline.classifier import rule_based  # noqa: E402
from app.schemas import TriageResult  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token-123"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"alert_{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def chat() -> FakeChatClient:
    return FakeChatClient()


class FakeClassifier:
    """Deterministic classifier for pipeline tests (rule based, counts calls)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, alert):
        self.calls += 1
        return rule_based(alert), "llm"


@pytest.fixture
def classifier() -> FakeClassifier:
    return FakeClassifier()


@pytest.fixture
def services(repo, chat, classifier) -> Services:
    settings = Settings(_env_file=None, xmatters_webhook_token=TOKEN, slack_mode="off",
                        openrouter_api_key="", model="")
    return Services(settings, repo, classifier, chat, build_alert_graph(repo, classifier, chat))


@pytest.fixture
def client(services):
    from fastapi.testclient import TestClient

    with TestClient(create_app(services, start_socket_mode=False)) as c:
        yield c


def triaged(repo: Repository, name: str, category: str = "DEVELOPER", fixable: bool = True) -> str:
    """Create an alert already in status TRIAGED."""
    payload = load(name)
    repo.create_alert(payload)
    result = TriageResult(category=category, confidence=0.9, summary="s", root_cause="rc",
                          recommendation="rec", fixable_by_agent=fixable)
    repo.transition(payload["alert_id"], "TRIAGED", **result.model_dump())
    return payload["alert_id"]
