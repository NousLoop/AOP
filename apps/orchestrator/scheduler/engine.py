"""SchedulingEngine: persist DAG + enqueue ready jobs (Scheduler ⊕ Streams)."""

from __future__ import annotations

from typing import Any

from observability import record_task_created
from planner.dag import TaskPlan
from scheduler import DEFAULT_TENANT_ID, Scheduler, ScheduleResult
from .job_queue import JobQueue
from .event_publisher import EventPublisher


class SchedulingEngine:
    """Task persistence and queue side-effects for ready / unlocked nodes."""

    def __init__(
        self,
        *,
        scheduler: Scheduler | None = None,
        job_queue: JobQueue | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.scheduler = scheduler or Scheduler()
        self.job_queue = job_queue or JobQueue()
        self.event_publisher = event_publisher or EventPublisher()

    def enqueue_jobs(self, jobs: list[dict[str, Any]], *, task_id: str) -> list[str]:
        """XADD ready jobs and publish task.node.enqueued. Returns node_keys."""
        enqueued: list[str] = []
        for job in jobs or []:
            self.job_queue.enqueue(**job)
            enqueued.append(job["node_key"])
            self.event_publisher.publish_node_enqueued(
                task_id, job["node_key"], job["agent_key"]
            )
        return enqueued

    def create_and_enqueue(
        self,
        *,
        goal: str,
        plan: TaskPlan,
        planner_meta: dict[str, Any],
        plan_source: str,
        workflow_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        scheduled: ScheduleResult = self.scheduler.create_task(
            goal=goal,
            plan=plan,
            workflow_id=workflow_id,
            plan_source=plan_source,
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
        )
        enqueued = self.enqueue_jobs(scheduled.ready_jobs, task_id=scheduled.task_id)
        try:
            record_task_created(str(scheduled.status or "running"))
        except Exception:  # noqa: BLE001
            pass
        return {
            "task_id": scheduled.task_id,
            "status": scheduled.status,
            "title": plan.title,
            "workflow_id": workflow_id,
            "planner": planner_meta,
            "plan": plan.to_dict(),
            "ready_nodes": scheduled.ready_nodes,
            "enqueued_nodes": enqueued,
            "nodes": scheduled.nodes,
        }

    def approve(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        human_input: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        result = self.scheduler.approve_node(
            task_id, node_key, human_input=human_input, actor=actor
        )
        enqueued = self.enqueue_jobs(result.get("ready_jobs") or [], task_id=task_id)
        result["enqueued_nodes"] = enqueued
        if result.get("status") == "completed":
            side_effects = self.event_publisher.publish_task_completed(task_id)
            result["aggregation"] = side_effects.get("aggregation")
            result["evaluation"] = side_effects.get("evaluation")
        return result

    def reject(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        reason: str = "rejected by user",
    ) -> dict[str, Any]:
        result = self.scheduler.reject_node(task_id, node_key=node_key, reason=reason)
        side_effects = self.event_publisher.publish_task_failed(task_id)
        result["evaluation"] = side_effects.get("evaluation")
        return result

    def list_checkpoints(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.scheduler.list_checkpoints(
            task_id, node_key=node_key, limit=limit
        )

    def replay_from_node(
        self,
        task_id: str,
        node_key: str,
        *,
        clear_downstream: bool = True,
    ) -> dict[str, Any]:
        result = self.scheduler.replay_from_node(
            task_id, node_key, clear_downstream=clear_downstream
        )
        enqueued = self.enqueue_jobs(result.get("ready_jobs") or [], task_id=task_id)
        result["enqueued_nodes"] = enqueued
        return result

    def requeue_incomplete(self, task_id: str) -> dict[str, Any]:
        jobs = self.scheduler.requeue_incomplete(task_id)
        enqueued = self.enqueue_jobs(jobs, task_id=task_id)
        return {"task_id": task_id, "jobs": jobs, "enqueued_nodes": enqueued}

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.scheduler.get_task(task_id, tenant_id=tenant_id)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.scheduler.list_tasks(status=status, limit=limit, tenant_id=tenant_id)

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.scheduler.cancel_task(task_id)
        if row and row.get("cancelled") is True:
            side_effects = self.event_publisher.publish_task_failed(task_id)
            row = {**row, "evaluation": side_effects.get("evaluation")}
        return row

    def overview_stats(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.scheduler.overview_stats(tenant_id=tenant_id)

    def list_events(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.scheduler.list_events(task_id, limit=limit)
