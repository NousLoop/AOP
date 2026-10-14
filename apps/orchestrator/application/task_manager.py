"""TaskManager: application use-cases for task lifecycle."""

from __future__ import annotations

import os
from typing import Any

from artifacts.manager import ArtifactManager
from execution.dag_bridge import try_sync_from_ctx
from memory import MemoryService
from planner.dag import TaskPlan, validate_plan
from planner.engine import PlanningEngine
from quota import QuotaService
from scheduler import DEFAULT_TENANT_ID
from scheduler.engine import SchedulingEngine
from workflows import WorkflowService


def _os_state(rec: Any) -> str | None:
    if rec is None:
        return None
    state = getattr(rec, "state", None)
    if state:
        return str(state)
    if isinstance(rec, dict) and rec.get("state"):
        return str(rec["state"])
    return None


def _tenant_memory_enabled() -> bool:
    return os.getenv("TENANT_MEMORY_RECALL", "1").lower() not in {"0", "false", "no", "off"}


class TaskManager:
    """Orchestrates plan → schedule → enqueue and HITL decisions."""

    def __init__(
        self,
        *,
        planning: PlanningEngine | None = None,
        scheduling: SchedulingEngine | None = None,
        workflows: WorkflowService | None = None,
        quotas: QuotaService | None = None,
        memory: MemoryService | None = None,
        artifacts: ArtifactManager | None = None,
    ) -> None:
        self.planning = planning or PlanningEngine()
        self.scheduling = scheduling or SchedulingEngine()
        self.workflows = workflows or WorkflowService()
        self.quotas = quotas or QuotaService()
        self.memory = memory or MemoryService()
        self.artifacts = artifacts or ArtifactManager(
            store=None,
            scheduler=self.scheduling.scheduler,
        )

    def create(
        self,
        content: str,
        *,
        title: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        self.quotas.assert_can_create_task(tenant_id=tenant_id or DEFAULT_TENANT_ID)
        result = self.planning.plan(content, title=title)
        scheduled = self.scheduling.create_and_enqueue(
            goal=content,
            plan=result.plan,
            planner_meta={
                "method": result.method,
                "available_agent_keys": result.available_agent_keys,
            },
            plan_source="planner",
            tenant_id=tenant_id,
        )
        self._remember_goal(scheduled["task_id"], content, tenant_id=tenant_id)
        rec = try_sync_from_ctx(
            scheduled["task_id"],
            scheduled.get("status") or "running",
            tenant_id=tenant_id,
            metadata={"planner": scheduled.get("planner")},
        )
        os_state = _os_state(rec)
        if os_state:
            scheduled["os_state"] = os_state
        return scheduled

    def run_workflow(
        self,
        workflow_id: str,
        *,
        content: str,
        title: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("input.content is required")
        self.quotas.assert_can_create_task(tenant_id=tenant_id or DEFAULT_TENANT_ID)
        wf = self.workflows.get(workflow_id)
        if not wf:
            raise ValueError(f"workflow not found: {workflow_id}")
        plan: TaskPlan = self.workflows.build_plan(workflow_id, goal=content, title=title)
        available = set(self.planning.list_available_agents())
        validate_plan(plan, available_agent_keys=available)
        scheduled = self.scheduling.create_and_enqueue(
            goal=content,
            plan=plan,
            planner_meta={
                "method": "workflow",
                "workflow_id": wf["workflow_id"],
                "workflow_key": wf["workflow_key"],
                "available_agent_keys": sorted(available),
            },
            plan_source="workflow",
            workflow_id=wf["workflow_id"],
            tenant_id=tenant_id,
        )
        self._remember_goal(scheduled["task_id"], content, tenant_id=tenant_id)
        rec = try_sync_from_ctx(
            scheduled["task_id"],
            scheduled.get("status") or "running",
            tenant_id=tenant_id,
            metadata={"planner": scheduled.get("planner"), "workflow_id": workflow_id},
        )
        os_state = _os_state(rec)
        if os_state:
            scheduled["os_state"] = os_state
        return scheduled

    def get(self, task_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.scheduling.get_task(task_id, tenant_id=tenant_id)

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.scheduling.list_tasks(status=status, limit=limit, tenant_id=tenant_id)

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        out = self.scheduling.cancel_task(task_id)
        if out:
            try_sync_from_ctx(task_id, "cancelled")
        return out

    def approve(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        human_input: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        out = self.scheduling.approve(
            task_id, node_key=node_key, human_input=human_input, actor=actor
        )
        try_sync_from_ctx(task_id, out.get("status") or "running")
        return out

    def reject(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        reason: str = "rejected by user",
    ) -> dict[str, Any]:
        out = self.scheduling.reject(task_id, node_key=node_key, reason=reason)
        try_sync_from_ctx(task_id, "failed", error=reason)
        return out

    def list_checkpoints(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.scheduling.list_checkpoints(
            task_id, node_key=node_key, limit=limit
        )

    def replay_from_node(
        self,
        task_id: str,
        node_key: str,
        *,
        clear_downstream: bool = True,
    ) -> dict[str, Any]:
        out = self.scheduling.replay_from_node(
            task_id, node_key, clear_downstream=clear_downstream
        )
        try_sync_from_ctx(task_id, "running")
        return out

    def overview(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.scheduling.overview_stats(tenant_id=tenant_id)

    def events(self, task_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return self.scheduling.list_events(task_id, limit=limit)

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        return self.artifacts.list_for_task(task_id)

    def list_all_artifacts(
        self,
        *,
        task_id: str | None = None,
        type_filter: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.artifacts.list_all(
            task_id=task_id,
            type_filter=type_filter,
            limit=limit,
        )

    def _remember_goal(
        self,
        task_id: str,
        content: str,
        *,
        tenant_id: str | None,
    ) -> None:
        tid = tenant_id or DEFAULT_TENANT_ID
        try:
            self.memory.remember_goal(task_id, content, tenant_id=tid)
            if _tenant_memory_enabled():
                self.memory.recall_into_task(task_id, content, tenant_id=tid)
        except Exception:  # noqa: BLE001
            pass
