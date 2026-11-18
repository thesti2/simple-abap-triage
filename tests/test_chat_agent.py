import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.chat_agent import ChatAgent, ChatContext, build_tools
from app.slack_app import answer_mention
from tests.conftest import triaged


class ScriptedModel(GenericFakeChatModel):
    """Fake chat model that returns scripted AIMessages (incl. tool calls); records what it was sent."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):  # create_agent binds tools; the script decides the calls
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _tools(repo, alert_id=None, user="U1"):
    ctx = ChatContext(user_id=user, channel="C1", thread="1.1" if alert_id else None, alert_id=alert_id)
    return {t.name: t for t in build_tools(repo, ctx)}


# ---------------------------------------------------------------- tools (deterministic)
def test_get_alert_defaults_to_thread_alert(repo):
    aid = triaged(repo, "div0")
    data = json.loads(_tools(repo, aid)["get_alert"].invoke({}))
    assert data["alert_id"] == aid and data["error_line"] == "55" and data["joblog"]


def test_get_alert_outside_thread_needs_id(repo):
    assert "No alert in this thread" in _tools(repo)["get_alert"].invoke({})


def test_list_open_alerts(repo):
    triaged(repo, "div0")
    triaged(repo, "rfc", category="BASIS", fixable=False)
    rows = json.loads(_tools(repo)["list_alerts"].invoke({"status": "OPEN"}))
    assert len(rows) == 2 and {r["status"] for r in rows} == {"TRIAGED"}


def test_request_fix_tool_respects_rules(repo):
    dev = triaged(repo, "div0")
    basis = triaged(repo, "rfc", category="BASIS", fixable=False)
    assert json.loads(_tools(repo, basis)["request_fix"].invoke({}))["ok"] is False
    r = json.loads(_tools(repo, dev, user="U7")["request_fix"].invoke({}))
    assert r["ok"] is True
    a = repo.get(dev)
    assert a.status == "FIX_REQUESTED" and repo.history(dev)[-1].actor == "slack:U7"


def test_request_fix_with_other_variant(repo):
    dev = triaged(repo, "div0")
    _tools(repo, dev)["request_fix"].invoke({"variant": "zok"})
    assert repo.get(dev).variant == "ZOK"


def test_approve_tool_only_when_fix_ready(repo):
    dev = triaged(repo, "div0")
    tools = _tools(repo, dev)
    assert json.loads(tools["approve_fix"].invoke({}))["ok"] is False
    for s in ("FIX_REQUESTED", "FIX_IN_PROGRESS", "FIX_READY"):
        repo.transition(dev, s)
    assert json.loads(tools["approve_fix"].invoke({}))["ok"] is True
    assert repo.get(dev).approved_by == "U1"


def test_history_tool(repo):
    dev = triaged(repo, "div0")
    rows = json.loads(_tools(repo, dev)["get_alert_history"].invoke({}))
    assert [r["to"] for r in rows] == ["NEW", "TRIAGED"]


# ---------------------------------------------------------------- agent loop (scripted model)
def test_agent_calls_tool_and_answers(repo):
    dev = triaged(repo, "div0")
    model = ScriptedModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "request_fix", "args": {}, "id": "c1"}]),
        AIMessage(content="Fix requested for `ZTRIAGE_DEMO_BUG`."),
    ]))
    changed = []
    agent = ChatAgent(model, repo, InMemorySaver())
    ctx = ChatContext("U5", "C1", "1.1", dev)
    reply = agent.answer(ctx, "please fix it", on_change=changed.append)
    assert "Fix requested" in reply
    assert repo.get(dev).status == "FIX_REQUESTED" and changed == [dev]


def test_agent_memory_per_thread(repo):
    dev = triaged(repo, "div0")
    model = ScriptedModel(messages=iter([AIMessage(content="first"), AIMessage(content="second"),
                                         AIMessage(content="other thread")]))
    model.seen.clear()
    agent = ChatAgent(model, repo, InMemorySaver())
    agent.answer(ChatContext("U1", "C1", "1.1", dev), "question one")
    agent.answer(ChatContext("U1", "C1", "1.1", dev), "question two")
    agent.answer(ChatContext("U1", "C1", "9.9", None), "question three")
    texts = [[getattr(m, "content", "") for m in call] for call in model.seen]
    assert any("question one" in t for t in texts[1])          # same thread remembers
    assert not any("question one" in t for t in texts[2])      # other thread does not


# ---------------------------------------------------------------- Slack mention handling
class _BrokenAgent:
    def answer(self, *a, **k):
        raise RuntimeError("LLM down")


def test_mention_falls_back_when_agent_fails(repo, chat):
    dev = triaged(repo, "div0")
    repo.update_fields(dev, chat_channel="C1", chat_thread="1.1")
    text = answer_mention(repo, chat, _BrokenAgent(), {"channel": "C1", "thread_ts": "1.1", "user": "U1",
                                                       "text": "<@UBOT> status?", "ts": "1.2"})
    assert "unavailable" in text and "TRIAGED" in text


def test_mention_strips_bot_id_and_passes_thread_alert(repo, chat):
    dev = triaged(repo, "div0")
    repo.update_fields(dev, chat_channel="C1", chat_thread="1.1")
    seen = {}

    class _Agent:
        def answer(self, ctx, text, on_change=None):
            seen.update(ctx=ctx, text=text)
            return "ok"

    answer_mention(repo, chat, _Agent(), {"channel": "C1", "thread_ts": "1.1", "user": "U3",
                                          "text": "<@UBOT123> what is the root cause?", "ts": "1.5"})
    assert seen["text"] == "what is the root cause?"
    assert seen["ctx"].alert_id == dev and seen["ctx"].user_id == "U3" and seen["ctx"].thread_id == "C1:1.1"


# ---------------------------------------------------------------- live (real LLM): uv run pytest -m live -s
@pytest.fixture
def live_agent(repo):
    from app.config import get_settings
    from app.llm import get_llm

    return ChatAgent(get_llm(get_settings()), repo, InMemorySaver())


@pytest.mark.live
def test_live_root_cause_is_grounded(repo, live_agent):
    dev = triaged(repo, "div0")
    repo.update_fields(dev, root_cause="Division by zero at line 55 because P_QTY is 0.")
    reply = live_agent.answer(ChatContext("U1", "C1", "1.1", dev), "what is the root cause?")
    print("\n", reply)
    assert "55" in reply or "zero" in reply.lower()


@pytest.mark.live
def test_live_fix_refused_for_basis(repo, live_agent):
    basis = triaged(repo, "rfc", category="BASIS", fixable=False)
    reply = live_agent.answer(ChatContext("U1", "C1", "1.1", basis), "please fix this program")
    print("\n", reply)
    assert repo.get(basis).status == "TRIAGED"


@pytest.mark.live
def test_live_does_not_approve_without_explicit_request(repo, live_agent):
    dev = triaged(repo, "div0")
    for s in ("FIX_REQUESTED", "FIX_IN_PROGRESS", "FIX_READY"):
        repo.transition(dev, s)
    reply = live_agent.answer(ChatContext("U1", "C1", "1.1", dev), "what changed in the fix?")
    print("\n", reply)
    assert repo.get(dev).status == "FIX_READY"


@pytest.mark.live
def test_live_fix_request_and_list(repo, live_agent):
    dev = triaged(repo, "div0")
    reply = live_agent.answer(ChatContext("U1", "C1", "1.1", dev), "yes, please fix it")
    print("\n", reply)
    assert repo.get(dev).status == "FIX_REQUESTED"
    reply = live_agent.answer(ChatContext("U1", "C1", None, None), "list open alerts")
    print("\n", reply)
    assert "ZTRIAGE_TEST_DIV0" in reply or dev in reply
