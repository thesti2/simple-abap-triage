"""FastAPI backend: xMatters webhook -> alert pipeline; Slack events (Socket Mode locally, HTTP on Cloud Run).

Run locally:  uv run uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
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
from app.worker_api import router as worker_router

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


class ServiceHolder:
    """Builds the services (DB, Slack, LLM) once. If that fails (database stopped while GCP is paused, Cloud SQL
    still starting) the app still starts; requests get 503 and the build is retried at most every `retry_s`."""

    def __init__(self, factory: Callable[[], Services], retry_s: float = 30) -> None:
        self.factory, self.retry_s = factory, retry_s
        self.svc: Services | None = None
        self._lock = threading.Lock()
        self._next_try = 0.0

    def try_get(self) -> Services | None:
        try:
            return self.get()
        except HTTPException:
            return None

    def get(self) -> Services:
        if self.svc is not None:
            return self.svc
        with self._lock:
            if self.svc is None:
                if time.monotonic() < self._next_try:
                    raise HTTPException(status_code=503, detail="backend not ready (database unavailable)")
                try:
                    self.svc = self.factory()
                except Exception as e:  # noqa: BLE001
                    self._next_try = time.monotonic() + self.retry_s
                    log.error("services not available (database down or GCP paused?): %s", e)
                    raise HTTPException(status_code=503, detail="backend not ready (database unavailable)") from None
        return self.svc


def create_app(services: Services | None = None, start_socket_mode: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder = ServiceHolder(lambda: services or default_services(get_settings()))
        app.state.services = holder
        handler = None
        svc = holder.try_get()  # database down (e.g. GCP paused) -> start anyway, requests get 503 + retry later
        s = svc.settings if svc else None
        if (start_socket_mode and svc is not None and svc.slack_app is not None and s.slack_mode == "socket"
                and s.slack_app_token):
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
        svc = request.app.state.services.get()
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
        svc = request.app.state.services.get()
        if svc.slack_app is None or svc.settings.slack_mode != "http":
            raise HTTPException(status_code=404, detail="Slack HTTP mode not enabled")
        from slack_bolt.adapter.fastapi import SlackRequestHandler

        return await SlackRequestHandler(svc.slack_app).handle(request)

    # fix worker -> alert store over HTTPS (app/worker_api.py)
    app.include_router(worker_router)
    return app


def run_pipeline(svc: Services, payload: dict[str, Any]) -> dict[str, Any]:
    result = svc.graph.invoke({"payload": payload}, config={"run_name": "alert_pipeline",
                                                              "metadata": {"alert_id": payload.get("alert_id")}})
    log.info("pipeline done for %s: %s", payload.get("alert_id"),
             {k: result.get(k) for k in ("duplicate", "triage_source", "posted", "error")})
    return result


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
