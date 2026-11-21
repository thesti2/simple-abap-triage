"""Persistence: alerts + status history. SQLite locally, Postgres (Cloud SQL) in GCP - same code."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.status import Status, check_transition


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Alert(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default=Status.NEW)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # key fields from the payload (full payload kept in `payload`)
    system: Mapped[str] = mapped_column(String(10), default="")
    job_name: Mapped[str] = mapped_column(String(64), default="")
    program: Mapped[str] = mapped_column(String(64), default="")
    variant: Mapped[str] = mapped_column(String(64), default="")
    error_type: Mapped[str] = mapped_column(String(20), default="")
    runtime_error: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # triage result
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixable_by_agent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    classification_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # llm | rules

    # chat thread (Slack: channel + parent message ts)
    chat_channel: Mapped[str | None] = mapped_column(String(50), nullable=True)
    chat_thread: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # fix (Phase 3.4/3.5)
    fix_tr: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alerts.alert_id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text, default="")


class DuplicateAlert(Exception):
    pass


class AlertNotFound(KeyError):
    pass


class Repository:
    def __init__(self, database_url: str) -> None:
        kwargs: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:  # Cloud SQL: drop dead connections (idle timeouts, instance restarts) instead of failing a request
            kwargs.update(pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5,
                          connect_args={"connect_timeout": 10})  # stopped instance: fail fast, don't hang startup
        self.engine = create_engine(database_url, **kwargs)
        self._session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Any]:
        with self._session.begin() as s:
            yield s

    # ---------------------------------------------------------------- alerts
    def create_alert(self, payload: dict[str, Any]) -> Alert:
        alert = Alert(
            alert_id=payload["alert_id"],
            status=Status.NEW,
            system=payload.get("system", ""),
            job_name=payload.get("job_name", ""),
            program=payload.get("program", ""),
            variant=payload.get("variant", ""),
            error_type=payload.get("error_type", ""),
            runtime_error=payload.get("runtime_error", ""),
            error_message=payload.get("error_message", ""),
            payload=payload,
        )
        try:
            with self.session() as s:
                s.add(alert)
                s.flush()
                s.add(AlertEvent(alert_id=alert.alert_id, to_status=Status.NEW, actor="xmatters", note="alert received"))
        except IntegrityError as e:
            raise DuplicateAlert(payload["alert_id"]) from e
        return alert

    def get(self, alert_id: str) -> Alert:
        with self.session() as s:
            alert = s.get(Alert, alert_id)
            if alert is None:
                raise AlertNotFound(alert_id)
            return alert

    def find_by_thread(self, channel: str, thread: str) -> Alert | None:
        with self.session() as s:
            return s.scalar(select(Alert).where(Alert.chat_channel == channel, Alert.chat_thread == thread))

    def list_alerts(self, statuses: list[str] | None = None, limit: int = 20) -> list[Alert]:
        with self.session() as s:
            q = select(Alert).order_by(Alert.received_at.desc()).limit(limit)
            if statuses:
                q = q.where(Alert.status.in_(statuses))
            return list(s.scalars(q))

    def history(self, alert_id: str) -> list[AlertEvent]:
        with self.session() as s:
            return list(s.scalars(select(AlertEvent).where(AlertEvent.alert_id == alert_id).order_by(AlertEvent.id)))

    def update_fields(self, alert_id: str, **fields: Any) -> Alert:
        """Update non-status fields (triage result, chat thread, ...)."""
        if "status" in fields:
            raise ValueError("use transition() to change status")
        with self.session() as s:
            alert = s.get(Alert, alert_id)
            if alert is None:
                raise AlertNotFound(alert_id)
            for k, v in fields.items():
                setattr(alert, k, v)
            return alert

    def transition(self, alert_id: str, new_status: str, actor: str = "system", note: str = "", **fields: Any) -> Alert:
        """Change status (validated against the lifecycle) and log it in alert_events, atomically."""
        with self.session() as s:
            alert = s.get(Alert, alert_id, with_for_update=True)
            if alert is None:
                raise AlertNotFound(alert_id)
            check_transition(alert.status, new_status)
            s.add(AlertEvent(alert_id=alert_id, actor=actor, from_status=alert.status, to_status=new_status, note=note))
            alert.status = new_status
            for k, v in fields.items():
                setattr(alert, k, v)
            return alert
