"""Alert pipeline (LangGraph): persist -> classify -> persist triage -> post to chat -> save thread.

Deterministic: the only LLM use is the classify node (structured output). Posting to chat is plain code.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.chat.base import ChatClient
from app.db import DuplicateAlert, Repository
from app.pipeline.classifier import Classifier
from app.schemas import AlertPayload
from app.status import Status

log = logging.getLogger(__name__)


class AlertState(TypedDict, total=False):
    payload: dict[str, Any]
    alert_id: str
    duplicate: bool
    triage: dict[str, Any]
    triage_source: str
    posted: bool
    error: str


def build_alert_graph(repo: Repository, classifier: Classifier, chat: ChatClient):
    def persist_new(state: AlertState) -> AlertState:
        payload = AlertPayload.model_validate(state["payload"])
        try:
            repo.create_alert(payload.model_dump())
        except DuplicateAlert:
            log.info("duplicate alert %s ignored", payload.alert_id)
            return {"alert_id": payload.alert_id, "duplicate": True}
        return {"alert_id": payload.alert_id, "duplicate": False}

    def classify(state: AlertState) -> AlertState:
        result, source = classifier(AlertPayload.model_validate(state["payload"]))
        return {"triage": result.model_dump(), "triage_source": source}

    def persist_triage(state: AlertState) -> AlertState:
        repo.transition(
            state["alert_id"],
            Status.TRIAGED,
            actor="triage-agent",
            note=f"classified as {state['triage']['category']} ({state['triage_source']})",
            classification_source=state["triage_source"],
            **state["triage"],
        )
        return {}

    def post_to_chat(state: AlertState) -> AlertState:
        alert = repo.get(state["alert_id"]).to_dict()
        try:
            ref = chat.post_alert(alert)
        except Exception as e:  # noqa: BLE001 - alert stays TRIAGED in DB; can be re-posted
            log.exception("posting alert %s to chat failed", state["alert_id"])
            return {"posted": False, "error": f"chat post failed: {e}"}
        repo.update_fields(state["alert_id"], chat_channel=ref.channel, chat_thread=ref.thread)
        return {"posted": True}

    g = StateGraph(AlertState)
    g.add_node("persist_new", persist_new)
    g.add_node("classify", classify)
    g.add_node("persist_triage", persist_triage)
    g.add_node("post_to_chat", post_to_chat)
    g.add_edge(START, "persist_new")
    g.add_conditional_edges("persist_new", lambda s: END if s.get("duplicate") else "classify")
    g.add_edge("classify", "persist_triage")
    g.add_edge("persist_triage", "post_to_chat")
    g.add_edge("post_to_chat", END)
    return g.compile()
