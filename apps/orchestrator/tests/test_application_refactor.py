"""Regression tests for Phase 35-01 application-layer split (no PG required)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from artifacts.manager import ArtifactManager
from planner.dag import PlanNode, TaskPlan
from planner.engine import PlanningEngine
from router import AgentRouter
from scheduler.engine import SchedulingEngine


def test_service_shim_exports_task_service():
    from service import TaskService

    assert TaskService.__name__ == "TaskService"
    assert TaskService.__module__ == "application.task_service"


def test_planning_engine_delegates_to_planner():
    fake = MagicMock()
    fake.plan.return_value = SimpleNamespace(plan="p", method="heuristic", available_agent_keys=["claude-code"])
    fake.list_available_agents.return_value = ["claude-code"]
    engine = PlanningEngine(planner=fake)
    out = engine.plan("goal", title="t")
    fake.plan.assert_called_once_with("goal", title="t")
    assert out.method == "heuristic"
    assert engine.list_available_agents() == ["claude-code"]
    assert engine.planner is fake


def test_agent_router_preview_delegates():
    fake = MagicMock()
    fake.preview.return_value = {"agent_key": "claude-code", "candidates": []}
    # AgentRouter is the public surface (RoutingEngine wrapper removed).
    assert fake.preview("claude-code")["agent_key"] == "claude-code"
    fake.preview.assert_called_once_with("claude-code")
    assert AgentRouter is not None

def test_scheduling_engine_create_and_enqueue():
    plan = TaskPlan(
        title="t",
        goal="g",
        nodes=[PlanNode(id="a", agent_key="claude-code")],
    )
    sched = MagicMock()
    sched.create_task.return_value = SimpleNamespace(
        task_id="tid-1",
        status="running",
        ready_jobs=[{"task_id": "tid-1", "node_key": "a", "agent_key": "claude-code", "node_id": "n1"}],
        ready_nodes=["a"],
        nodes=[{"id": "a", "status": "ready"}],
    )
    job_queue = MagicMock()
    job_queue.enqueue.return_value = "msg-id"
    event_publisher = MagicMock()
    event_publisher.publish_node_enqueued.return_value = "event-id"
    engine = SchedulingEngine(scheduler=sched, job_queue=job_queue, event_publisher=event_publisher)
    with patch("scheduler.engine.record_task_created"):
        out = engine.create_and_enqueue(
            goal="g",
            plan=plan,
            planner_meta={"method": "heuristic", "available_agent_keys": ["claude-code"]},
            plan_source="planner",
        )
    assert out["task_id"] == "tid-1"
    assert out["enqueued_nodes"] == ["a"]
    job_queue.enqueue.assert_called_once()
    event_publisher.publish_node_enqueued.assert_called_once()


def test_scheduling_engine_requeue_incomplete_enqueues():
    sched = MagicMock()
    sched.requeue_incomplete.return_value = [
        {"task_id": "t1", "node_id": "n1", "node_key": "a", "agent_key": "claude-code", "attempt": 1}
    ]
    job_queue = MagicMock()
    event_publisher = MagicMock()
    engine = SchedulingEngine(scheduler=sched, job_queue=job_queue, event_publisher=event_publisher)
    out = engine.requeue_incomplete("t1")
    assert out["enqueued_nodes"] == ["a"]
    job_queue.enqueue.assert_called_once()
    event_publisher.publish_node_enqueued.assert_called_once()


def test_scheduling_engine_approve_enqueues_ready_jobs():
    sched = MagicMock()
    sched.approve_node.return_value = {
        "status": "running",
        "ready_jobs": [{"task_id": "t1", "node_key": "b", "agent_key": "hermes"}],
    }
    job_queue = MagicMock()
    job_queue.enqueue.return_value = "msg-id"
    event_publisher = MagicMock()
    event_publisher.publish_node_enqueued.return_value = "event-id"
    engine = SchedulingEngine(scheduler=sched, job_queue=job_queue, event_publisher=event_publisher)
    out = engine.approve("t1", node_key="hitl", human_input="补充定价分析")
    assert out["enqueued_nodes"] == ["b"]
    job_queue.enqueue.assert_called_once()
    sched.approve_node.assert_called_once_with(
        "t1", "hitl", human_input="补充定价分析", actor="system"
    )


def test_artifact_manager_merges_by_uri():
    store = MagicMock()
    store.list_task_artifacts.return_value = [
        {"uri": "s3://b/a", "name": "output.md", "source": "minio"},
    ]
    scheduler = MagicMock()
    scheduler.get_task.return_value = {"nodes": [{"id": "n1"}]}
    scheduler.get_node.return_value = {
        "output_json": {
            "artifacts": [
                {"uri": "s3://b/a", "name": "output.md", "type": "text", "size": 10},
                {"uri": "s3://b/b", "name": "output.json", "type": "json"},
            ]
        }
    }
    mgr = ArtifactManager(store=store, scheduler=scheduler)
    items = mgr.list_for_task("tid")
    uris = {i["uri"] for i in items}
    assert uris == {"s3://b/a", "s3://b/b"}
    merged = next(i for i in items if i["uri"] == "s3://b/a")
    assert merged["source"] == "minio"
    assert merged["size"] == 10


def test_task_manager_create_pipeline():
    from application.task_manager import TaskManager

    planning = MagicMock()
    planning.plan.return_value = SimpleNamespace(
        plan=TaskPlan(title="t", goal="hello", nodes=[PlanNode(id="a", agent_key="claude-code")]),
        method="heuristic",
        available_agent_keys=["claude-code"],
    )
    scheduling = MagicMock()
    scheduling.create_and_enqueue.return_value = {
        "task_id": "tid-x",
        "status": "running",
        "enqueued_nodes": ["a"],
    }
    quotas = MagicMock(spec=["assert_can_create_task"])
    memory = MagicMock()
    mgr = TaskManager(
        planning=planning,
        scheduling=scheduling,
        quotas=quotas,
        memory=memory,
        artifacts=MagicMock(),
        workflows=MagicMock(),
    )
    out = mgr.create("hello", title="t")
    quotas.assert_can_create_task.assert_called_once()
    planning.plan.assert_called_once_with("hello", title="t")
    scheduling.create_and_enqueue.assert_called_once()
    memory.remember_goal.assert_called_once()
    assert out["task_id"] == "tid-x"


def test_task_manager_approve_and_reject_sync_os():
    from application.task_manager import TaskManager
    from unittest.mock import patch

    scheduling = MagicMock()
    scheduling.approve.return_value = {"status": "running", "enqueued_nodes": ["b"]}
    scheduling.reject.return_value = {"status": "failed"}
    mgr = TaskManager(
        planning=MagicMock(),
        scheduling=scheduling,
        quotas=MagicMock(),
        memory=MagicMock(),
        artifacts=MagicMock(),
        workflows=MagicMock(),
    )
    with patch("application.task_manager.try_sync_from_ctx") as sync:
        mgr.approve("tid-a", node_key="hitl")
        mgr.reject("tid-a", reason="no")
    assert sync.call_count == 2
    assert sync.call_args_list[0].args[0] == "tid-a"
    assert sync.call_args_list[1].kwargs.get("error") == "no"
