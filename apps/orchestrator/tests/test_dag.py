"""Unit tests for Task DAG validation and ready-node selection."""

from __future__ import annotations

import pytest

from planner.dag import (
    DAGValidationError,
    PlanNode,
    TaskPlan,
    ready_node_ids,
    validate_plan,
)


def _plan(*nodes: PlanNode, title: str = "t", goal: str = "g") -> TaskPlan:
    return TaskPlan(title=title, goal=goal, nodes=list(nodes))


def test_validate_ok_linear_dag():
    plan = _plan(
        PlanNode(id="a", agent_key="claude-code"),
        PlanNode(id="b", agent_key="deepseek-harness", depends_on=["a"]),
        PlanNode(id="c", agent_key="hermes", depends_on=["b"]),
    )
    validate_plan(plan, available_agent_keys={"claude-code", "deepseek-harness", "hermes"})


def test_validate_ok_parallel_roots():
    plan = _plan(
        PlanNode(id="hop-a", agent_key="claude-code"),
        PlanNode(id="hop-b", agent_key="deepseek-harness"),
        PlanNode(id="hop-c", agent_key="pi", depends_on=["hop-a", "hop-b"]),
    )
    validate_plan(
        plan,
        available_agent_keys={"claude-code", "deepseek-harness", "pi"},
    )


def test_reject_empty_plan():
    with pytest.raises(DAGValidationError, match="at least one node"):
        validate_plan(_plan())


def test_reject_duplicate_ids():
    with pytest.raises(DAGValidationError, match="unique"):
        validate_plan(
            _plan(
                PlanNode(id="a", agent_key="claude-code"),
                PlanNode(id="a", agent_key="deepseek-harness"),
            )
        )


def test_reject_unknown_dependency():
    with pytest.raises(DAGValidationError, match="unknown node"):
        validate_plan(
            _plan(PlanNode(id="a", agent_key="claude-code", depends_on=["missing"]))
        )


def test_reject_self_dependency():
    with pytest.raises(DAGValidationError, match="cannot depend on itself"):
        validate_plan(_plan(PlanNode(id="a", agent_key="claude-code", depends_on=["a"])))


def test_reject_cycle():
    with pytest.raises(DAGValidationError, match="cycle"):
        validate_plan(
            _plan(
                PlanNode(id="a", agent_key="claude-code", depends_on=["b"]),
                PlanNode(id="b", agent_key="deepseek-harness", depends_on=["a"]),
            )
        )


def test_reject_unavailable_agent_key():
    with pytest.raises(DAGValidationError, match="not available"):
        validate_plan(
            _plan(PlanNode(id="a", agent_key="claude-code")),
            available_agent_keys={"pi"},
        )


def test_ready_node_ids_unlocks_after_deps():
    nodes = [
        PlanNode(id="a", agent_key="claude-code"),
        PlanNode(id="b", agent_key="deepseek-harness"),
        PlanNode(id="c", agent_key="pi", depends_on=["a", "b"]),
    ]
    pending = {"a", "b", "c"}
    assert set(ready_node_ids(nodes, completed=set(), pending=pending)) == {"a", "b"}

    pending = {"c"}
    assert ready_node_ids(nodes, completed={"a"}, pending=pending) == []
    assert ready_node_ids(nodes, completed={"a", "b"}, pending=pending) == ["c"]


def test_plan_roundtrip_dict():
    plan = _plan(
        PlanNode(id="a", agent_key="claude-code", requires_approval=True),
        PlanNode(
            id="b",
            agent_key="hermes",
            depends_on=["a"],
            approval_mode="both",
            approver_agent="pi",
        ),
    )
    restored = TaskPlan.from_dict(plan.to_dict())
    assert restored.nodes[0].agent_key == "claude-code"
    assert restored.to_dict()["nodes"][0]["agent_key"] == "claude-code"
    assert restored.to_dict()["nodes"][0]["agent_key"] == "claude-code"
    assert restored.title == plan.title
    assert restored.nodes[0].requires_approval is True
    assert restored.nodes[1].depends_on == ["a"]
    assert restored.nodes[1].approval_mode == "both"
    assert restored.nodes[1].approver_agent == "pi"
