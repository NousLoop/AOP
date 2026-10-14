"""Task DAG models and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


class DAGValidationError(ValueError):
    pass


@dataclass
class PlanNode:
    id: str
    agent_key: str
    depends_on: list[str] = field(default_factory=list)
    requires_approval: bool = False
    # none | system | agent | both — empty falls back to requires_approval → system
    approval_mode: str | None = None
    # Peer agent_key that must approve when mode is agent or both
    approver_agent: str | None = None
    # Per-agent work brief (routing preamble stripped). Worker sends this to the agent.
    instruction: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "agent_key": self.agent_key}
        if self.depends_on:
            payload["depends_on"] = list(self.depends_on)
        mode = (self.approval_mode or "").strip().lower() or None
        if mode and mode not in {"none", "off"}:
            payload["approval_mode"] = mode
        if self.requires_approval or mode in {"system", "both"}:
            payload["requires_approval"] = True
        if self.approver_agent:
            payload["approver_agent"] = self.approver_agent
        if self.instruction:
            payload["instruction"] = self.instruction
        return payload


@dataclass
class TaskPlan:
    title: str
    goal: str
    nodes: list[PlanNode]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "goal": self.goal,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskPlan:
        nodes = [
            PlanNode(
                id=str(n["id"]),
                agent_key=str(n.get("agent_key") or ""),
                depends_on=[str(d) for d in (n.get("depends_on") or [])],
                requires_approval=bool(n.get("requires_approval")),
                approval_mode=(
                    str(n["approval_mode"]).strip().lower()
                    if isinstance(n.get("approval_mode"), str) and n["approval_mode"].strip()
                    else None
                ),
                approver_agent=(
                    str(n["approver_agent"]).strip()
                    if isinstance(n.get("approver_agent"), str) and n["approver_agent"].strip()
                    else None
                ),
                instruction=(
                    str(n["instruction"]).strip()
                    if isinstance(n.get("instruction"), str) and n["instruction"].strip()
                    else None
                ),
            )
            for n in (raw.get("nodes") or [])
        ]
        return cls(
            title=str(raw.get("title") or "task"),
            goal=str(raw.get("goal") or ""),
            nodes=nodes,
        )


def validate_plan(plan: TaskPlan, *, available_agent_keys: Iterable[str] | None = None) -> None:
    if not plan.nodes:
        raise DAGValidationError("plan must contain at least one node")

    ids = [n.id for n in plan.nodes]
    if any(not i or not str(i).strip() for i in ids):
        raise DAGValidationError("node id must be non-empty")
    if len(ids) != len(set(ids)):
        raise DAGValidationError("node ids must be unique")

    id_set = set(ids)
    keys_ok = set(available_agent_keys) if available_agent_keys is not None else None

    for node in plan.nodes:
        if not node.agent_key:
            raise DAGValidationError(f"node {node.id!r} agent_key must be non-empty")
        if keys_ok is not None and node.agent_key not in keys_ok:
            raise DAGValidationError(
                f"node {node.id!r} agent_key {node.agent_key!r} is not available in registry"
            )
        for dep in node.depends_on:
            if dep not in id_set:
                raise DAGValidationError(
                    f"node {node.id!r} depends on unknown node {dep!r}"
                )
            if dep == node.id:
                raise DAGValidationError(f"node {node.id!r} cannot depend on itself")

    # Cycle detection (DFS)
    graph = {n.id: list(n.depends_on) for n in plan.nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node_id: str) -> None:
        if node_id in visiting:
            raise DAGValidationError(f"cycle detected at node {node_id!r}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dep in graph.get(node_id, []):
            dfs(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        dfs(node_id)


def ready_node_ids(nodes: list[PlanNode], completed: set[str], pending: set[str]) -> list[str]:
    """Return node ids in pending whose dependencies are all completed."""
    ready: list[str] = []
    by_id = {n.id: n for n in nodes}
    for node_id in pending:
        node = by_id[node_id]
        if all(dep in completed for dep in node.depends_on):
            ready.append(node_id)
    return ready
