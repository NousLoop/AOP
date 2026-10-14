"""Scheduler integration tests: claim / unlock / HITL / retry (requires Postgres)."""

from __future__ import annotations

import uuid

import pytest

from planner.dag import PlanNode, TaskPlan
from scheduler import Scheduler
from tests.conftest import requires_pg


def _research_plan(*, hitl_report: bool = False) -> TaskPlan:
    return TaskPlan(
        title="research",
        goal="test goal",
        nodes=[
            PlanNode(id="hop-a", agent_key="claude-code"),
            PlanNode(id="hop-b", agent_key="deepseek-harness"),
            PlanNode(
                id="hop-c",
                agent_key="pi",
                depends_on=["hop-a", "hop-b"],
            ),
            PlanNode(
                id="hop-d",
                agent_key="hermes",
                depends_on=["hop-c"],
                requires_approval=hitl_report,
            ),
        ],
    )


@requires_pg
def test_create_task_marks_parallel_roots_ready(database_url: str):
    sched = Scheduler(database_url=database_url)
    result = sched.create_task(goal="test goal", plan=_research_plan())
    assert result.status == "running"
    assert set(result.ready_nodes) == {"hop-a", "hop-b"}
    assert len(result.ready_jobs) == 2

    nodes = {n["id"]: n for n in result.nodes}
    assert nodes["hop-a"]["status"] == "ready"
    assert nodes["hop-b"]["status"] == "ready"
    assert nodes["hop-c"]["status"] == "pending"
    assert nodes["hop-d"]["status"] == "pending"


@requires_pg
def test_claim_running_is_idempotent(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    result = sched.create_task(goal="claim", plan=_research_plan())
    task_id = result.task_id

    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=1) is True
    # Second claim while already running must fail (no double-dispatch)
    other = str(uuid.uuid4())
    # Insert second agent for FK
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO agents (id, tenant_id, agent_key, name, status)
                VALUES (%s::uuid, %s::uuid, %s, 'other', 'online')
                """,
                (other, sched.tenant_id, f"other-{other[:8]}"),
            )
    try:
        assert sched.claim_running(task_id, "hop-a", other, attempt=1) is False
        node = sched.get_node(task_id, "hop-a")
        assert node is not None
        assert node["status"] == "running"
        assert node["agent_id"] == agent_id
    finally:
        with psycopg.connect(database_url) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM agents WHERE id = %s::uuid", (other,))


@requires_pg
def test_mark_success_unlocks_dependents(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    result = sched.create_task(goal="unlock", plan=_research_plan())
    task_id = result.task_id

    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=1)
    jobs = sched.mark_success(task_id, "hop-a", output={"ok": True}, latency_ms=10)
    assert jobs == []  # analysis still blocked on rag

    assert sched.claim_running(task_id, "hop-b", agent_id, attempt=1)
    jobs = sched.mark_success(task_id, "hop-b", output={"ok": True}, latency_ms=12)
    assert len(jobs) == 1
    assert jobs[0]["node_key"] == "hop-c"
    assert jobs[0]["agent_key"] == "pi"

    analysis = sched.get_node(task_id, "hop-c")
    assert analysis is not None
    assert analysis["status"] == "ready"


@requires_pg
def test_mark_failure_retries_then_fails(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="retry",
        goal="retry",
        nodes=[PlanNode(id="hop-a", agent_key="claude-code")],
    )
    result = sched.create_task(goal="retry", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=1)

    d1 = sched.mark_failure(task_id, "hop-a", error="boom", attempt=1, agent_id=agent_id)
    assert d1["decision"] == "retry"
    assert d1["next_attempt"] == 2
    node = sched.get_node(task_id, "hop-a")
    assert node is not None
    assert node["status"] == "retrying"

    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=2)
    d2 = sched.mark_failure(task_id, "hop-a", error="boom", attempt=2, agent_id=agent_id)
    assert d2["decision"] == "retry"

    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=3)
    d3 = sched.mark_failure(task_id, "hop-a", error="boom", attempt=3, agent_id=agent_id)
    assert d3["decision"] == "failed"
    node = sched.get_node(task_id, "hop-a")
    assert node is not None
    assert node["status"] == "failed"


@requires_pg
def test_mark_failure_cancel_does_not_retry(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="cancel-no-retry",
        goal="cancel-no-retry",
        nodes=[PlanNode(id="hop-a", agent_key="claude-code")],
    )
    result = sched.create_task(goal="cancel-no-retry", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=1)

    decision = sched.mark_failure(
        task_id,
        "hop-a",
        error="a2a status=canceled",
        attempt=1,
        agent_id=agent_id,
    )
    assert decision["decision"] == "cancelled"
    node = sched.get_node(task_id, "hop-a")
    assert node is not None
    assert node["status"] == "cancelled"
    task = sched.get_task(task_id)
    assert task is not None
    assert task["status"] == "cancelled"
    # Late failure callback after OS cancel must also stay cancelled (no retry).
    again = sched.mark_failure(
        task_id,
        "hop-a",
        error="boom after cancel",
        attempt=1,
        agent_id=agent_id,
    )
    assert again["decision"] == "cancelled"
    assert sched.get_node(task_id, "hop-a")["status"] == "cancelled"
    assert not sched.claim_running(task_id, "hop-a", agent_id, attempt=2)


@requires_pg
def test_hitl_approve_unlocks_downstream(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="hitl",
        goal="hitl",
        nodes=[
            PlanNode(id="hop-d", agent_key="hermes", requires_approval=True),
            PlanNode(id="hop-e", agent_key="claude-code", depends_on=["hop-d"]),
        ],
    )
    result = sched.create_task(goal="hitl", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-d", agent_id, attempt=1)

    jobs = sched.mark_success(task_id, "hop-d", output={"draft": True})
    assert jobs == []
    report = sched.get_node(task_id, "hop-d")
    assert report is not None
    assert report["status"] == "waiting_for_user"
    publish = sched.get_node(task_id, "hop-e")
    assert publish is not None
    assert publish["status"] == "pending"

    approved = sched.approve_node(
        task_id, "hop-d", human_input="请在报告中强调竞品对比"
    )
    assert approved["approved"] is True
    assert approved["has_human_input"] is True
    assert len(approved["ready_jobs"]) == 1
    assert approved["ready_jobs"][0]["node_key"] == "hop-e"
    publish = sched.get_node(task_id, "hop-e")
    assert publish is not None
    assert publish["status"] == "ready"
    report = sched.get_node(task_id, "hop-d")
    assert report is not None
    out = report.get("output_json") or {}
    if isinstance(out, str):
        import json
        out = json.loads(out)
    assert out.get("hitl", {}).get("input") == "请在报告中强调竞品对比"
    assert out.get("hitl", {}).get("approved") is True
    # original node output preserved
    assert out.get("draft") is True


@requires_pg
def test_hitl_approve_without_input_still_works(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="hitl-no-input",
        goal="hitl",
        nodes=[
            PlanNode(id="hop-d", agent_key="hermes", requires_approval=True),
            PlanNode(id="hop-e", agent_key="claude-code", depends_on=["hop-d"]),
        ],
    )
    result = sched.create_task(goal="hitl", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-d", agent_id, attempt=1)
    sched.mark_success(task_id, "hop-d", output={"draft": True})
    approved = sched.approve_node(task_id, "hop-d")
    assert approved["approved"] is True
    assert approved.get("has_human_input") is False
    assert len(approved["ready_jobs"]) == 1


@requires_pg
def test_hitl_reject_fails_task(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="hitl-reject",
        goal="hitl",
        nodes=[
            PlanNode(id="hop-d", agent_key="hermes", requires_approval=True),
        ],
    )
    result = sched.create_task(goal="hitl", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-d", agent_id, attempt=1)
    sched.mark_success(task_id, "hop-d", output={"draft": True})

    rejected = sched.reject_node(task_id, node_key="hop-d", reason="not good")
    assert rejected["rejected"] is True
    assert rejected["status"] == "failed"
    node = sched.get_node(task_id, "hop-d")
    assert node is not None
    assert node["status"] == "failed"
