"""Planner JSON repair + goal decomposition (agent_key hops)."""

from __future__ import annotations

import os
from unittest.mock import patch

from planner import Planner, RegisteredAgent
from planner.dag import PlanNode, TaskPlan, validate_plan
from planner.decompose import decompose_to_nodes, split_goal_fragments
from planner.json_plan import extract_json_object, nodes_from_payload


HARNESS = {"claude-code", "deepseek-harness", "pi", "openclaw", "hermes"}


def _reg(keys: set[str]) -> list[RegisteredAgent]:
    return [RegisteredAgent(key=k, name=k) for k in sorted(keys)]


def test_extract_json_from_fence():
    raw = 'Sure.\n```json\n{"nodes":[{"id":"a","agent_key":"claude-code"}]}\n```\n'
    data = extract_json_object(raw)
    assert data and data["nodes"][0]["agent_key"] == "claude-code"


def test_nodes_from_payload_drops_specialty_and_unknown():
    data = {
        "nodes": [
            {"id": "a", "agent_key": "claude-code"},
            {"id": "b", "agent_key": "web-search", "depends_on": ["a"]},
            {"id": "c", "agent_key": "pi", "depends_on": ["a", "ghost"]},
            {"id": "d", "agent_key": "not-real"},
        ]
    }
    nodes = nodes_from_payload(data, available_agent_keys=HARNESS, hitl=set())
    ids = {n.id for n in nodes}
    assert ids == {"a", "c"}
    c = next(n for n in nodes if n.id == "c")
    assert c.depends_on == ["a"]
    validate_plan(TaskPlan(title="t", goal="g", nodes=nodes), available_agent_keys=HARNESS)


def test_split_numbered_goal():
    frags = split_goal_fragments("1. 搜索竞品 2. 分析市场 3. 生成报告")
    assert len(frags) >= 3


def test_decompose_named_agents_linear():
    nodes = decompose_to_nodes(
        "1. 用 pi 调研竞品 2. 用 claude-code 写报告",
        HARNESS,
        hitl=set(),
    )
    assert [n.agent_key for n in nodes] == ["pi", "claude-code"]
    assert nodes[0].depends_on == []
    assert nodes[1].depends_on == [nodes[0].id]


def test_decompose_ignores_specialty_catalog():
    nodes = decompose_to_nodes(
        "1. 搜索竞品 2. 做业务分析 3. 生成报告",
        HARNESS,
        hitl=set(),
    )
    assert nodes == []


def test_planner_multi_step_without_named_agents_stays_single_hop():
    """Specialty catalogs are gone — numbered research goals are one agent_key hop."""
    p = Planner(database_url="postgresql://invalid/invalid")
    with patch.object(p, "list_registered_agents", return_value=_reg(HARNESS)):
        with patch.object(p, "list_ready_agents", return_value=sorted(HARNESS)):
            with patch.dict(
                os.environ,
                {
                    "PLANNER_V2": "1",
                    "PLANNER_MULTI_STEP": "1",
                    "HITL_SKILLS": "off",
                },
                clear=False,
            ):
                result = p.plan("1. 搜索资料 2. 分析一下 3. 生成报告")
    assert result.method == "heuristic"
    assert [n.agent_key for n in result.plan.nodes] == ["claude-code"]


def test_planner_llm_invalid_falls_back_to_heuristic():
    p = Planner(database_url="postgresql://invalid/invalid")
    with patch.object(p, "list_registered_agents", return_value=_reg(HARNESS)):
        with patch.object(p, "list_ready_agents", return_value=sorted(HARNESS)):
            with patch.dict(
                os.environ,
                {
                    "PLANNER_LLM": "true",
                    "PLANNER_V2": "1",
                    "PLANNER_MULTI_STEP": "0",
                    "HITL_SKILLS": "off",
                },
                clear=False,
            ):
                with patch.object(
                    p,
                    "_try_llm_nodes",
                    return_value=(
                        [PlanNode(id="x", agent_key="missing-skill")],
                        False,
                    ),
                ):
                    result = p.plan("帮我研究一个 AI 产品并生成报告")
    assert result.method == "heuristic"
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_research_goal_still_heuristic_pipeline():
    p = Planner(database_url="postgresql://invalid/invalid")
    with patch.object(p, "list_registered_agents", return_value=_reg(HARNESS)):
        with patch.object(p, "list_ready_agents", return_value=sorted(HARNESS)):
            with patch.dict(
                os.environ,
                {
                    "PLANNER_V2": "1",
                    "PLANNER_MULTI_STEP": "0",
                    "HITL_SKILLS": "off",
                },
                clear=False,
            ):
                result = p.plan("帮我研究一个 AI 产品并生成报告")
    assert result.method == "heuristic"
    assert len(result.plan.nodes) == 1
    assert result.plan.nodes[0].agent_key == "claude-code"
