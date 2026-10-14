"""Phase 3 — Execution Record store (source of truth).

Events / metrics / logs are derived from transitions on this store.
Supports an in-memory backend for hermetic tests and a Postgres backend for prod.
"""

from __future__ import annotations

from defaults import database_url as resolve_database_url

import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .state_machine import (
    ExecutionState,
    ExecutionStateMachine,
    IllegalTransition,
    RecoveryClass,
)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None

@dataclass
class ExecutionRecord:
    task_id: str
    operation: str = "execute"
    root_task_id: Optional[str] = None
    correlation_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    agent_id: Optional[str] = None
    state: str = ExecutionState.PENDING.value
    attempt: int = 0
    max_retries: int = 3
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: int = 0
    error: Optional[str] = None
    idempotency_key: Optional[str] = None
    branch_lineage: list[str] = field(default_factory=list)
    visited_agents: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    ownership_token: Optional[str] = None
    owner_id: Optional[str] = None
    lease_until: Optional[str] = None
    sequence: int = 0
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ExecutionRecord:
        def _j(v, default):
            if v is None:
                return default
            if isinstance(v, (list, dict)):
                return v
            return default

        started = row.get("started_at")
        updated = row.get("updated_at")
        finished = row.get("finished_at")
        lease = row.get("lease_until")
        return cls(
            id=row.get("id"),
            task_id=str(row["task_id"]),
            operation=str(row.get("operation") or "execute"),
            root_task_id=row.get("root_task_id"),
            correlation_id=row.get("correlation_id"),
            parent_task_id=row.get("parent_task_id"),
            agent_id=row.get("agent_id"),
            state=str(row.get("state") or ExecutionState.PENDING.value),
            attempt=int(row.get("attempt") or 0),
            max_retries=int(row.get("max_retries") or 3),
            started_at=_iso(started) if isinstance(started, datetime) else started,
            updated_at=_iso(updated) if isinstance(updated, datetime) else updated,
            finished_at=_iso(finished) if isinstance(finished, datetime) else finished,
            duration_ms=int(row.get("duration_ms") or 0),
            error=row.get("error"),
            idempotency_key=row.get("idempotency_key"),
            branch_lineage=list(_j(row.get("branch_lineage"), [])),
            visited_agents=dict(_j(row.get("visited_agents"), {})),
            metadata=dict(_j(row.get("metadata"), {})),
            ownership_token=row.get("ownership_token"),
            owner_id=row.get("owner_id"),
            lease_until=_iso(lease) if isinstance(lease, datetime) else lease,
            sequence=int(row.get("sequence") or 0),
            tenant_id=row.get("tenant_id"),
            user_id=row.get("user_id"),
            project_id=row.get("project_id"),
        )

class ExecutionRecordStore:
    """In-memory SoT used by unit tests and as fallback when DB is down."""

    def __init__(self):
        self._lock = threading.RLock()
        self._by_key: dict[tuple[str, str], ExecutionRecord] = {}
        self._by_idemp: dict[str, ExecutionRecord] = {}
        self._seq = 0

    def upsert(self, record: ExecutionRecord) -> ExecutionRecord:
        with self._lock:
            key = (record.task_id, record.operation)
            now = _utc_now().isoformat()
            existing = self._by_key.get(key)
            if existing is None and record.idempotency_key:
                existing = self._by_idemp.get(record.idempotency_key)
            if existing:
                # Idempotent create: return existing without regressing state
                return existing
            self._seq += 1
            record.id = self._seq
            record.updated_at = now
            if not record.started_at and record.state != ExecutionState.PENDING.value:
                record.started_at = now
            self._by_key[key] = record
            if record.idempotency_key:
                self._by_idemp[record.idempotency_key] = record
            return record

    def get(
        self, task_id: str, *, operation: str = "execute"
    ) -> Optional[ExecutionRecord]:
        with self._lock:
            rec = self._by_key.get((task_id, operation))
            return ExecutionRecord.from_row(rec.to_dict()) if rec else None

    def get_many(
        self, task_ids: list[str], *, operation: str = "execute"
    ) -> dict[str, ExecutionRecord]:
        out: dict[str, ExecutionRecord] = {}
        with self._lock:
            for tid in task_ids:
                rec = self._by_key.get((str(tid), operation))
                if rec:
                    out[str(tid)] = ExecutionRecord.from_row(rec.to_dict())
        return out

    def get_by_idempotency(self, key: str) -> Optional[ExecutionRecord]:
        with self._lock:
            rec = self._by_idemp.get(key)
            return ExecutionRecord.from_row(rec.to_dict()) if rec else None

    def list_for_root(self, root_task_id: str) -> list[ExecutionRecord]:
        with self._lock:
            return [
                ExecutionRecord.from_row(r.to_dict())
                for r in self._by_key.values()
                if r.root_task_id == root_task_id
            ]

    def transition(
        self,
        task_id: str,
        target: ExecutionState | str,
        *,
        operation: str = "execute",
        error: Optional[str] = None,
        allow_recover_retry: bool = False,
        bump_attempt: bool = False,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> tuple[ExecutionRecord, bool, str]:
        with self._lock:
            rec = self._by_key.get((task_id, operation))
            if rec is None:
                raise KeyError(f"execution record not found: {task_id}/{operation}")
            result = ExecutionStateMachine.transition(
                rec.state, target, allow_recover_retry=allow_recover_retry
            )
            if not result.changed:
                return ExecutionRecord.from_row(rec.to_dict()), False, result.event_type
            now = _utc_now()
            rec.state = result.state.value  # type: ignore[union-attr]
            rec.updated_at = now.isoformat()
            if bump_attempt or result.state == ExecutionState.RETRYING:
                rec.attempt += 1
            if result.state == ExecutionState.RUNNING and not rec.started_at:
                rec.started_at = now.isoformat()
            if error is not None:
                rec.error = error
            if metadata_patch:
                rec.metadata.update(metadata_patch)
            if ExecutionStateMachine.is_terminal(result.state):  # type: ignore[arg-type]
                rec.finished_at = now.isoformat()
                if rec.started_at:
                    try:
                        started = datetime.fromisoformat(rec.started_at)
                        rec.duration_ms = int((now - started).total_seconds() * 1000)
                    except ValueError:
                        pass
            return ExecutionRecord.from_row(rec.to_dict()), True, result.event_type

    def try_acquire_ownership(
        self,
        task_id: str,
        *,
        operation: str = "execute",
        token: Optional[str] = None,
        owner_id: Optional[str] = None,
        lease_seconds: float = 30.0,
    ) -> Optional[str]:
        """Acquire ownership if free or lease expired (Phase 4 lease semantics)."""
        with self._lock:
            rec = self._by_key.get((task_id, operation))
            if rec is None:
                return None
            now = _utc_now()
            if rec.ownership_token and rec.lease_until:
                try:
                    until = datetime.fromisoformat(rec.lease_until)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    if now <= until:
                        return None  # lease still held
                except ValueError:
                    return None
            elif rec.ownership_token and not rec.lease_until:
                return None
            tok = token or uuid.uuid4().hex
            rec.ownership_token = tok
            rec.owner_id = owner_id
            from datetime import timedelta
            rec.lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            rec.updated_at = now.isoformat()
            return tok

    def release_ownership(
        self, task_id: str, token: str, *, operation: str = "execute"
    ) -> bool:
        with self._lock:
            rec = self._by_key.get((task_id, operation))
            if rec is None or rec.ownership_token != token:
                return False
            rec.ownership_token = None
            rec.owner_id = None
            rec.lease_until = None
            return True

    def renew_lease(
        self,
        task_id: str,
        token: str,
        *,
        operation: str = "execute",
        lease_seconds: float = 30.0,
    ) -> bool:
        with self._lock:
            rec = self._by_key.get((task_id, operation))
            if rec is None or rec.ownership_token != token:
                return False
            from datetime import timedelta
            now = _utc_now()
            rec.lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            rec.updated_at = now.isoformat()
            return True

    def list_stale(
        self, *, stale_after_s: float = 30.0, limit: int = 50
    ) -> list[ExecutionRecord]:
        """RUNNING/WAITING/RETRYING with expired lease or old updated_at."""
        with self._lock:
            now = _utc_now()
            out = []
            for rec in self._by_key.values():
                if rec.state not in {
                    ExecutionState.RUNNING.value,
                    ExecutionState.WAITING.value,
                    ExecutionState.RETRYING.value,
                }:
                    continue
                expired = False
                if rec.lease_until:
                    try:
                        until = datetime.fromisoformat(rec.lease_until)
                        if until.tzinfo is None:
                            until = until.replace(tzinfo=timezone.utc)
                        expired = now > until
                    except ValueError:
                        expired = True
                elif rec.updated_at:
                    try:
                        upd = datetime.fromisoformat(rec.updated_at)
                        if upd.tzinfo is None:
                            upd = upd.replace(tzinfo=timezone.utc)
                        expired = (now - upd).total_seconds() > stale_after_s
                    except ValueError:
                        expired = True
                if expired:
                    out.append(ExecutionRecord.from_row(rec.to_dict()))
                if len(out) >= limit:
                    break
            return out

    def classify(self, task_id: str, *, operation: str = "execute",
                 stale_after_s: float = 120.0) -> dict[str, Any]:
        rec = self.get(task_id, operation=operation)
        if rec is None:
            return {"class": RecoveryClass.UNKNOWN.value, "record": None}
        stale = False
        now = _utc_now()
        if rec.lease_until:
            try:
                until = datetime.fromisoformat(rec.lease_until)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                stale = now > until and rec.state == ExecutionState.RUNNING.value
            except ValueError:
                stale = True
        elif rec.state == ExecutionState.RUNNING.value and rec.updated_at:
            try:
                age = (now - datetime.fromisoformat(rec.updated_at)).total_seconds()
                stale = age > stale_after_s
            except ValueError:
                stale = True
        remaining = rec.attempt < rec.max_retries
        klass = ExecutionStateMachine.recovery_class(
            rec.state, stale=stale, attempts_remaining=remaining
        )
        return {"class": klass.value, "record": rec.to_dict(), "stale": stale}

class PostgresExecutionRecordStore:
    """DB-backed SoT. Falls back is handled by ExecutionService."""

    _txn_outbox = True  # transitions write outbox in the same DB transaction

    def __init__(self, database_url: str | None = None):
        self.database_url = resolve_database_url(database_url)

    def _connect(self):
        from db import connect

        return connect(self.database_url)

    def upsert(self, record: ExecutionRecord) -> ExecutionRecord:
        from psycopg.types.json import Jsonb

        sql = """
            INSERT INTO a2a_execution_records (
              task_id, root_task_id, correlation_id, parent_task_id, agent_id,
              operation, state, attempt, max_retries, started_at, updated_at,
              finished_at, duration_ms, error, idempotency_key, branch_lineage,
              visited_agents, metadata, ownership_token, tenant_id
            ) VALUES (
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
            ON CONFLICT (task_id, operation) DO UPDATE
              SET updated_at = a2a_execution_records.updated_at
            RETURNING *
        """
        with self._connect() as conn:
            row = conn.execute(
                sql,
                (
                    record.task_id,
                    record.root_task_id,
                    record.correlation_id,
                    record.parent_task_id,
                    record.agent_id,
                    record.operation,
                    record.state,
                    record.attempt,
                    record.max_retries,
                    record.started_at,
                    record.finished_at,
                    record.duration_ms,
                    record.error,
                    record.idempotency_key,
                    Jsonb(record.branch_lineage),
                    Jsonb(record.visited_agents),
                    Jsonb(record.metadata),
                    record.ownership_token,
                    record.tenant_id,
                ),
            ).fetchone()
        return ExecutionRecord.from_row(dict(row))

    def get(self, task_id: str, *, operation: str = "execute") -> Optional[ExecutionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM a2a_execution_records WHERE task_id=%s AND operation=%s",
                (task_id, operation),
            ).fetchone()
        return ExecutionRecord.from_row(dict(row)) if row else None

    def get_many(
        self, task_ids: list[str], *, operation: str = "execute"
    ) -> dict[str, ExecutionRecord]:
        ids = [str(t) for t in task_ids if t]
        if not ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM a2a_execution_records
                WHERE operation = %s AND task_id = ANY(%s)
                """,
                (operation, ids),
            ).fetchall()
        return {
            str(r["task_id"]): ExecutionRecord.from_row(dict(r))
            for r in rows
            if r.get("task_id")
        }

    def get_by_idempotency(self, key: str) -> Optional[ExecutionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM a2a_execution_records WHERE idempotency_key=%s",
                (key,),
            ).fetchone()
        return ExecutionRecord.from_row(dict(row)) if row else None

    def list_for_root(self, root_task_id: str) -> list[ExecutionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM a2a_execution_records WHERE root_task_id=%s ORDER BY id",
                (root_task_id,),
            ).fetchall()
        return [ExecutionRecord.from_row(dict(r)) for r in rows]

    def transition(
        self,
        task_id: str,
        target: ExecutionState | str,
        *,
        operation: str = "execute",
        error: Optional[str] = None,
        allow_recover_retry: bool = False,
        bump_attempt: bool = False,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> tuple[ExecutionRecord, bool, str]:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT * FROM a2a_execution_records
                     WHERE task_id=%s AND operation=%s
                     FOR UPDATE
                    """,
                    (task_id, operation),
                ).fetchone()
                if not row:
                    raise KeyError(f"execution record not found: {task_id}/{operation}")
                rec = ExecutionRecord.from_row(dict(row))
                result = ExecutionStateMachine.transition(
                    rec.state, target, allow_recover_retry=allow_recover_retry
                )
                if not result.changed:
                    return rec, False, result.event_type
                attempt = rec.attempt + (
                    1 if bump_attempt or result.state == ExecutionState.RETRYING else 0
                )
                now = _utc_now()
                finished = None
                duration = rec.duration_ms
                started = rec.started_at
                if result.state == ExecutionState.RUNNING and not started:
                    started = now.isoformat()
                if ExecutionStateMachine.is_terminal(result.state):  # type: ignore[arg-type]
                    finished = now.isoformat()
                    if started:
                        try:
                            duration = int(
                                (now - datetime.fromisoformat(started)).total_seconds() * 1000
                            )
                        except ValueError:
                            pass
                meta = dict(rec.metadata)
                if metadata_patch:
                    meta.update(metadata_patch)
                seq = int(rec.sequence or 0) + 1
                updated = conn.execute(
                    """
                    UPDATE a2a_execution_records SET
                      state=%s, attempt=%s, updated_at=now(), started_at=COALESCE(%s::timestamptz, started_at),
                      finished_at=%s, duration_ms=%s, error=COALESCE(%s, error),
                      metadata=%s, sequence=%s
                     WHERE task_id=%s AND operation=%s
                    RETURNING *
                    """,
                    (
                        result.state.value,  # type: ignore[union-attr]
                        attempt,
                        started,
                        finished,
                        duration,
                        error,
                        Jsonb(meta),
                        seq,
                        task_id,
                        operation,
                    ),
                ).fetchone()
                out_rec = ExecutionRecord.from_row(dict(updated))
                # Same transaction: outbox event (P4.4 / P4.22)
                try:
                    from .outbox_bridge import write_execution_outbox
                    import uuid as _uuid

                    event_id = _uuid.uuid4().hex
                    write_execution_outbox(
                        conn,
                        event_id=event_id,
                        event_type=result.event_type,
                        root_task_id=out_rec.root_task_id,
                        correlation_id=out_rec.correlation_id,
                        task_id=out_rec.task_id,
                        agent_id=out_rec.agent_id,
                        sequence=seq,
                        payload={
                            "state": out_rec.state,
                            "attempt": out_rec.attempt,
                            "operation": operation,
                            "error": error,
                        },
                    )
                except Exception:  # noqa: BLE001
                    # Schema may lack Phase 4 columns in hermetic envs — still
                    # commit the record transition; EventBus covers local path.
                    pass
                return out_rec, True, result.event_type

    def try_acquire_ownership(
        self,
        task_id: str,
        *,
        operation: str = "execute",
        token: Optional[str] = None,
        owner_id: Optional[str] = None,
        lease_seconds: float = 30.0,
    ) -> Optional[str]:
        """CAS ownership with row lock; steal only when lease expired."""
        tok = token or uuid.uuid4().hex
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT ownership_token, lease_until, state
                      FROM a2a_execution_records
                     WHERE task_id=%s AND operation=%s
                     FOR UPDATE
                    """,
                    (task_id, operation),
                ).fetchone()
                if not row:
                    return None
                now = _utc_now()
                held = row.get("ownership_token")
                lease = row.get("lease_until")
                if held:
                    if lease is not None:
                        until = lease if getattr(lease, "tzinfo", None) else (
                            lease.replace(tzinfo=timezone.utc) if hasattr(lease, "replace") else None
                        )
                        if until is not None and now <= until:
                            return None
                    else:
                        # legacy token without lease — treat as held
                        return None
                updated = conn.execute(
                    """
                    UPDATE a2a_execution_records
                       SET ownership_token=%s,
                           owner_id=%s,
                           lease_until=now() + (%s || ' seconds')::interval,
                           updated_at=now()
                     WHERE task_id=%s AND operation=%s
                    RETURNING ownership_token
                    """,
                    (tok, owner_id, str(lease_seconds), task_id, operation),
                ).fetchone()
                return updated["ownership_token"] if updated else None

    def renew_lease(
        self,
        task_id: str,
        token: str,
        *,
        operation: str = "execute",
        lease_seconds: float = 30.0,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE a2a_execution_records
                   SET lease_until=now() + (%s || ' seconds')::interval,
                       updated_at=now()
                 WHERE task_id=%s AND operation=%s AND ownership_token=%s
                RETURNING id
                """,
                (str(lease_seconds), task_id, operation, token),
            ).fetchone()
        return bool(row)

    def release_ownership(
        self, task_id: str, token: str, *, operation: str = "execute"
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE a2a_execution_records
                   SET ownership_token=NULL, owner_id=NULL, lease_until=NULL, updated_at=now()
                 WHERE task_id=%s AND operation=%s AND ownership_token=%s
                RETURNING id
                """,
                (task_id, operation, token),
            ).fetchone()
        return bool(row)

    def list_stale(self, *, stale_after_s: float = 30.0, limit: int = 50) -> list[ExecutionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM a2a_execution_records
                 WHERE state IN ('RUNNING','WAITING','RETRYING')
                   AND (
                     (lease_until IS NOT NULL AND lease_until < now())
                     OR (lease_until IS NULL AND updated_at < now() - (%s || ' seconds')::interval)
                   )
                 ORDER BY updated_at ASC
                 LIMIT %s
                """,
                (str(stale_after_s), limit),
            ).fetchall()
        return [ExecutionRecord.from_row(dict(r)) for r in rows]

    def classify(self, task_id: str, *, operation: str = "execute",
                 stale_after_s: float = 120.0) -> dict[str, Any]:
        rec = self.get(task_id, operation=operation)
        if rec is None:
            return {"class": RecoveryClass.UNKNOWN.value, "record": None}
        stale = False
        now = _utc_now()
        if rec.lease_until and rec.state == ExecutionState.RUNNING.value:
            try:
                until = rec.lease_until
                if isinstance(until, str):
                    until = datetime.fromisoformat(until)
                if getattr(until, "tzinfo", None) is None and hasattr(until, "replace"):
                    until = until.replace(tzinfo=timezone.utc)
                stale = now > until
            except (ValueError, TypeError):
                stale = True
        elif rec.state == ExecutionState.RUNNING.value and rec.updated_at:
            try:
                age = (now - datetime.fromisoformat(rec.updated_at)).total_seconds()
                stale = age > stale_after_s
            except ValueError:
                stale = True
        remaining = rec.attempt < rec.max_retries
        klass = ExecutionStateMachine.recovery_class(
            rec.state, stale=stale, attempts_remaining=remaining
        )
        return {"class": klass.value, "record": rec.to_dict(), "stale": stale}

__all__ = [
    "ExecutionRecord",
    "ExecutionRecordStore",
    "PostgresExecutionRecordStore",
    "IllegalTransition",
]
