"""Planner heuristic tests — registry-driven routing (no hardcoded catalog)."""

from __future__ import annotations

import os
import re
from unittest.mock import patch

import pytest

from planner import (
    Planner,
    RegisteredAgent,
    agent_instruction,
    extract_work_goal,
    probe_keys_ready,
    requested_agent,
    wants_all_agents,
)
from planner.dag import DAGValidationError


AGENTS = {"claude-code"}
ALL_AGENTS = {"claude-code", "deepseek-harness", "pi"}
ALL_REGISTRY = [
    RegisteredAgent(key="claude-code", name="Claude Code"),
    RegisteredAgent(key="deepseek-harness", name="DeepSeek Harness"),
    RegisteredAgent(key="pi", name="Pi"),
]


def _registry_for(keys: set[str] | list[str]) -> list[RegisteredAgent]:
    keyed = {a.key: a for a in ALL_REGISTRY}
    out: list[RegisteredAgent] = []
    for k in sorted(keys):
        out.append(keyed.get(k, RegisteredAgent(key=k, name=k)))
    return out


@pytest.fixture
def planner() -> Planner:
    p = Planner(database_url="postgresql://invalid/invalid")
    reg = _registry_for(AGENTS)
    with patch.object(p, "list_registered_agents", return_value=reg):
        with patch.object(p, "list_ready_agents", return_value=sorted(AGENTS)):
            yield p


def _plan(
    planner: Planner,
    goal: str,
    *,
    hitl: str = "off",
    agents: set[str] | None = None,
    ready: set[str] | None = None,
):
    online = set(agents if agents is not None else AGENTS)
    ready_keys = sorted(ready if ready is not None else online)
    reg = _registry_for(online)
    with patch.object(planner, "list_registered_agents", return_value=reg):
        with patch.object(planner, "list_ready_agents", return_value=ready_keys):
            with patch.dict(
                os.environ,
                {"HITL_SKILLS": hitl, "PLANNER_V2": "1", "PLANNER_MULTI_STEP": "0"},
                clear=False,
            ):
                return planner.plan(goal)


def test_any_goal_goes_to_default_agent(planner: Planner):
    result = _plan(planner, "搜索 OpenAI 最新消息")
    assert result.method == "heuristic"
    assert len(result.plan.nodes) == 1
    assert result.plan.nodes[0].id == "run"
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_code_goal_also_default(planner: Planner):
    result = _plan(planner, "帮我写一个 python 函数实现快速排序")
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_default_uses_ready_when_cli_not_ready(planner: Planner):
    result = _plan(
        planner,
        "写一份简报",
        agents={"claude-code", "hermes"},
        ready={"hermes"},
    )
    assert result.plan.nodes[0].agent_key == "hermes"


def test_named_cli_still_used_when_not_ready(planner: Planner):
    result = _plan(
        planner,
        "用 claude-code 写一份简报",
        agents={"claude-code", "hermes"},
        ready={"hermes"},
    )
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_requested_agent_pi_chinese_preamble():
    assert (
        requested_agent(
            "使用pi调研一款 AI 产品，整理公开资料并输出研究摘要",
            registry=ALL_REGISTRY,
        )
        == "pi"
    )
    assert requested_agent("用 pi 调研 ui2v", registry=ALL_REGISTRY) == "pi"
    assert (
        requested_agent(
            "用openclaw写个脚本",
            ALL_AGENTS | {"openclaw"},
            registry=list(ALL_REGISTRY)
            + [RegisteredAgent(key="openclaw", name="OpenClaw")],
        )
        == "openclaw"
    )
    assert requested_agent("搜索 OpenAI 最新消息", registry=ALL_REGISTRY) is None
    assert requested_agent("ping", registry=ALL_REGISTRY) is None
    assert (
        requested_agent("使用目前所有的agent调研 ui2v", registry=ALL_REGISTRY) is None
    )


def test_named_pi_routes_to_pi(planner: Planner):
    result = _plan(
        planner,
        "使用pi调研一款 AI 产品，整理公开资料并输出研究摘要",
        agents=ALL_AGENTS,
        ready=ALL_AGENTS,
    )
    assert result.method == "heuristic"
    assert len(result.plan.nodes) == 1
    assert result.plan.nodes[0].agent_key == "pi"
    assert result.plan.nodes[0].instruction
    assert "使用pi" not in result.plan.nodes[0].instruction
    assert "调研" in result.plan.nodes[0].instruction


def test_wants_all_agents_markers():
    assert wants_all_agents("用目前所有的agent调研 ui2v")
    assert wants_all_agents("请使用全部 agent 完成调研")
    assert wants_all_agents("use all agents to research X")
    assert not wants_all_agents("搜索 OpenAI 最新消息", ALL_REGISTRY)
    assert not wants_all_agents("使用pi调研一款 AI 产品", ALL_REGISTRY)
    # Naming ≥2 registered agents also fans out.
    assert wants_all_agents("用 pi 和 claude-code 一起调研", ALL_REGISTRY)


def test_extract_work_goal_strips_routing_preamble():
    assert "所有" not in extract_work_goal(
        "使用目前所有的agent，调研ui2v这个网页，整理公开资料并输出研究摘要",
        ALL_REGISTRY,
    )
    work = extract_work_goal(
        "使用目前所有的agent，调研ui2v这个网页，整理公开资料并输出研究摘要",
        ALL_REGISTRY,
    )
    assert "ui2v" in work
    assert "研究摘要" in work
    assert "agent" not in work.lower()
    pi_work = extract_work_goal(
        "使用pi调研一款 AI 产品，整理公开资料并输出研究摘要",
        ALL_REGISTRY,
    )
    assert "调研" in pi_work
    assert "研究摘要" in pi_work
    assert re.search(r"(?<![a-zA-Z])pi(?![a-zA-Z])", pi_work, flags=re.I) is None


def test_dispatch_brief_contains_work_not_routing():
    brief = agent_instruction(
        "claude-code",
        "调研ui2v这个网页，整理公开资料并输出研究摘要",
        parallel=True,
        label="Claude Code",
    )
    assert "调研ui2v" in brief
    assert "不要再 spawn" in brief
    assert "使用目前所有" not in brief
    assert "Claude Code" in brief
    assert "平台已并行" in brief


def test_all_agents_goal_fans_out_parallel(planner: Planner):
    result = _plan(
        planner,
        "用目前所有的agent调研一下ui2v这个项目",
        agents=ALL_AGENTS,
        ready=ALL_AGENTS,
    )
    assert result.method == "heuristic_multi"
    keys = {n.agent_key for n in result.plan.nodes}
    assert keys == ALL_AGENTS
    assert all(n.depends_on == [] for n in result.plan.nodes)
    assert len(result.plan.nodes) == 3
    for n in result.plan.nodes:
        assert n.instruction
        assert "ui2v" in n.instruction
        assert "用目前所有" not in n.instruction
        assert "独立" in n.instruction or n.agent_key in n.instruction


def test_all_agents_skips_unready_peers(planner: Planner):
    """Missing CLI/API key → runner_ready=false → only schedule ready agents."""
    result = _plan(
        planner,
        "用目前所有的agent调研 ui2v",
        agents=ALL_AGENTS,
        ready={"claude-code"},
    )
    assert result.method == "heuristic_multi"
    assert len(result.plan.nodes) == 1
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_all_agents_with_only_one_online_stays_single(planner: Planner):
    result = _plan(
        planner,
        "用目前所有的agent调研 ui2v",
        agents={"claude-code"},
        ready={"claude-code"},
    )
    assert result.method == "heuristic_multi"
    assert len(result.plan.nodes) == 1
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_supervisor_aop_node_excluded_from_fanout(planner: Planner):
    mixed = ALL_AGENTS | {"aop-node", "node-local-1"}
    result = _plan(
        planner,
        "用目前所有的agent调研 ui2v",
        agents=mixed,
        ready=mixed,
    )
    keys = {n.agent_key for n in result.plan.nodes}
    assert "aop-node" not in keys
    assert "node-local-1" not in keys
    assert keys == ALL_AGENTS


def test_no_agents_raises():
    p = Planner(database_url="postgresql://invalid/invalid")
    with patch.object(p, "list_registered_agents", return_value=[]):
        with patch.object(p, "list_ready_agents", return_value=[]):
            with pytest.raises(ValueError, match="no online agents"):
                p.plan("anything")


def test_fallback_default_agent():
    p = Planner(database_url="postgresql://invalid/invalid")
    reg = _registry_for({"claude-code"})
    with patch.object(p, "list_registered_agents", return_value=reg):
        with patch.object(p, "list_ready_agents", return_value=["claude-code"]):
            with patch.object(p, "_heuristic_nodes", return_value=[]):
                result = p.plan("noop")
    assert result.method == "fallback"
    assert result.plan.nodes[0].agent_key == "claude-code"


def test_plan_rejects_unknown_agent():
    p = Planner(database_url="postgresql://invalid/invalid")
    reg = _registry_for({"claude-code"})
    with patch.object(p, "list_registered_agents", return_value=reg):
        with patch.object(p, "list_ready_agents", return_value=["claude-code"]):
            from planner.dag import PlanNode

            bad = [PlanNode(id="a", agent_key="missing-agent")]
            with patch.object(p, "_heuristic_nodes", return_value=bad):
                with pytest.raises(DAGValidationError):
                    p.plan("x")


def test_probe_keys_ready_runs_in_parallel(monkeypatch):
    import time

    def slow(_endpoint, *, timeout_s=0.8):
        time.sleep(0.2)
        return True

    monkeypatch.setattr("planner.probe_agent_ready", slow)
    endpoints = {f"a{i}": f"http://127.0.0.1:{8011 + i}/" for i in range(8)}
    t0 = time.perf_counter()
    ready = probe_keys_ready(endpoints, endpoints)
    elapsed = time.perf_counter() - t0
    assert set(ready) == set(endpoints)
    assert elapsed < 1.0
