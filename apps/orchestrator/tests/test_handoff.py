"""Structured handoff schema + dependency-scoped upstream selection."""

from __future__ import annotations

from handoff import (
    artifact_uris_from_refs,
    build_handoff,
    parse_handoff,
    plan_dependencies,
    plan_dependents,
    prune_upstream_slices,
    select_upstream_nodes,
    truncate_text,
)


def test_build_and_parse_handoff_roundtrip():
    h = build_handoff(
        from_node="hop-a",
        to_nodes=["hop-d"],
        reason="search done; unlock report",
        artifact_ids=["s3://bucket/tasks/t/search/output.md"],
        confidence=0.9,
        agent_key="claude-code",
        agent_id="agent-1",
    )
    assert h["from"] == "hop-a"
    assert h["to"] == ["hop-d"]
    assert h["artifact_ids"][0].endswith("output.md")
    parsed = parse_handoff(h)
    assert parsed is not None
    assert parsed["reason"] == "search done; unlock report"
    assert parsed["confidence"] == 0.9


def test_parse_handoff_rejects_invalid():
    assert parse_handoff(None) is None
    assert parse_handoff({}) is None
    assert parse_handoff("x") is None


def test_plan_dependents_and_dependencies():
    plan = {
        "nodes": [
            {"id": "hop-a", "agent_key": "claude-code"},
            {"id": "hop-b", "agent_key": "deepseek-harness"},
            {"id": "hop-d", "agent_key": "hermes", "depends_on": ["hop-a", "hop-b"]},
        ]
    }
    assert plan_dependents(plan, "hop-a") == ["hop-d"]
    assert plan_dependents(plan, "hop-b") == ["hop-d"]
    assert plan_dependents(plan, "hop-d") == []
    assert set(plan_dependencies(plan, "hop-d")) == {"hop-a", "hop-b"}
    assert plan_dependencies(plan, "hop-a") == []


def test_select_upstream_nodes_filters_by_depends_on():
    plan = {
        "nodes": [
            {"id": "hop-a", "agent_key": "claude-code"},
            {"id": "hop-b", "agent_key": "deepseek-harness"},
            {"id": "hop-c", "agent_key": "pi"},
            {"id": "hop-d", "agent_key": "hermes", "depends_on": ["hop-a", "hop-b"]},
        ]
    }
    nodes = [
        {"id": "hop-a", "status": "success", "agent_key": "claude-code"},
        {"id": "hop-b", "status": "success", "agent_key": "deepseek-harness"},
        {"id": "hop-c", "status": "success", "agent_key": "pi"},
        {"id": "hop-d", "status": "ready", "agent_key": "hermes"},
    ]
    up = select_upstream_nodes(nodes=nodes, current_node_key="hop-d", plan_json=plan)
    assert {n["id"] for n in up} == {"hop-a", "hop-b"}
    # root node: no upstream
    assert select_upstream_nodes(nodes=nodes, current_node_key="hop-a", plan_json=plan) == []


def test_artifact_uris_skip_meta():
    class Ref:
        def __init__(self, name, uri):
            self.name = name
            self.uri = uri

    refs = [
        Ref("output.md", "s3://b/t/n/output.md"),
        Ref("meta.json", "s3://b/t/n/meta.json"),
        {"name": "output.json", "uri": "s3://b/t/n/output.json"},
    ]
    uris = artifact_uris_from_refs(refs)
    assert "s3://b/t/n/meta.json" not in uris
    assert "s3://b/t/n/output.md" in uris
    assert "s3://b/t/n/output.json" in uris


def test_truncate_text():
    assert truncate_text("abc", 10) == "abc"
    assert truncate_text("abcdefghij", 5).endswith("…")
    assert len(truncate_text("abcdefghij", 5)) == 5


def test_prune_upstream_slices_respects_total_budget():
    long_a = "A" * 5000
    long_b = "B" * 5000
    slices = [
        {
            "node_key": "hop-a",
            "agent_key": "claude-code",
            "reason": "search done",
            "confidence": 0.9,
            "artifact_ids": ["s3://b/t/search/output.md"],
            "body": long_a,
            "human": "",
        },
        {
            "node_key": "hop-b",
            "agent_key": "deepseek-harness",
            "reason": "rag done",
            "confidence": 0.4,
            "artifact_ids": ["s3://b/t/rag/output.md"],
            "body": long_b,
            "human": "",
        },
    ]
    pruned = prune_upstream_slices(slices, total_budget=2000, per_cap=1500)
    assert len(pruned) == 2
    # Original order preserved
    assert pruned[0]["node_key"] == "hop-a"
    assert pruned[1]["node_key"] == "hop-b"
    total_body = sum(len(s["body"]) for s in pruned)
    assert total_body < len(long_a) + len(long_b)
    assert any(s.get("pruned") for s in pruned)
    assert "[artifacts:" in pruned[0]["body"] or len(pruned[0]["body"]) <= 1500


def test_prune_prefers_high_confidence_when_budget_tight():
    slices = [
        {
            "node_key": "low",
            "agent_key": "x",
            "confidence": 0.1,
            "artifact_ids": [],
            "body": "L" * 3000,
            "human": "",
            "reason": "",
        },
        {
            "node_key": "high",
            "agent_key": "y",
            "confidence": 0.99,
            "artifact_ids": ["s3://b/high.txt"],
            "body": "H" * 3000,
            "human": "",
            "reason": "keep me",
        },
    ]
    pruned = prune_upstream_slices(slices, total_budget=800, per_cap=600)
    by_key = {s["node_key"]: s for s in pruned}
    # High-confidence slice should retain more (or equal) body than low
    assert len(by_key["high"]["body"]) >= len(by_key["low"]["body"]) // 2


def test_compose_query_prefers_handoff_artifacts():
    """ExecutionEngine._compose_query: deps-only + handoff artifact_ids."""
    from executor.engine import ExecutionEngine

    class FakeScheduler:
        def get_task(self, task_id):
            return {
                "plan_json": {
                    "nodes": [
                        {"id": "hop-a", "agent_key": "claude-code"},
                        {"id": "hop-d", "agent_key": "hermes", "depends_on": ["hop-a"]},
                    ]
                },
                "nodes": [
                    {"id": "hop-a", "status": "success", "agent_key": "claude-code"},
                    {"id": "hop-d", "status": "ready", "agent_key": "hermes"},
                ],
            }

        def get_node(self, task_id, node_key):
            if node_key == "hop-a":
                return {
                    "output_json": {
                        "text": "INLINE SHOULD NOT WIN",
                        "handoff": {
                            "from": "hop-a",
                            "to": ["hop-d"],
                            "reason": "search done; unlock report",
                            "artifact_ids": ["s3://b/t/search/output.md"],
                            "confidence": 0.9,
                        },
                        "artifacts": [
                            {"name": "output.md", "uri": "s3://b/t/search/output.md"},
                        ],
                    }
                }
            return None

    class FakeArtifacts:
        def get_text(self, uri):
            assert uri.endswith("output.md")
            return "FROM_HANDOFF_ARTIFACT"

    class FakeMemory:
        def compose_context(self, task_id):
            return ""

    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.scheduler = FakeScheduler()
    eng.artifacts = FakeArtifacts()
    eng.memory = FakeMemory()

    q = eng._compose_query("task-1", "hop-d", "research AI")
    assert "FROM_HANDOFF_ARTIFACT" in q
    assert "INLINE SHOULD NOT WIN" not in q
    assert "handoff: search done; unlock report" in q
    assert "### hop-a" in q


def test_compose_query_injects_hitl_human_input():
    from executor.engine import ExecutionEngine

    class FakeScheduler:
        def get_task(self, task_id):
            return {
                "plan_json": {
                    "nodes": [
                        {"id": "hop-d", "agent_key": "hermes"},
                        {"id": "hop-e", "agent_key": "claude-code", "depends_on": ["hop-d"]},
                    ]
                },
                "nodes": [
                    {"id": "hop-d", "status": "success", "agent_key": "hermes"},
                    {"id": "hop-e", "status": "ready", "agent_key": "claude-code"},
                ],
            }

        def get_node(self, task_id, node_key):
            if node_key == "hop-d":
                return {
                    "output_json": {
                        "text": "draft report",
                        "hitl": {
                            "approved": True,
                            "input": "请强调竞品对比与定价",
                        },
                        "artifacts": [],
                    }
                }
            return None

    class FakeArtifacts:
        def get_text(self, uri):
            raise AssertionError("should not load")

    class FakeMemory:
        def compose_context(self, task_id):
            return ""

    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.scheduler = FakeScheduler()
    eng.artifacts = FakeArtifacts()
    eng.memory = FakeMemory()

    q = eng._compose_query("task-1", "hop-e", "research AI")
    assert "Human guidance (from hop-d)" in q
    assert "请强调竞品对比与定价" in q
    assert "draft report" in q
