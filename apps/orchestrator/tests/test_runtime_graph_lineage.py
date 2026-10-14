"""Phase 2 — branch_lineage limited revisits + collaboration_graph shaping."""

from __future__ import annotations

from runtime_graph import RuntimeGraphService


def test_branch_lineage_counts_limited_revisits(monkeypatch):
    svc = RuntimeGraphService.__new__(RuntimeGraphService)
    edges = [
        {
            "id": 1,
            "root_task_id": "root",
            "parent_task_id": None,
            "task_id": "t-b",
            "correlation_id": "c",
            "caller_agent_id": "agent-a",
            "target_agent_id": "agent-b",
            "skill": "s",
            "depth": 1,
            "status": "completed",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": 2,
            "root_task_id": "root",
            "parent_task_id": "t-b",
            "task_id": "t-a2",
            "correlation_id": "c",
            "caller_agent_id": "agent-b",
            "target_agent_id": "agent-a",
            "skill": "s2",
            "depth": 2,
            "status": "completed",
            "created_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    monkeypatch.setattr(svc, "list_edges", lambda root, limit=500: edges)

    lin = svc.branch_lineage("root", "t-a2")
    assert lin["agent_chain"] == ["agent-a", "agent-b", "agent-a"]
    assert lin["agent_visits"]["agent-a"] == 2
    assert lin["agent_visits"]["agent-b"] == 1


def test_collaboration_graph_shapes_nodes_and_links(monkeypatch):
    svc = RuntimeGraphService.__new__(RuntimeGraphService)
    edges = [
        {
            "id": 1,
            "root_task_id": "root",
            "parent_task_id": None,
            "task_id": "t-b",
            "caller_agent_id": "agent-a",
            "target_agent_id": "agent-b",
            "skill": "s",
            "depth": 1,
            "status": "completed",
            "correlation_id": "c",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    monkeypatch.setattr(svc, "list_edges", lambda root, limit=500: edges)
    monkeypatch.setattr(svc, "_agent_directory", lambda: {})
    g = svc.collaboration_graph("root")
    assert g["kind"] == "collaboration_graph"
    assert g["link_count"] == 1
    assert any(n["type"] == "agent" and n["id"] == "agent-a" for n in g["nodes"])
    assert any(n["type"] == "task" for n in g["nodes"])
    assert g["links"][0]["source"] == "agent-a"
    assert g["links"][0]["target"] == "agent-b"


def test_collaboration_graph_merges_key_and_uuid(monkeypatch):
    """Caller keys and target UUIDs for the same agent must collapse to one node."""
    svc = RuntimeGraphService.__new__(RuntimeGraphService)
    analysis_id = "194bc8f1-ee92-413f-a69e-4f4a8affa74b"
    rag_id = "baeefa35-1b76-41da-bf08-38cd03160fe9"
    edges = [
        {
            "id": 1,
            "root_task_id": "root",
            "parent_task_id": None,
            "task_id": "t1",
            "caller_agent_id": "search-agent",
            "target_agent_id": analysis_id,
            "skill": "business-analysis",
            "depth": 1,
            "status": "completed",
            "correlation_id": "c",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": 2,
            "root_task_id": "root",
            "parent_task_id": "t1",
            "task_id": "t2",
            "caller_agent_id": "analysis-agent",
            "target_agent_id": rag_id,
            "skill": "knowledge-search",
            "depth": 2,
            "status": "completed",
            "correlation_id": "c",
            "created_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    directory = {
        "search-agent": {"id": "3853d2a2-0000-0000-0000-000000000001", "key": "search-agent", "name": "Search Agent"},
        "3853d2a2-0000-0000-0000-000000000001": {
            "id": "3853d2a2-0000-0000-0000-000000000001",
            "key": "search-agent",
            "name": "Search Agent",
        },
        "analysis-agent": {"id": analysis_id, "key": "analysis-agent", "name": "Analysis Agent"},
        analysis_id: {"id": analysis_id, "key": "analysis-agent", "name": "Analysis Agent"},
        "rag-agent": {"id": rag_id, "key": "rag-agent", "name": "RAG Agent"},
        rag_id: {"id": rag_id, "key": "rag-agent", "name": "RAG Agent"},
    }
    monkeypatch.setattr(svc, "list_edges", lambda root, limit=500: edges)
    monkeypatch.setattr(svc, "_agent_directory", lambda: directory)
    g = svc.collaboration_graph("root")
    agents = [n for n in g["nodes"] if n["type"] == "agent"]
    tasks = [n for n in g["nodes"] if n["type"] == "task"]
    assert len(agents) == 3
    assert len(tasks) == 2
    assert {a["id"] for a in agents} == {
        "3853d2a2-0000-0000-0000-000000000001",
        analysis_id,
        rag_id,
    }
    assert g["links"][0]["source"] == "3853d2a2-0000-0000-0000-000000000001"
    assert g["links"][0]["target"] == analysis_id
    assert g["links"][1]["source"] == analysis_id
    assert g["links"][1]["target"] == rag_id
    assert any(a["label"] == "Analysis Agent" for a in agents)


def test_collaboration_from_plan_builds_mid_task_hops(monkeypatch):
    svc = RuntimeGraphService.__new__(RuntimeGraphService)
    monkeypatch.setattr(svc, "_agent_directory", lambda: {
        "3853d2a2-aaaa-bbbb-cccc-000000000001": {
            "id": "3853d2a2-aaaa-bbbb-cccc-000000000001",
            "key": "search-agent",
            "name": "Search Agent",
        },
        "baeefa35-aaaa-bbbb-cccc-000000000002": {
            "id": "baeefa35-aaaa-bbbb-cccc-000000000002",
            "key": "rag-agent",
            "name": "RAG Agent",
        },
        "85cc6a4d-aaaa-bbbb-cccc-000000000003": {
            "id": "85cc6a4d-aaaa-bbbb-cccc-000000000003",
            "key": "report-agent",
            "name": "Report Agent",
        },
    })
    row = {
        "id": "task-root",
        "plan_json": {
            "nodes": [
                {"id": "hop-a", "agent_key": "claude-code"},
                {"id": "hop-b", "agent_key": "deepseek-harness"},
                {"id": "hop-d", "agent_key": "hermes", "depends_on": ["hop-a", "hop-b"]},
            ]
        },
        "nodes": [
            {"id": "hop-a", "agent_key": "claude-code", "status": "success",
             "agent_id": "3853d2a2-aaaa-bbbb-cccc-000000000001",
             "handoff": {
                 "from": "hop-a", "to": ["hop-d"],
                 "reason": "search done; unlock report",
                 "artifact_ids": ["s3://b/t/search/output.md"],
                 "confidence": 0.9,
             }},
            {"id": "hop-b", "agent_key": "deepseek-harness", "status": "success",
             "agent_id": "baeefa35-aaaa-bbbb-cccc-000000000002",
             "handoff": {
                 "from": "hop-b", "to": ["hop-d"],
                 "reason": "rag done; unlock report",
                 "artifact_ids": ["s3://b/t/rag/output.md"],
                 "confidence": 0.85,
             }},
            {"id": "hop-d", "agent_key": "hermes", "status": "success",
             "agent_id": "85cc6a4d-aaaa-bbbb-cccc-000000000003"},
        ],
    }
    g = svc.collaboration_from_plan(row)
    assert g["source"] == "plan_synthesis"
    assert g["link_count"] >= 3
    tasks = [n for n in g["nodes"] if n["type"] == "task"]
    assert len(tasks) == 3
    # every link has a mid task_id that exists as task: node
    task_ids = {n["task_id"] for n in tasks}
    for link in g["links"]:
        assert link["task_id"] in task_ids
        assert link["source"]
        assert link["target"]
    # handoff.reason from upstream search node surfaces on report edges
    hop_d_links = [l for l in g["links"] if "hop-d" in str(l.get("task_id"))]
    assert any(
        (l.get("reason") or "").startswith("search done") for l in hop_d_links
    )
    # task nodes carry handoff + artifact_ids for console (中间传递 / 产物)
    hop_a_task = next(n for n in tasks if n.get("plan_node_id") == "hop-a")
    assert hop_a_task.get("handoff_reason", "").startswith("search done")
    assert any(
        str(a).endswith("output.md") for a in (hop_a_task.get("artifact_ids") or [])
    )
    # inbound hop-d links expose upstream-passed artifact_ids (not mid outputs)
    assert any(
        (l.get("artifact_ids") or []) and "search/output.md" in str(l.get("artifact_ids"))
        for l in hop_d_links
    )
    # orchestrator → leaf fan-out must NOT pretend to pass the node's own outputs
    root_links = [
        l
        for l in g["links"]
        if str(l.get("source")) in {"orchestrator", "aop-orchestrator"}
        or str(l.get("kind")) == "dispatch"
    ]
    for l in root_links:
        assert not (l.get("artifact_ids") or []), l
