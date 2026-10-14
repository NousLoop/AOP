"""DAG task status maps onto OS ExecutionRecord without dual-write races."""

from __future__ import annotations

from execution import ExecutionService
from execution.dag_bridge import ensure_record, sync_from_dag
from execution.state_machine import ExecutionState


def test_ensure_record_idempotent():
    svc = ExecutionService()
    a = ensure_record(svc, "t-dag-1", metadata={"plane": "dag"})
    b = ensure_record(svc, "t-dag-1")
    assert a.task_id == b.task_id == "t-dag-1"
    assert a.state == ExecutionState.PENDING.value


def test_sync_running_then_completed():
    svc = ExecutionService()
    sync_from_dag(svc, "t-dag-2", "running")
    assert svc.get("t-dag-2").state == ExecutionState.RUNNING.value
    sync_from_dag(svc, "t-dag-2", "completed")
    assert svc.get("t-dag-2").state == ExecutionState.SUCCEEDED.value
    # Idempotent
    sync_from_dag(svc, "t-dag-2", "completed")
    assert svc.get("t-dag-2").state == ExecutionState.SUCCEEDED.value


def test_sync_failed_from_pending():
    svc = ExecutionService()
    sync_from_dag(svc, "t-dag-3", "failed", error="boom")
    rec = svc.get("t-dag-3")
    assert rec.state == ExecutionState.FAILED.value
    assert rec.error == "boom"


def test_states_for_tasks_batch():
    svc = ExecutionService()
    sync_from_dag(svc, "t-os-a", "running")
    sync_from_dag(svc, "t-os-b", "completed")
    states = svc.states_for_tasks(["t-os-a", "t-os-b", "missing"])
    assert states["t-os-a"] == ExecutionState.RUNNING.value
    assert states["t-os-b"] == ExecutionState.SUCCEEDED.value
    assert "missing" not in states


def test_try_sync_from_ctx_returns_record(monkeypatch):
    from execution import dag_bridge
    from execution.dag_bridge import try_sync_from_ctx

    svc = ExecutionService()
    monkeypatch.setattr(dag_bridge, "_ctx_execution", lambda: svc)
    rec = try_sync_from_ctx("t-sync-ctx", "running")
    assert rec is not None
    assert rec.state == ExecutionState.RUNNING.value


def test_sync_waiting_and_cancel():
    svc = ExecutionService()
    sync_from_dag(svc, "t-dag-4", "waiting_for_user")
    assert svc.get("t-dag-4").state == ExecutionState.WAITING.value
    sync_from_dag(svc, "t-dag-4", "cancelled")
    assert svc.get("t-dag-4").state == ExecutionState.CANCELLED.value
