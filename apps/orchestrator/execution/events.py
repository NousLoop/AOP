"""Phase 3 — unified execution events (propagation layer)."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionEvent:
    event_type: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=_utc_now)
    root_task_id: Optional[str] = None
    correlation_id: Optional[str] = None
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    execution_id: Optional[str] = None
    sequence: Optional[int] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Keep sequence in payload for backward compatibility with collab WS clients
        if data.get("sequence") is not None:
            payload = dict(data.get("payload") or {})
            payload.setdefault("sequence", data["sequence"])
            data["payload"] = payload
        return data


class ExecutionEventBus:
    """In-memory ordered event log + optional fan-out hooks.

    Phase 4: assigns per-root ``sequence`` for collaboration stream ordering.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._events: list[ExecutionEvent] = []
        self._seen_ids: set[str] = set()
        self._hooks: list = []
        self._root_seq: dict[str, int] = {}

    def on_emit(self, hook) -> None:
        self._hooks.append(hook)

    def emit(self, event: ExecutionEvent | dict[str, Any]) -> ExecutionEvent:
        if isinstance(event, dict):
            event = ExecutionEvent(**{k: v for k, v in event.items() if k in ExecutionEvent.__dataclass_fields__})
        with self._lock:
            if event.event_id in self._seen_ids:
                # Idempotent: duplicate event_id is a no-op
                for e in self._events:
                    if e.event_id == event.event_id:
                        return e
            self._seen_ids.add(event.event_id)
            root = event.root_task_id or event.task_id or "_"
            seq = self._root_seq.get(root, 0) + 1
            self._root_seq[root] = seq
            event.sequence = seq
            if "sequence" not in (event.payload or {}):
                event.payload = {**(event.payload or {}), "sequence": seq}
            self._events.append(event)
        for hook in list(self._hooks):
            try:
                hook(event)
            except Exception:  # noqa: BLE001 - hooks must not break emission
                pass
        return event

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._events if e.task_id == task_id]

    def list_for_root(self, root_task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._events if e.root_task_id == root_task_id]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._seen_ids.clear()
            self._root_seq.clear()


class PostgresExecutionEventBus(ExecutionEventBus):
    """Write-through event log. Queries Postgres so collaboration views survive restart."""

    def __init__(self, database_url: str | None = None):
        super().__init__()
        from defaults import database_url as resolve_database_url

        self.database_url = resolve_database_url(database_url)

    def _connect(self):
        from db import connect

        return connect(self.database_url)

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM a2a_execution_events LIMIT 0")

    def emit(self, event: ExecutionEvent | dict[str, Any]) -> ExecutionEvent:
        if isinstance(event, dict):
            event = ExecutionEvent(
                **{k: v for k, v in event.items() if k in ExecutionEvent.__dataclass_fields__}
            )
        with self._lock:
            if event.event_id in self._seen_ids:
                for e in self._events:
                    if e.event_id == event.event_id:
                        return e
            persisted = self._insert(event)
            event.sequence = persisted.sequence
            if "sequence" not in (event.payload or {}):
                event.payload = {**(event.payload or {}), "sequence": event.sequence}
            self._seen_ids.add(event.event_id)
            self._events.append(event)
        for hook in list(self._hooks):
            try:
                hook(event)
            except Exception:  # noqa: BLE001
                pass
        return event

    def _insert(self, event: ExecutionEvent) -> ExecutionEvent:
        from psycopg.types.json import Jsonb

        root = event.root_task_id or event.task_id
        try:
            with self._connect() as conn:
                with conn.transaction():
                    seq_row = conn.execute(
                        """
                        SELECT COALESCE(MAX(sequence), 0) + 1 AS seq
                        FROM a2a_execution_events
                        WHERE COALESCE(root_task_id, task_id, '') = %s
                        """,
                        (root or "",),
                    ).fetchone()
                    seq = int((seq_row or {}).get("seq") or 1)
                    payload = dict(event.payload or {})
                    payload["sequence"] = seq
                    if event.parent_agent_id:
                        payload.setdefault("parent_agent_id", event.parent_agent_id)
                    if event.execution_id:
                        payload.setdefault("execution_id", event.execution_id)
                    row = conn.execute(
                        """
                        INSERT INTO a2a_execution_events (
                          event_id, event_type, timestamp, root_task_id, correlation_id,
                          task_id, agent_id, parent_task_id, payload, sequence
                        ) VALUES (
                          %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (event_id) DO NOTHING
                        RETURNING *
                        """,
                        (
                            event.event_id,
                            event.event_type,
                            event.timestamp,
                            event.root_task_id,
                            event.correlation_id,
                            event.task_id,
                            event.agent_id,
                            event.parent_task_id,
                            Jsonb(payload),
                            seq,
                        ),
                    ).fetchone()
                    if row is None:
                        row = conn.execute(
                            "SELECT * FROM a2a_execution_events WHERE event_id = %s",
                            (event.event_id,),
                        ).fetchone()
            if row:
                return _event_from_row(dict(row))
        except Exception:  # noqa: BLE001
            logger.warning("execution event persist failed id=%s", event.event_id, exc_info=True)
        root_key = event.root_task_id or event.task_id or "_"
        seq = self._root_seq.get(root_key, 0) + 1
        self._root_seq[root_key] = seq
        event.sequence = seq
        return event

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM a2a_execution_events
                    WHERE task_id = %s
                    ORDER BY COALESCE(sequence, id), id
                    """,
                    (task_id,),
                ).fetchall()
            if rows:
                return [_event_from_row(dict(r)).to_dict() for r in rows]
        except Exception:  # noqa: BLE001
            pass
        return super().list_for_task(task_id)

    def list_for_root(self, root_task_id: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM a2a_execution_events
                    WHERE root_task_id = %s OR (root_task_id IS NULL AND task_id = %s)
                    ORDER BY COALESCE(sequence, id), id
                    """,
                    (root_task_id, root_task_id),
                ).fetchall()
            return [_event_from_row(dict(r)).to_dict() for r in rows]
        except Exception:  # noqa: BLE001
            return super().list_for_root(root_task_id)


def _event_from_row(row: dict[str, Any]) -> ExecutionEvent:
    ts = row.get("timestamp")
    if hasattr(ts, "isoformat"):
        ts = ts.isoformat()
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    seq = row.get("sequence")
    if seq is not None:
        payload.setdefault("sequence", int(seq))
    return ExecutionEvent(
        event_type=str(row.get("event_type") or ""),
        event_id=str(row.get("event_id") or ""),
        timestamp=str(ts or _utc_now()),
        root_task_id=row.get("root_task_id"),
        correlation_id=row.get("correlation_id"),
        task_id=row.get("task_id"),
        agent_id=row.get("agent_id"),
        parent_task_id=row.get("parent_task_id"),
        parent_agent_id=payload.get("parent_agent_id"),
        execution_id=payload.get("execution_id"),
        sequence=int(seq) if seq is not None else payload.get("sequence"),
        payload=payload,
    )


# Canonical event type constants
TASK_CREATED = "task.created"
TASK_STARTED = "task.started"
TASK_WAITING = "task.waiting"
TASK_RETRYING = "task.retrying"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
TASK_TIMEOUT = "task.timeout"
TASK_CANCELLED = "task.cancelled"

DELEGATION_REQUESTED = "delegation.requested"
DELEGATION_ACCEPTED = "delegation.accepted"
DELEGATION_STARTED = "delegation.started"
DELEGATION_COMPLETED = "delegation.completed"
DELEGATION_FAILED = "delegation.failed"
DELEGATION_RELEASED = "delegation.released"

AGENT_REGISTERED = "agent.registered"
AGENT_READY = "agent.ready"
AGENT_BUSY = "agent.busy"
AGENT_DRAINING = "agent.draining"
AGENT_OFFLINE = "agent.offline"

# Visual Runtime observation types (emitted by observation layer / discovery hooks)
AGENT_DISCOVERED = "agent.discovered"
AGENT_SELECTED = "agent.selected"
AGENT_DELEGATED = "agent.delegated"
AGENT_STARTED = "agent.started"
AGENT_WAITING = "agent.waiting"
AGENT_COMPLETED = "agent.completed"
AGENT_FAILED = "agent.failed"
AGENT_RETRYING = "agent.retrying"
AGENT_RECOVERED = "agent.recovered"
TOOL_CALLED = "tool.called"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"


__all__ = [
    "ExecutionEvent",
    "ExecutionEventBus",
    "TASK_CREATED",
    "TASK_STARTED",
    "TASK_WAITING",
    "TASK_RETRYING",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_TIMEOUT",
    "TASK_CANCELLED",
    "DELEGATION_REQUESTED",
    "DELEGATION_ACCEPTED",
    "DELEGATION_STARTED",
    "DELEGATION_COMPLETED",
    "DELEGATION_FAILED",
    "DELEGATION_RELEASED",
    "AGENT_REGISTERED",
    "AGENT_READY",
    "AGENT_BUSY",
    "AGENT_DRAINING",
    "AGENT_OFFLINE",
    "AGENT_DISCOVERED",
    "AGENT_SELECTED",
    "AGENT_DELEGATED",
    "AGENT_STARTED",
    "AGENT_WAITING",
    "AGENT_COMPLETED",
    "AGENT_FAILED",
    "AGENT_RETRYING",
    "AGENT_RECOVERED",
    "TOOL_CALLED",
    "TOOL_COMPLETED",
    "TOOL_FAILED",
]
