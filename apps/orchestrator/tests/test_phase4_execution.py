"""Phase 4 — durable execution plane hermetic tests."""

from __future__ import annotations

import os

import pytest

# Force memory before importing execution bits that read env
os.environ["EXECUTION_STORE"] = "memory"
os.environ["EXECUTION_OUTBOX_ENABLED"] = "false"
os.environ["EXECUTION_RECOVERY_ENABLED"] = "false"

from execution import (
    CapacityQueue,
    ExecutionConfig,
    ExecutionService,
    RecoveryScheduler,
    RetryPolicy,
    build_execution_service,
    cancel_fanout,
)
from execution.record import ExecutionRecordStore
from execution.state_machine import AgentLifecycleStateMachine, ExecutionState
from agent_lifecycle import AgentLifecycleService
from router.filter import CandidateFilter, TaskRequirements


def test_factory_defaults_to_memory_when_env_memory():
    svc = build_execution_service(ExecutionConfig(store="memory", outbox_enabled=False))
    assert isinstance(svc.store, ExecutionRecordStore)


def test_factory_postgres_unavailable_raises(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("execution.factory.PostgresExecutionRecordStore", Boom)
    with pytest.raises(RuntimeError, match="Postgres execution store unavailable"):
        build_execution_service(ExecutionConfig(store="postgres"), force_memory=False)


def test_lease_ownership_contention():
    svc = ExecutionService(
        retry_policy=RetryPolicy(max_retries=3),
        config=ExecutionConfig(store="memory", outbox_enabled=False, lease_duration_s=30),
    )
    svc.create(task_id="lease-1", root_task_id="lease-1")
    svc.start("lease-1")
    # Force stale so recover can claim
    tok1 = svc.store.try_acquire_ownership("lease-1", owner_id="w1", lease_seconds=60)
    assert tok1
    tok2 = svc.store.try_acquire_ownership("lease-1", owner_id="w2", lease_seconds=60)
    assert tok2 is None  # lease held
    assert svc.store.release_ownership("lease-1", tok1)
    tok3 = svc.store.try_acquire_ownership("lease-1", owner_id="w2", lease_seconds=60)
    assert tok3


def test_lease_steal_after_expiry():
    svc = ExecutionService(config=ExecutionConfig(store="memory", outbox_enabled=False))
    svc.create(task_id="lease-2", root_task_id="lease-2")
    svc.start("lease-2")
    tok1 = svc.store.try_acquire_ownership("lease-2", owner_id="w1", lease_seconds=0.001)
    assert tok1
    import time

    time.sleep(0.01)
    tok2 = svc.store.try_acquire_ownership("lease-2", owner_id="w2", lease_seconds=30)
    assert tok2 and tok2 != tok1


def test_recover_pending_starts():
    svc = ExecutionService(
        retry_policy=RetryPolicy(max_retries=3),
        config=ExecutionConfig(store="memory", outbox_enabled=False, lease_duration_s=30),
    )
    svc.create(task_id="r-pending", root_task_id="r-pending")
    out = svc.recover("r-pending", stale_after_s=0, owner_id="rec-p")
    assert out["action"] == "start"
    assert svc.get("r-pending").state == "RUNNING"


def test_recover_uses_lease_owner():
    svc = ExecutionService(
        retry_policy=RetryPolicy(max_retries=3),
        config=ExecutionConfig(
            store="memory", outbox_enabled=False, lease_duration_s=30, owner_id="rec-a"
        ),
    )
    svc.create(task_id="r1", root_task_id="r1")
    svc.start("r1")
    out = svc.recover("r1", stale_after_s=0, owner_id="rec-a")
    assert out["action"] == "retry"
    assert out.get("ownership_token")
    out2 = svc.recover("r1", stale_after_s=0, owner_id="rec-b")
    assert out2["action"] == "contention"


def test_cancel_fanout_walks_graph():
    class FakeGraph:
        def list_edges(self, root):
            return [
                {"task_id": "b", "parent_task_id": "a", "target_agent_id": "B"},
                {"task_id": "c", "parent_task_id": "b", "target_agent_id": "C"},
            ]

    svc = ExecutionService(config=ExecutionConfig(store="memory", outbox_enabled=False))
    for tid in ("a", "b", "c"):
        svc.create(task_id=tid, root_task_id="a", parent_task_id=("a" if tid != "a" else None))
        svc.start(tid)
    cancelled_a2a = []

    def a2a_cancel(ep, tid):
        cancelled_a2a.append((ep, tid))
        return True

    result = cancel_fanout(
        root_task_id="a",
        execution_service=svc,
        runtime_graph=FakeGraph(),
        a2a_cancel=a2a_cancel,
        agent_endpoints={"B": "http://b", "C": "http://c"},
    )
    assert len(result["cancelled"]) >= 3
    assert set(result["graph_children"]) == {"b", "c"}
    assert {t for _, t in cancelled_a2a} >= {"b", "c"}


def test_cancel_fanout_extra_a2a_targets():
    svc = ExecutionService(config=ExecutionConfig(store="memory", outbox_enabled=False))
    svc.create(task_id="root-x", root_task_id="root-x")
    svc.start("root-x")
    seen = []

    def a2a_cancel(ep, tid):
        seen.append((ep, tid))
        return True

    result = cancel_fanout(
        root_task_id="root-x",
        execution_service=svc,
        a2a_cancel=a2a_cancel,
        extra_a2a_targets=[
            {"endpoint": "http://harness", "task_id": "root-x", "agent_id": "H"},
            {"endpoint": "http://harness", "task_id": "a2a-uuid", "agent_id": "H"},
        ],
    )
    assert ("http://harness", "root-x") in seen
    assert ("http://harness", "a2a-uuid") in seen
    assert all(r["ok"] for r in result["a2a_cancel"])


def test_capacity_queue_fifo_and_depth():
    q = CapacityQueue(max_depth=2)
    a = q.enqueue(task_id="t1", agent_id="B", priority=100)
    b = q.enqueue(task_id="t2", agent_id="B", priority=50)  # higher priority
    assert a["queued"] and b["queued"]
    reject = q.enqueue(task_id="t3", agent_id="B")
    assert reject.get("rejected")
    first = q.dequeue("B")
    assert first and first.task_id == "t2"  # priority 50 first


def test_event_sequence_per_root():
    svc = ExecutionService(config=ExecutionConfig(store="memory", outbox_enabled=False))
    svc.create(task_id="s1", root_task_id="root-s")
    svc.start("s1")
    svc.succeed("s1")
    events = svc.events.list_for_root("root-s")
    seqs = [e["payload"]["sequence"] for e in events]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1


def test_recovery_scheduler_tick():
    svc = ExecutionService(
        retry_policy=RetryPolicy(max_retries=3),
        config=ExecutionConfig(store="memory", outbox_enabled=False),
    )
    svc.create(task_id="stale-1", root_task_id="stale-1")
    svc.start("stale-1")
    # Acquire with expired lease so list_stale finds it
    svc.store.try_acquire_ownership("stale-1", lease_seconds=0.0)
    import time

    time.sleep(0.01)
    life = AgentLifecycleService(event_bus=svc.events)
    life.register("agent-x")
    life.mark_ready("agent-x")
    sched = RecoveryScheduler(svc, interval_s=60, lease_seconds=0.0, agent_lifecycle=life)
    result = sched.tick()
    assert result["scanned"] >= 1


def test_lifecycle_offline_then_heartbeat_ready():
    life = AgentLifecycleService()
    life.register("z")
    life.mark_ready("z")
    offline = life.mark_stale_offline(stale_after_s=-1)
    assert "z" in offline
    assert life.get("z")["state"] == "OFFLINE"
    again = life.heartbeat("z")
    assert again.state in {"READY", "REGISTERED"}


def test_router_filter_excludes_draining():
    life = AgentLifecycleService()
    life.register("drain-me")
    life.mark_ready("drain-me")
    life.drain("drain-me")
    life.register("ok-agent")
    life.mark_ready("ok-agent")

    cf = CandidateFilter(lifecycle_checker=life.router_accepts)
    cands = [
        {"agent_id": "drain-me", "status": "online"},
        {"agent_id": "ok-agent", "status": "online"},
    ]
    filtered, _stats = cf._filter_by_health(cands)
    ids = {c["agent_id"] for c in filtered}
    assert "drain-me" not in ids
    assert "ok-agent" in ids

def test_cancel_skips_terminal():
    svc = ExecutionService(config=ExecutionConfig(store="memory", outbox_enabled=False))
    svc.create(task_id="done", root_task_id="root-c")
    svc.start("done")
    svc.succeed("done")
    svc.create(task_id="live", root_task_id="root-c")
    svc.start("live")
    out = cancel_fanout(root_task_id="root-c", execution_service=svc)
    cancelled_ids = {c["task_id"] for c in out["cancelled"]}
    assert "live" in cancelled_ids
    assert "done" not in cancelled_ids
