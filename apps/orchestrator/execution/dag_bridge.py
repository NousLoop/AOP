"""Keep OS ExecutionRecord in lockstep with the Console DAG task.

The DAG (tasks / task_nodes / Worker / Outbox) stays the enqueue path.
ExecutionRecord is the OS SoT for recover, collaboration views, and cost.
"""

from __future__ import annotations

import logging
from typing import Any

from .service import ExecutionService
from .state_machine import ExecutionState, ExecutionStateMachine, IllegalTransition

logger = logging.getLogger(__name__)

_DAG_SUCCESS = frozenset({"completed", "success"})
_DAG_FAILED = frozenset({"failed"})
_DAG_CANCELLED = frozenset({"cancelled", "canceled"})
_DAG_WAITING = frozenset({"waiting_for_user", "waiting_for_agent"})
_DAG_RUNNING = frozenset({"running", "planning", "retrying", "queued"})


def _ctx_execution() -> ExecutionService | None:
    try:
        from app_context import ctx

        return getattr(ctx, "execution", None)
    except Exception:  # noqa: BLE001
        return None


def ensure_record(
    svc: ExecutionService,
    task_id: str,
    *,
    tenant_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    existing = svc.get(task_id)
    if existing is not None:
        return existing
    meta = {"plane": "dag", **dict(metadata or {})}
    return svc.create(
        task_id=task_id,
        root_task_id=task_id,
        correlation_id=task_id,
        tenant_id=tenant_id,
        metadata=meta,
        idempotency_key=f"dag:{task_id}",
    )


def sync_from_dag(
    svc: ExecutionService,
    task_id: str,
    dag_status: str | None,
    *,
    error: str | None = None,
    tenant_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Map a DAG ``tasks.status`` onto the OS execution record. Idempotent."""
    if not task_id:
        return None
    rec = ensure_record(svc, task_id, tenant_id=tenant_id, metadata=metadata)
    st = (dag_status or "").strip().lower()
    cur = rec.state
    try:
        if st in _DAG_SUCCESS:
            if cur == ExecutionState.PENDING.value:
                rec = svc.start(task_id)
            if not ExecutionStateMachine.is_terminal(rec.state):
                rec = svc.succeed(task_id)
        elif st in _DAG_FAILED:
            if not ExecutionStateMachine.is_terminal(cur):
                rec = svc.fail(task_id, error or "dag failed")
        elif st in _DAG_CANCELLED:
            if not ExecutionStateMachine.is_terminal(cur):
                rec = svc.cancel(task_id)
        elif st in _DAG_WAITING:
            if cur == ExecutionState.PENDING.value or cur == ExecutionState.RUNNING.value:
                rec = svc.transition(task_id, ExecutionState.WAITING)
        elif st in _DAG_RUNNING or not st:
            if cur in {
                ExecutionState.PENDING.value,
                ExecutionState.RETRYING.value,
                ExecutionState.WAITING.value,
            }:
                rec = svc.start(task_id)
    except (IllegalTransition, KeyError) as exc:
        logger.debug("dag bridge skip task=%s dag=%s os=%s: %s", task_id, st, cur, exc)
    return rec


def try_sync_from_ctx(
    task_id: str,
    dag_status: str | None,
    *,
    error: str | None = None,
    tenant_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    svc = _ctx_execution()
    if svc is None:
        return None
    try:
        return sync_from_dag(
            svc,
            task_id,
            dag_status,
            error=error,
            tenant_id=tenant_id,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001
        logger.warning("dag bridge sync failed task=%s", task_id, exc_info=True)
        return None
