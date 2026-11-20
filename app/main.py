"""FastAPI backend: xMatters webhook -> alert pipeline; Slack events (Socket Mode locally, HTTP on Cloud Run).

Run locally:  uv run uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.chat.base import ChatClient
from app.chat.fake import FakeChatClient
from app.config import Settings, get_settings
from app.db import AlertNotFound, Repository
from app.pipeline.alert_graph import build_alert_graph
from app.pipeline.classifier import Classifier, LLMClassifier, rule_based
from app.schemas import AlertPayload

log = logging.getLogger("abap_triage")


@dataclass
class Services:
    settings: Settings
    repo: Repository
    classifier: Classifier
    chat: ChatClient
    graph: Any
    slack_app: Any = None


def default_services(settings: Settings) -> Services:
    repo = Repository(settings.db_url)
    chat_agent = None
    if settings.openrouter_api_key and settings.model:
        from app.agents.chat_agent import ChatAgent, make_checkpointer
        from app.llm import get_llm

        llm = get_llm(settings)
        classifier: Classifier = LLMClassifier(llm, method=settings.structured_output_method)
        chat_agent = ChatAgent(llm, repo, make_checkpointer(settings.db_url))
    else:
        log.warning("no LLM configured - using rule-based classification, no chat agent")
        classifier = lambda a: (rule_based(a), "rules")  # noqa: E731

    slack_app = None
    if settings.slack_mode != "off" and settings.slack_bot_token and settings.slack_channel_id:
        from app.chat.slack import SlackChatClient
        from app.slack_app import build_slack_app

        chat: ChatClient = SlackChatClient(settings.slack_bot_token, settings.slack_channel_id)
        slack_app = build_slack_app(settings.slack_bot_token, settings.slack_signing_secret, repo, chat, chat_agent)
    else:
        log.warning("Slack not configured - using in-memory fake chat client")
        chat = FakeChatClient()
    return Services(settings, repo, classifier, chat, build_alert_graph(repo, classifier, chat), slack_app)


def create_app(services: Services | None = None, start_socket_mode: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or default_services(get_settings())
        app.state.svc = svc
        handler = None
        s = svc.settings
        if start_socket_mode and svc.slack_app is not None and s.slack_mode == "socket" and s.slack_app_token:
            from slack_bolt.adapter.socket_mode import SocketModeHandler

            handler = SocketModeHandler(svc.slack_app, s.slack_app_token)
            threading.Thread(target=handler.connect, daemon=True).start()
            log.info("Slack Socket Mode connected")
        yield
        if handler is not None:
            handler.close()

    app = FastAPI(title="ABAP Triage", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/xmatters", status_code=status.HTTP_202_ACCEPTED)
    def xmatters_webhook(
        payload: AlertPayload,
        background: BackgroundTasks,
        request: Request,
        x_triage_token: str | None = Header(default=None),
    ) -> Any:
        svc: Services = request.app.state.svc
        expected = svc.settings.xmatters_webhook_token
        # fail closed: no configured token -> nobody gets in
        if not expected or not x_triage_token or not secrets.compare_digest(x_triage_token, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
        try:
            existing = svc.repo.get(payload.alert_id)
            return JSONResponse({"alert_id": payload.alert_id, "result": "duplicate", "status": existing.status},
                                status_code=status.HTTP_200_OK)
        except AlertNotFound:
            pass
        background.add_task(run_pipeline, svc, payload.model_dump())
        return {"alert_id": payload.alert_id, "result": "accepted"}

    @app.post("/slack/events")
    @app.post("/slack/interactions")
    async def slack_http(request: Request) -> Any:
        """HTTP mode (Cloud Run). In Socket Mode these endpoints are not used."""
        svc: Services = request.app.state.svc
        if svc.slack_app is None or svc.settings.slack_mode != "http":
            raise HTTPException(status_code=404, detail="Slack HTTP mode not enabled")
        from slack_bolt.adapter.fastapi import SlackRequestHandler

        return await SlackRequestHandler(svc.slack_app).handle(request)

    return app


def run_pipeline(svc: Services, payload: dict[str, Any]) -> dict[str, Any]:
    result = svc.graph.invoke({"payload": payload}, config={"run_name": "alert_pipeline",
                                                              "metadata": {"alert_id": payload.get("alert_id")}})
    log.info("pipeline done for %s: %s", payload.get("alert_id"),
             {k: result.get(k) for k in ("duplicate", "triage_source", "posted", "error")})
    return result


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
