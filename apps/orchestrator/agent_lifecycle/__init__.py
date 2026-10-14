"""Phase 3 — Agent lifecycle (REGISTERED→READY→BUSY→DRAINING→OFFLINE)."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from execution.state_machine import (
    AgentLifecycleState,
    AgentLifecycleStateMachine,
    IllegalTransition,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentLifecycleRecord:
    agent_id: str
    state: str = AgentLifecycleState.REGISTERED.value
    last_seen: Optional[str] = None
    active_tasks: int = 0
    max_concurrency: int = 8
    load: float = 0.0
    version: Optional[str] = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    drain_started_at: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentLifecycleService:
    """In-memory lifecycle authority (DB wiring optional later)."""

    def __init__(self, event_bus=None):
        self._lock = threading.RLock()
        self._agents: dict[str, AgentLifecycleRecord] = {}
        self._events = event_bus

    def register(
        self,
        agent_id: str,
        *,
        version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        max_concurrency: int = 8,
    ) -> AgentLifecycleRecord:
        with self._lock:
            existing = self._agents.get(agent_id)
            if existing:
                existing.last_seen = _utc_now()
                existing.updated_at = existing.last_seen
                if version:
                    existing.version = version
                if capabilities:
                    existing.capabilities.update(capabilities)
                self._persist(existing)
                return AgentLifecycleRecord(**existing.to_dict())
            rec = AgentLifecycleRecord(
                agent_id=agent_id,
                state=AgentLifecycleState.REGISTERED.value,
                last_seen=_utc_now(),
                updated_at=_utc_now(),
                version=version,
                capabilities=dict(capabilities or {}),
                max_concurrency=max_concurrency,
            )
            self._agents[agent_id] = rec
            self._persist(rec)
            self._emit(AgentLifecycleStateMachine.event_for(AgentLifecycleState.REGISTERED), rec)
            return AgentLifecycleRecord(**rec.to_dict())

    def transition(self, agent_id: str, target: AgentLifecycleState | str) -> AgentLifecycleRecord:
        with self._lock:
            rec = self._require(agent_id)
            result = AgentLifecycleStateMachine.transition(rec.state, target)
            if result.changed:
                rec.state = result.state.value  # type: ignore[union-attr]
                rec.updated_at = _utc_now()
                if result.state == AgentLifecycleState.DRAINING:
                    rec.drain_started_at = rec.updated_at
                self._emit(result.event_type, rec)
            self._persist(rec)
            return AgentLifecycleRecord(**rec.to_dict())

    def mark_ready(self, agent_id: str) -> AgentLifecycleRecord:
        return self.transition(agent_id, AgentLifecycleState.READY)

    def heartbeat(
        self,
        agent_id: str,
        *,
        active_tasks: int | None = None,
        load: float | None = None,
        version: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> AgentLifecycleRecord:
        with self._lock:
            if self._cached(agent_id) is None:
                self.register(agent_id, version=version, capabilities=capabilities)
            rec = self._agents[agent_id]
            rec.last_seen = _utc_now()
            rec.updated_at = rec.last_seen
            if active_tasks is not None:
                rec.active_tasks = max(0, int(active_tasks))
            if load is not None:
                rec.load = float(load)
            if version:
                rec.version = version
            if capabilities:
                rec.capabilities.update(capabilities)
            # Auto READY/BUSY from heartbeat when not draining
            if rec.state == AgentLifecycleState.OFFLINE.value:
                # P4.18: OFFLINE → REGISTERED → READY (never OFFLINE → BUSY)
                try:
                    r1 = AgentLifecycleStateMachine.transition(
                        rec.state, AgentLifecycleState.REGISTERED
                    )
                    if r1.changed:
                        rec.state = r1.state.value  # type: ignore[union-attr]
                        self._emit(r1.event_type, rec)
                    r2 = AgentLifecycleStateMachine.transition(
                        rec.state, AgentLifecycleState.READY
                    )
                    if r2.changed:
                        rec.state = r2.state.value  # type: ignore[union-attr]
                        self._emit(r2.event_type, rec)
                except IllegalTransition:
                    pass
            elif rec.state not in {
                AgentLifecycleState.DRAINING.value,
            }:
                desired = (
                    AgentLifecycleState.BUSY
                    if rec.active_tasks > 0
                    else AgentLifecycleState.READY
                )
                if rec.state == AgentLifecycleState.REGISTERED.value:
                    desired = AgentLifecycleState.READY
                try:
                    result = AgentLifecycleStateMachine.transition(rec.state, desired)
                    if result.changed:
                        rec.state = result.state.value  # type: ignore[union-attr]
                        self._emit(result.event_type, rec)
                except IllegalTransition:
                    pass
            self._persist(rec)
            return AgentLifecycleRecord(**rec.to_dict())

    def drain(self, agent_id: str) -> AgentLifecycleRecord:
        rec = self.transition(agent_id, AgentLifecycleState.DRAINING)
        # If no active work, move to OFFLINE immediately
        with self._lock:
            live = self._agents[agent_id]
            if live.active_tasks <= 0 and live.state == AgentLifecycleState.DRAINING.value:
                return self.transition(agent_id, AgentLifecycleState.OFFLINE)
        return rec

    def on_task_started(self, agent_id: str) -> AgentLifecycleRecord:
        with self._lock:
            if self._cached(agent_id) is None:
                self.register(agent_id)
            rec = self._agents[agent_id]
            if not AgentLifecycleStateMachine.accepts_work(rec.state):
                raise IllegalTransition(
                    AgentLifecycleState(rec.state),
                    AgentLifecycleState.BUSY,
                    kind="agent_lifecycle",
                )
            rec.active_tasks += 1
            return self.transition(agent_id, AgentLifecycleState.BUSY)

    def on_task_finished(self, agent_id: str) -> AgentLifecycleRecord:
        with self._lock:
            rec = self._require(agent_id)
            rec.active_tasks = max(0, rec.active_tasks - 1)
            rec.updated_at = _utc_now()
            if rec.state == AgentLifecycleState.DRAINING.value and rec.active_tasks == 0:
                return self.transition(agent_id, AgentLifecycleState.OFFLINE)
            if rec.active_tasks == 0 and rec.state == AgentLifecycleState.BUSY.value:
                return self.transition(agent_id, AgentLifecycleState.READY)
            self._persist(rec)
            return AgentLifecycleRecord(**rec.to_dict())

    def health(self, agent_id: str, *, stale_after_s: float = 60.0) -> dict[str, Any]:
        with self._lock:
            rec = self._cached(agent_id)
            if not rec:
                return {
                    "agent_id": agent_id,
                    "healthy": False,
                    "state": None,
                    "reason": "not_registered",
                }
            stale = False
            if rec.last_seen:
                try:
                    age = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(rec.last_seen)
                    ).total_seconds()
                    stale = age > stale_after_s
                except ValueError:
                    stale = True
            accepts = AgentLifecycleStateMachine.accepts_work(rec.state) and not stale
            at_capacity = rec.active_tasks >= rec.max_concurrency
            return {
                "agent_id": agent_id,
                "healthy": accepts and not at_capacity,
                "state": rec.state,
                "last_seen": rec.last_seen,
                "active_tasks": rec.active_tasks,
                "max_concurrency": rec.max_concurrency,
                "load": rec.load,
                "version": rec.version,
                "capabilities": rec.capabilities,
                "stale": stale,
                "at_capacity": at_capacity,
                "accepts_work": accepts,
            }

    def accepts_delegation(self, agent_id: str) -> tuple[bool, str]:
        h = self.health(agent_id)
        if not h.get("state"):
            # Legacy agents without lifecycle still allowed (capability detection)
            return True, "legacy_untracked"
        if h["state"] == AgentLifecycleState.DRAINING.value:
            return False, "draining"
        if h["state"] == AgentLifecycleState.OFFLINE.value:
            return False, "offline"
        if h.get("stale"):
            return False, "heartbeat_stale"
        if h.get("at_capacity"):
            return False, "at_capacity"
        if not h.get("accepts_work"):
            return False, f"state:{h['state']}"
        return True, "ok"

    def mark_stale_offline(self, *, stale_after_s: float = 60.0) -> list[str]:
        """P4.8/P4.18: heartbeat timeout → OFFLINE for all stale agents."""
        offline: list[str] = []
        with self._lock:
            ids = list(self._agents.keys())
        for agent_id in ids:
            h = self.health(agent_id, stale_after_s=stale_after_s)
            if not h.get("stale"):
                continue
            if h.get("state") in {
                AgentLifecycleState.OFFLINE.value,
                None,
            }:
                continue
            try:
                with self._lock:
                    rec = self._cached(agent_id)
                    if not rec:
                        continue
                    if rec.state == AgentLifecycleState.OFFLINE.value:
                        continue
                    # Force OFFLINE via allowed path: DRAINING→OFFLINE or direct
                    if rec.state != AgentLifecycleState.DRAINING.value:
                        try:
                            AgentLifecycleStateMachine.transition(
                                rec.state, AgentLifecycleState.DRAINING
                            )
                            rec.state = AgentLifecycleState.DRAINING.value
                            self._emit("agent.draining", rec)
                        except IllegalTransition:
                            # REGISTERED can go OFFLINE? check machine — use DRAINING first
                            pass
                    try:
                        result = AgentLifecycleStateMachine.transition(
                            rec.state, AgentLifecycleState.OFFLINE
                        )
                        if result.changed:
                            rec.state = result.state.value  # type: ignore[union-attr]
                            rec.updated_at = _utc_now()
                            self._emit(result.event_type, rec)
                            self._persist(rec)
                            offline.append(agent_id)
                    except IllegalTransition:
                        # Last resort: set OFFLINE for recovery scanning
                        rec.state = AgentLifecycleState.OFFLINE.value
                        rec.updated_at = _utc_now()
                        self._emit("agent.offline", rec)
                        self._persist(rec)
                        offline.append(agent_id)
            except Exception:  # noqa: BLE001
                continue
        return offline

    def get(self, agent_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            rec = self._cached(agent_id)
            return rec.to_dict() if rec else None

    def list_agent_ids(self) -> list[str]:
        with self._lock:
            ids = set(self._agents.keys())
            ids.update(self._list_persisted_ids())
            return list(ids)

    def router_accepts(self, agent_id: str) -> tuple[bool, str]:
        """P4.15: READY/BUSY + healthy + capacity available."""
        h = self.health(agent_id)
        if not h.get("state"):
            return True, "legacy_untracked"
        state = h["state"]
        if state in {
            AgentLifecycleState.DRAINING.value,
            AgentLifecycleState.OFFLINE.value,
        }:
            return False, f"lifecycle:{state}"
        if h.get("stale"):
            return False, "heartbeat_stale"
        if state not in {
            AgentLifecycleState.READY.value,
            AgentLifecycleState.BUSY.value,
            AgentLifecycleState.REGISTERED.value,
        }:
            return False, f"lifecycle:{state}"
        if h.get("at_capacity"):
            return False, "at_capacity"
        return True, "ok"

    def _require(self, agent_id: str) -> AgentLifecycleRecord:
        rec = self._cached(agent_id)
        if not rec:
            raise KeyError(f"agent not registered: {agent_id}")
        return rec

    def _cached(self, agent_id: str) -> Optional[AgentLifecycleRecord]:
        rec = self._agents.get(agent_id)
        if rec:
            return rec
        loaded = self._load(agent_id)
        if loaded:
            self._agents[agent_id] = loaded
        return loaded

    def _load(self, agent_id: str) -> Optional[AgentLifecycleRecord]:
        return None

    def _list_persisted_ids(self) -> list[str]:
        return []

    def _persist(self, rec: AgentLifecycleRecord) -> None:
        return

    def _emit(self, event_type: str, rec: AgentLifecycleRecord) -> None:
        if not self._events:
            return
        from execution.events import ExecutionEvent

        self._events.emit(
            ExecutionEvent(
                event_type=event_type,
                agent_id=rec.agent_id,
                payload={
                    "state": rec.state,
                    "active_tasks": rec.active_tasks,
                    "max_concurrency": rec.max_concurrency,
                },
            )
        )


class PostgresAgentLifecycleService(AgentLifecycleService):
    """Write-through lifecycle so READY/OFFLINE survives orchestrator restart."""

    def __init__(self, event_bus=None, database_url: str | None = None):
        super().__init__(event_bus=event_bus)
        from defaults import database_url as resolve_database_url

        self.database_url = resolve_database_url(database_url)

    def _connect(self):
        from db import connect

        return connect(self.database_url)

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM a2a_agent_lifecycle LIMIT 0")

    def _persist(self, rec: AgentLifecycleRecord) -> None:
        from psycopg.types.json import Jsonb

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO a2a_agent_lifecycle (
                      agent_id, state, last_seen, active_tasks, max_concurrency,
                      load, version, capabilities, drain_started_at, updated_at, metadata
                    ) VALUES (
                      %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s::timestamptz, now(), %s
                    )
                    ON CONFLICT (agent_id) DO UPDATE SET
                      state = EXCLUDED.state,
                      last_seen = EXCLUDED.last_seen,
                      active_tasks = EXCLUDED.active_tasks,
                      max_concurrency = EXCLUDED.max_concurrency,
                      load = EXCLUDED.load,
                      version = EXCLUDED.version,
                      capabilities = EXCLUDED.capabilities,
                      drain_started_at = EXCLUDED.drain_started_at,
                      updated_at = now(),
                      metadata = EXCLUDED.metadata
                    """,
                    (
                        rec.agent_id,
                        rec.state,
                        rec.last_seen,
                        rec.active_tasks,
                        rec.max_concurrency,
                        rec.load,
                        rec.version,
                        Jsonb(rec.capabilities or {}),
                        rec.drain_started_at,
                        Jsonb(rec.metadata or {}),
                    ),
                )
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning(
                "agent lifecycle persist failed agent=%s", rec.agent_id, exc_info=True
            )

    def _load(self, agent_id: str) -> Optional[AgentLifecycleRecord]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM a2a_agent_lifecycle WHERE agent_id = %s",
                    (agent_id,),
                ).fetchone()
            if not row:
                return None
            return _lifecycle_from_row(dict(row))
        except Exception:  # noqa: BLE001
            return None

    def _list_persisted_ids(self) -> list[str]:
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT agent_id FROM a2a_agent_lifecycle").fetchall()
            return [str(r["agent_id"]) for r in rows]
        except Exception:  # noqa: BLE001
            return []


def _lifecycle_from_row(row: dict[str, Any]) -> AgentLifecycleRecord:
    def _ts(val: Any) -> Optional[str]:
        if val is None:
            return None
        if hasattr(val, "isoformat"):
            return val.isoformat()
        return str(val)

    return AgentLifecycleRecord(
        agent_id=str(row["agent_id"]),
        state=str(row.get("state") or AgentLifecycleState.REGISTERED.value),
        last_seen=_ts(row.get("last_seen")),
        active_tasks=int(row.get("active_tasks") or 0),
        max_concurrency=int(row.get("max_concurrency") or 8),
        load=float(row.get("load") or 0),
        version=row.get("version"),
        capabilities=dict(row.get("capabilities") or {}),
        drain_started_at=_ts(row.get("drain_started_at")),
        updated_at=_ts(row.get("updated_at")),
        metadata=dict(row.get("metadata") or {}),
    )


__all__ = [
    "AgentLifecycleService",
    "PostgresAgentLifecycleService",
    "AgentLifecycleRecord",
    "IllegalTransition",
]
