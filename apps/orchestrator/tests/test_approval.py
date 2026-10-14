"""Approval policy: none / system / agent / both."""

from __future__ import annotations

from approval import apply_decision, combine, parse_runtime_signal, policy_from_plan_node, resolve_for_node
from planner.dag import PlanNode, TaskPlan


def test_requires_approval_is_system_gate():
    p = policy_from_plan_node(PlanNode(id="r", agent_key="hermes", requires_approval=True))
    assert p.mode == "system"
    assert p.needs_system is True
    assert p.needs_agent is False
    assert p.wait_status() == "waiting_for_user"


def test_none_by_default():
    p = policy_from_plan_node(PlanNode(id="a", agent_key="claude-code"))
    assert p.mode == "none"
    assert p.needs_gate is False


def test_agent_mode_needs_named_peer():
    p = policy_from_plan_node(
        PlanNode(
            id="run",
            agent_key="deepseek-harness",
            approval_mode="agent",
            approver_agent="claude-code",
        )
    )
    assert p.mode == "agent"
    assert p.needs_agent is True
    assert p.needs_system is False
    assert p.wait_status() == "waiting_for_agent"


def test_agent_mode_without_approver_falls_back_to_system():
    p = policy_from_plan_node(PlanNode(id="run", agent_key="pi", approval_mode="agent"))
    assert p.mode == "system"


def test_runtime_agent_signal_upgrades_system_to_both():
    plan = policy_from_plan_node(
        PlanNode(id="r", agent_key="hermes", requires_approval=True)
    )
    runtime = parse_runtime_signal(
        {"hitl": {"mode": "agent", "approver_agent": "pi", "reason": "peer review"}}
    )
    merged = combine(plan, runtime)
    assert merged.mode == "both"
    assert merged.approver_agent == "pi"
    assert merged.wait_status() == "waiting_for_user"


def test_runtime_signal_alone_is_agent_gate():
    p = resolve_for_node(
        plan_node=PlanNode(id="a", agent_key="deepseek-harness"),
        output={"hitl": {"approver_agent": "claude-code", "reason": "needs review"}},
    )
    assert p.mode == "agent"
    assert p.approver_agent == "claude-code"


def test_apply_system_then_agent_clears_both_gate():
    p = resolve_for_node(
        plan_node=PlanNode(
            id="r",
            agent_key="hermes",
            approval_mode="both",
            approver_agent="pi",
        ),
        output=None,
    )
    after_sys = apply_decision(p, actor="system", approved=True)
    assert after_sys.needs_system is False
    assert after_sys.needs_agent is True
    assert after_sys.wait_status() == "waiting_for_agent"
    after_both = apply_decision(after_sys, actor="agent", approved=True)
    assert after_both.needs_gate is False
    assert after_both.wait_status() == "success"


def test_plan_roundtrip_keeps_approval_fields():
    plan = TaskPlan(
        title="t",
        goal="g",
        nodes=[
            PlanNode(
                id="a",
                agent_key="deepseek-harness",
                approval_mode="agent",
                approver_agent="claude-code",
            )
        ],
    )
    restored = TaskPlan.from_dict(plan.to_dict())
    assert restored.nodes[0].approval_mode == "agent"
    assert restored.nodes[0].approver_agent == "claude-code"


def test_signal_agent_approval_posts_harness_inbox(monkeypatch):
    from approval import ApprovalPolicy
    from scheduler import Scheduler

    sched = Scheduler.__new__(Scheduler)
    sched.tenant_id = "00000000-0000-0000-0000-000000000001"
    events: list[str] = []
    sched._append_event = lambda *a, **k: events.append(str(a[3]))
    sched.write_outbox_event = lambda *a, **k: None

    class _Row:
        def fetchone(self):
            return {
                "agent_id": "ag-1",
                "agent_key": "claude-code",
                "endpoint": "http://127.0.0.1:8011/",
                "name": "Claude",
            }

    class _Conn:
        def execute(self, *a, **k):
            return _Row()

    posts: list[tuple[str, dict, dict]] = []

    class _Resp:
        status_code = 202
        text = "{}"

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        posts.append((url, json or {}, headers or {}))
        return _Resp()

    monkeypatch.setenv("AOP_COLLAB_TOKEN", "tok-1")
    monkeypatch.setattr("httpx.post", fake_post)

    sched._signal_agent_approval(
        _Conn(),
        task_id="task-1",
        node_id="node-1",
        node_key="hop-a",
        policy=ApprovalPolicy(
            mode="agent", approver_agent="claude-code", reason="peer review"
        ),
        output={},
    )
    assert "agent.approval.requested" in events
    assert posts[0][0] == "http://127.0.0.1:8011/v1/approvals"
    assert posts[0][1]["task_id"] == "task-1"
    assert posts[0][1]["approver_agent"] == "claude-code"
    assert posts[0][2].get("X-AOP-Collab-Token") == "tok-1"
