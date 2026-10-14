"""Stale running-node reclaim (requires Postgres)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

from planner.dag import PlanNode, TaskPlan
from scheduler import Scheduler
from tests.conftest import requires_pg


def _backdate_running(database_url: str, task_id: str, node_key: str, *, seconds: int) -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute(
                """
                UPDATE task_nodes
                SET started_at = %s, updated_at = %s
                WHERE task_id = %s::uuid AND node_key = %s
                """,
                (past, past, task_id, node_key),
            )


@requires_pg
def test_reclaim_stale_running_enqueues_retry(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="stale",
        goal="stale",
        nodes=[PlanNode(id="hop-a", agent_key="claude-code")],
    )
    result = sched.create_task(goal="stale", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=1)
    _backdate_running(database_url, task_id, "hop-a", seconds=120)

    jobs = sched.reclaim_stale_nodes(stale_seconds=60)
    matching = [j for j in jobs if j["task_id"] == task_id and j["node_key"] == "hop-a"]
    assert len(matching) == 1
    assert matching[0]["attempt"] == 2
    assert agent_id in (matching[0].get("exclude_agent_ids") or [])

    node = sched.get_node(task_id, "hop-a")
    assert node is not None
    assert node["status"] == "retrying"


@requires_pg
def test_reclaim_stale_exhausts_retries_fails_task(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="stale-fail",
        goal="stale",
        nodes=[PlanNode(id="hop-a", agent_key="claude-code")],
    )
    result = sched.create_task(goal="stale", plan=plan)
    task_id = result.task_id
    assert sched.claim_running(task_id, "hop-a", agent_id, attempt=3)

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute(
                """
                UPDATE task_nodes
                SET attempt = 3, max_retry = 3
                WHERE task_id = %s::uuid AND node_key = 'hop-a'
                """,
                (task_id,),
            )
    _backdate_running(database_url, task_id, "hop-a", seconds=120)

    jobs = sched.reclaim_stale_nodes(stale_seconds=60)
    matching = [j for j in jobs if j["task_id"] == task_id]
    assert matching == []

    node = sched.get_node(task_id, "hop-a")
    assert node is not None
    assert node["status"] == "failed"
    task = sched.get_task(task_id)
    assert task is not None
    assert task["status"] == "failed"


@requires_pg
def test_requeue_incomplete_re_enqueues_ready_node(database_url: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="requeue",
        goal="requeue",
        nodes=[PlanNode(id="hop-a", agent_key="claude-code")],
    )
    result = sched.create_task(goal="requeue", plan=plan)
    jobs = sched.requeue_incomplete(result.task_id)
    keys = {j["node_key"] for j in jobs}
    assert "hop-a" in keys


@requires_pg
def test_requeue_incomplete_reclaims_running_node(database_url: str, agent_id: str):
    sched = Scheduler(database_url=database_url)
    plan = TaskPlan(
        title="requeue-run",
        goal="requeue-run",
        nodes=[PlanNode(id="hop-a", agent_key="claude-code")],
    )
    result = sched.create_task(goal="requeue-run", plan=plan)
    assert sched.claim_running(result.task_id, "hop-a", agent_id, attempt=1)
    jobs = sched.requeue_incomplete(result.task_id)
    matching = [j for j in jobs if j["node_key"] == "hop-a"]
    assert len(matching) == 1
    assert matching[0]["attempt"] == 1
    node = sched.get_node(result.task_id, "hop-a")
    assert node is not None
    assert node["status"] == "retrying"
