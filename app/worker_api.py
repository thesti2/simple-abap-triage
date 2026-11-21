"""Worker API: the fix worker reaches the alert store over HTTPS (443) instead of a direct DB connection.

Why: corporate networks often block the Cloud SQL Auth Proxy port (3307); HTTPS to Cloud Run is open.
It also gives the worker only what it needs (least privilege) instead of full database access.

Server: `router` (mounted in app.main), protected by header X-Worker-Token (WORKER_API_TOKEN, fail closed).
Client: `RemoteRepository` - same methods the worker uses on `app.db.Repository` (get, list_alerts,
update_fields, transition), so the worker code does not change. Selected with TRIAGE_API_URL in .env.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.db import AlertNotFound
from app.status import IllegalTransition, Status

# The worker may only move alerts through the fix/release part of the lifecycle and record the fix TR.
WORKER_STATUSES = {Status.FIX_IN_PROGRESS, Status.FIX_READY, Status.FIX_FAILED, Status.RELEASED}
WORKER_FIELDS = {"fix_tr"}


# ---------------------------------------------------------------- server
def _check_token(request: Request, x_worker_token: str | None = Header(default=None)) -> None:
    expected = request.app.state.services.get().settings.worker_api_token
    if not expected or not x_worker_token or not secrets.compare_digest(x_worker_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid worker token")


router = APIRouter(prefix="/worker", dependencies=[Depends(_check_token)])


class FieldsBody(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)


class TransitionBody(BaseModel):
    status: str
    actor: str = "fix-worker"
    note: str = ""


def _repo(request: Request):
    return request.app.state.services.get().repo


def _out(alert) -> dict[str, Any]:
    return jsonable_encoder(alert.to_dict())


@router.get("/alerts")
def list_alerts(request: Request, status_: list[str] = Query(default=[], alias="status"),
                limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    return [_out(a) for a in _repo(request).list_alerts(statuses=status_ or None, limit=limit)]


@router.get("/alerts/{alert_id:path}")
def get_alert(alert_id: str, request: Request) -> dict[str, Any]:
    try:
        return _out(_repo(request).get(alert_id))
    except AlertNotFound:
        raise HTTPException(status_code=404, detail="alert not found") from None


@router.patch("/alerts/{alert_id:path}")
def update_fields(alert_id: str, body: FieldsBody, request: Request) -> dict[str, Any]:
    if bad := set(body.fields) - WORKER_FIELDS:
        raise HTTPException(status_code=403, detail=f"fields not allowed for the worker: {sorted(bad)}")
    try:
        return _out(_repo(request).update_fields(alert_id, **body.fields))
    except AlertNotFound:
        raise HTTPException(status_code=404, detail="alert not found") from None


@router.post("/alerts/{alert_id:path}/transition")
def transition(alert_id: str, body: TransitionBody, request: Request) -> dict[str, Any]:
    if body.status not in WORKER_STATUSES:
        raise HTTPException(status_code=403, detail=f"status {body.status} not allowed for the worker")
    try:
        return _out(_repo(request).transition(alert_id, body.status, actor=body.actor, note=body.note))
    except AlertNotFound:
        raise HTTPException(status_code=404, detail="alert not found") from None
    except IllegalTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


# ---------------------------------------------------------------- client
class RemoteAlert:
    """Read-only view of an alert returned by the API (attribute access like app.db.Alert)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class RemoteRepository:
    def __init__(self, base_url: str, token: str, timeout_s: float = 30, client: Any = None) -> None:
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self.headers = {"X-Worker-Token": token}

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, headers=self.headers, **kw)
        if r.status_code == 404:
            raise AlertNotFound(path)
        if r.status_code == 409:
            raise IllegalTransition(r.json().get("detail", "illegal transition"))
        if r.status_code >= 400:
            raise RuntimeError(f"worker API {method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _path(alert_id: str) -> str:
        return f"/worker/alerts/{quote(alert_id, safe='')}"

    def get(self, alert_id: str) -> RemoteAlert:
        return RemoteAlert(self._call("GET", self._path(alert_id)))

    def list_alerts(self, statuses: list[str] | None = None, limit: int = 20) -> list[RemoteAlert]:
        params = [("status", s) for s in statuses or []] + [("limit", str(limit))]
        return [RemoteAlert(d) for d in self._call("GET", "/worker/alerts", params=params)]

    def update_fields(self, alert_id: str, **fields: Any) -> RemoteAlert:
        return RemoteAlert(self._call("PATCH", self._path(alert_id), json={"fields": fields}))

    def transition(self, alert_id: str, new_status: str, actor: str = "system", note: str = "",
                   **fields: Any) -> RemoteAlert:
        if fields:
            raise ValueError("the worker API does not set fields during a transition")
        return RemoteAlert(self._call("POST", f"{self._path(alert_id)}/transition",
                                      json={"status": str(new_status), "actor": actor, "note": note}))
