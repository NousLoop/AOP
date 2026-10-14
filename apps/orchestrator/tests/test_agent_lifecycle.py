"""Phase 3 — agent lifecycle + policy engine."""

from __future__ import annotations

import uuid

import pytest

from agent_lifecycle import AgentLifecycleService
from execution.policy import CollaborationPolicy, PolicyEngine
from execution.state_machine import IllegalTransition


def test_register_ready_busy_drain_offline():
    life = AgentLifecycleService()
    life.register("agent-a", version="1.0")
    life.mark_ready("agent-a")
    life.on_task_started("agent-a")
    h = life.health("agent-a")
    assert h["state"] == "BUSY"
    assert h["active_tasks"] == 1
    life.drain("agent-a")
    assert life.get("agent-a")["state"] == "DRAINING"
    ok, reason = life.accepts_delegation("agent-a")
    assert ok is False and reason == "draining"
    life.on_task_finished("agent-a")
    assert life.get("agent-a")["state"] == "OFFLINE"


def test_heartbeat_promotes_registered_to_ready():
    life = AgentLifecycleService()
    rec = life.heartbeat("agent-b", active_tasks=0, version="2")
    assert rec.state == "READY"
    assert life.health("agent-b")["healthy"] is True


def test_capacity_blocks_when_full():
    life = AgentLifecycleService()
    life.register("agent-c", max_concurrency=1)
    life.mark_ready("agent-c")
    life.on_task_started("agent-c")
    ok, reason = life.accepts_delegation("agent-c")
    assert ok is False and reason == "at_capacity"


def test_legacy_untracked_still_allowed():
    life = AgentLifecycleService()
    ok, reason = life.accepts_delegation("never-seen")
    assert ok is True and reason == "legacy_untracked"


def test_drain_rejects_new_work():
    life = AgentLifecycleService()
    life.register("d")
    life.mark_ready("d")
    life.drain("d")
    with pytest.raises(IllegalTransition):
        life.on_task_started("d")


def test_policy_merge_priority():
    engine = PolicyEngine()
    gov = {"max_agent_visits": 5, "max_delegation_depth": 8}
    pol = engine.resolve(governance_policy=gov, overrides={"max_retries": 9})
    assert pol.max_agent_visits == 5
    assert pol.max_depth == 8
    assert pol.max_retries == 9


def test_policy_allowed_denied_agents():
    pol = CollaborationPolicy(allowed_agents=["a"], denied_agents=["b"])
    assert pol.allows_agent("a")[0] is True
    assert pol.allows_agent("b")[0] is False
    assert pol.allows_agent("c")[0] is False


def test_postgres_lifecycle_survives_new_service():
    pytest.importorskip("psycopg")
    from tests.conftest import _pg_available

    if not _pg_available():
        pytest.skip("PostgreSQL not reachable")
    from agent_lifecycle import PostgresAgentLifecycleService

    aid = f"life-{uuid.uuid4().hex[:12]}"
    a = PostgresAgentLifecycleService()
    a.ping()
    a.register(aid, version="pg")
    a.mark_ready(aid)
    b = PostgresAgentLifecycleService()
    row = b.get(aid)
    assert row is not None
    assert row["state"] == "READY"
    assert row["version"] == "pg"

