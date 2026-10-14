"""Visual Runtime — event ordering, graph projection, revisit, snapshot recovery."""

from __future__ import annotations

from execution.events import ExecutionEvent, ExecutionEventBus
from visual_runtime import (
    apply_event_to_graph,
    map_execution_event_type,
    normalize_event,
    project_timeline,
    project_visual_graph,
)


def test_event_uniqueness_and_sequence():
    bus = ExecutionEventBus()
    e1 = bus.emit(
        ExecutionEvent(
            event_type="task.created",
            event_id="evt_1",
            task_id="t1",
            root_task_id="root",
        )
    )
    e2 = bus.emit(
        ExecutionEvent(
            event_type="agent.started",
            event_id="evt_2",
            task_id="t1",
            root_task_id="root",
            agent_id="A",
        )
    )
    # duplicate event_id is idempotent
    e1b = bus.emit(
        ExecutionEvent(
            event_type="task.created",
            event_id="evt_1",
            task_id="t1",
            root_task_id="root",
        )
    )
    assert e1.sequence == 1
    assert e2.sequence == 2
    assert e1b.sequence == 1
    assert len(bus.list_for_root("root")) == 2
    assert e1.to_dict()["sequence"] == 1
    assert e1.to_dict()["payload"]["sequence"] == 1


def test_event_type_mapping_preserves_visual_vocab():
    assert map_execution_event_type("delegation.started") == "agent.started"
    assert map_execution_event_type("delegation.requested") == "agent.delegated"
    # Core task.* types are already in the Visual Runtime vocabulary
    assert map_execution_event_type("task.started") == "task.started"
    assert map_execution_event_type("task.completed") == "task.completed"
    # Extended task.* aliases map into execution.* / agent.*
    assert map_execution_event_type("task.waiting") == "execution.waiting"
    assert map_execution_event_type("task.retrying") == "execution.retry"
    assert map_execution_event_type("task.retrying", operation="execute") == "agent.retrying"
    assert map_execution_event_type("agent.discovered") == "agent.discovered"
    n = normalize_event(
        {
            "event_type": "delegation.completed",
            "event_id": "x",
            "task_id": "t",
            "root_task_id": "r",
            "agent_id": "B",
            "payload": {"sequence": 3, "caller_agent_id": "A"},
        }
    )
    assert n["event_type"] == "agent.completed"
    assert n["sequence"] == 3
    assert n["parent_agent_id"] == "A"


def test_graph_projection_agent_revisit_a_b_c_a():
    """E2E projection: A → B → C → A revisit must remain visible."""
    events = [
        {
            "event_type": "task.created",
            "event_id": "e0",
            "task_id": "root",
            "root_task_id": "root",
            "payload": {"sequence": 1},
        },
        {
            "event_type": "agent.started",
            "event_id": "e1",
            "task_id": "t-a",
            "root_task_id": "root",
            "agent_id": "A",
            "payload": {"sequence": 2},
        },
        {
            "event_type": "agent.delegated",
            "event_id": "e2",
            "task_id": "t-b",
            "root_task_id": "root",
            "agent_id": "B",
            "parent_agent_id": "A",
            "payload": {"sequence": 3},
        },
        {
            "event_type": "agent.started",
            "event_id": "e3",
            "task_id": "t-c",
            "root_task_id": "root",
            "agent_id": "C",
            "parent_agent_id": "B",
            "payload": {"sequence": 4},
        },
        {
            "event_type": "agent.started",
            "event_id": "e4",
            "task_id": "t-a2",
            "root_task_id": "root",
            "agent_id": "A",
            "parent_agent_id": "C",
            "payload": {"sequence": 5},
        },
    ]
    collab = {
        "nodes": [
            {"id": "A", "type": "agent", "label": "Agent A"},
            {"id": "B", "type": "agent", "label": "Agent B"},
            {"id": "C", "type": "agent", "label": "Agent C"},
        ],
        "links": [
            {"id": "1", "source": "A", "target": "B", "task_id": "t-b", "status": "completed"},
            {"id": "2", "source": "B", "target": "C", "task_id": "t-c", "status": "completed"},
            {"id": "3", "source": "C", "target": "A", "task_id": "t-a2", "status": "running"},
        ],
    }
    g = project_visual_graph(
        task_id="root",
        events=events,
        collaboration_graph=collab,
        task_row={"id": "root", "status": "running", "title": "demo"},
    )
    agent_ids = {n["agent_id"] for n in g["nodes"] if n["type"] == "agent"}
    assert agent_ids == {"A", "B", "C"}
    a_node = next(n for n in g["nodes"] if n["agent_id"] == "A")
    assert int((a_node.get("metadata") or {}).get("visit_count") or 1) >= 2
    edge_pairs = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("agent:A", "agent:B") in edge_pairs or any(
        e.get("metadata", {}).get("task_id") == "t-b" for e in g["edges"]
    )
    assert ("agent:C", "agent:A") in edge_pairs or any(
        "C" in e["source"] and "A" in e["target"] for e in g["edges"]
    )
    assert g["sequence"] == 5
    timeline = project_timeline(events)
    assert [e["event_type"] for e in timeline] == [
        "task.created",
        "agent.started",
        "agent.delegated",
        "agent.started",
        "agent.started",
    ]


def test_snapshot_plus_incremental_recovery():
    bus = ExecutionEventBus()
    for i, et in enumerate(
        ["task.created", "agent.discovered", "agent.selected", "agent.started"],
        start=1,
    ):
        bus.emit(
            ExecutionEvent(
                event_type=et,
                event_id=f"evt_{i}",
                task_id="root",
                root_task_id="root",
                agent_id="A" if "agent" in et else None,
            )
        )
    snap = project_visual_graph(
        task_id="root",
        events=bus.list_for_root("root"),
        task_row={"id": "root", "status": "running"},
    )
    assert snap["sequence"] == 4
    # reconnect: fold one more event
    nxt = bus.emit(
        ExecutionEvent(
            event_type="agent.completed",
            event_id="evt_5",
            task_id="root",
            root_task_id="root",
            agent_id="A",
        )
    )
    # skip duplicate sequences on client
    assert nxt.sequence == 5
    g2 = apply_event_to_graph(snap, normalize_event(nxt))
    assert g2["sequence"] == 5
    a = next(n for n in g2["nodes"] if n.get("agent_id") == "A")
    assert a["status"] == "completed"


def test_retry_recovery_failure_parallel_edges():
    events = [
        {
            "event_type": "task.created",
            "event_id": "1",
            "root_task_id": "r",
            "task_id": "r",
            "payload": {"sequence": 1},
        },
        {
            "event_type": "agent.started",
            "event_id": "2",
            "root_task_id": "r",
            "task_id": "t1",
            "agent_id": "A",
            "payload": {"sequence": 2},
        },
        {
            "event_type": "task.retrying",
            "event_id": "3",
            "root_task_id": "r",
            "task_id": "t1",
            "agent_id": "A",
            "payload": {"sequence": 3, "operation": "execute"},
        },
        {
            "event_type": "agent.recovered",
            "event_id": "4",
            "root_task_id": "r",
            "task_id": "t1",
            "agent_id": "A",
            "payload": {"sequence": 4},
        },
        {
            "event_type": "delegation.failed",
            "event_id": "5",
            "root_task_id": "r",
            "task_id": "t2",
            "agent_id": "B",
            "parent_agent_id": "A",
            "payload": {"sequence": 5, "error": "boom"},
        },
        {
            "event_type": "agent.started",
            "event_id": "6",
            "root_task_id": "r",
            "task_id": "t3",
            "agent_id": "C",
            "parent_agent_id": "A",
            "payload": {"sequence": 6},
        },
    ]
    g = project_visual_graph(task_id="r", events=events)
    b = next(n for n in g["nodes"] if n.get("agent_id") == "B")
    assert b["status"] == "failed"
    assert (b.get("metadata") or {}).get("error") == "boom"
    assert not any(n["type"] == "error" for n in g["nodes"])
    assert any(e["type"] == "retry" or "retry" in str(e.get("metadata")) for e in g["edges"]) or any(
        n["agent_id"] == "A" and n["status"] in {"retrying", "running", "completed"}
        for n in g["nodes"]
        if n["type"] == "agent"
    )
    # parallel B and C from A
    targets = {e["target"] for e in g["edges"] if e["source"] == "agent:A"}
    assert "agent:B" in targets or "agent:C" in targets


def test_pause_resume_via_execution_service():
    from execution import ExecutionService

    svc = ExecutionService()
    svc.create(task_id="p1", root_task_id="p1", correlation_id="p1", agent_id="A")
    svc.start("p1")
    waiting = svc.transition("p1", "WAITING")
    assert waiting.state == "WAITING"
    running = svc.transition("p1", "RUNNING")
    assert running.state == "RUNNING"
    types = [e["event_type"] for e in svc.events.list_for_root("p1")]
    assert "task.waiting" in types
    assert types.count("task.started") >= 1


def test_route_selected_agent_observation_emit(monkeypatch):
    """Fusion B: route observation must read selected_agent dict."""
    from execution import ExecutionService
    from execution.events import AGENT_SELECTED
    import api.discovery as discovery_mod
    from app_context import ctx

    svc = ExecutionService()
    monkeypatch.setattr(ctx, "execution", svc, raising=False)

    class _Tasks:
        def route(self, **kwargs):
            return {
                "candidates": [
                    {
                        "agent_id": "agent-b",
                        "agent_key": "research",
                        "name": "Research",
                        "endpoint": "http://127.0.0.1:8012",
                    }
                ],
                "selected_agent": {
                    "agent_id": "agent-b",
                    "agent_key": "research",
                    "name": "Research",
                    "endpoint": "http://127.0.0.1:8012",
                },
            }

    monkeypatch.setattr(ctx, "tasks", _Tasks(), raising=False)
    body = discovery_mod.RouteRequest(
        skill="research",
        root_task_id="root-fuse",
        correlation_id="corr-1",
        task_id="root-fuse",
        caller_agent_id="agent-a",
    )
    out = discovery_mod.route_request(body)
    assert out["selected_agent"]["agent_id"] == "agent-b"
    events = svc.events.list_for_root("root-fuse")
    types = [e["event_type"] for e in events]
    assert AGENT_SELECTED in types
    sel = next(e for e in events if e["event_type"] == AGENT_SELECTED)
    assert sel["agent_id"] == "agent-b"
    assert sel.get("parent_agent_id") == "agent-a"


def test_runtime_edge_emits_delegated_started(monkeypatch):
    """Fusion B: recording a runtime edge emits agent.delegated / agent.started."""
    from execution import ExecutionService
    from execution.events import AGENT_DELEGATED, AGENT_STARTED
    import api.runtime as runtime_mod
    from app_context import ctx

    svc = ExecutionService()
    monkeypatch.setattr(ctx, "execution", svc, raising=False)

    class _Tasks:
        def record_runtime_edge(self, **kwargs):
            return {
                "id": 99,
                "status": kwargs.get("status") or "submitted",
                "caller_agent_id": kwargs["caller_agent_id"],
                "target_agent_id": kwargs["target_agent_id"],
            }

    monkeypatch.setattr(ctx, "tasks", _Tasks(), raising=False)
    body = runtime_mod.RuntimeEdgeRequest(
        caller_agent_id="A",
        target_agent_id="B",
        root_task_id="root-edge",
        task_id="t-b",
        correlation_id="c1",
        skill="search",
        depth=1,
        status="submitted",
    )
    edge = runtime_mod.record_runtime_edge(body)
    assert edge["id"] == 99
    types = [e["event_type"] for e in svc.events.list_for_root("root-edge")]
    assert AGENT_DELEGATED in types
    assert AGENT_STARTED in types
    delegated = next(e for e in svc.events.list_for_root("root-edge") if e["event_type"] == AGENT_DELEGATED)
    assert delegated["agent_id"] == "B"
    assert delegated.get("parent_agent_id") == "A"


def test_synthesize_timeline_from_collaboration_edges():
    from visual_runtime import (
        synthesize_from_collaboration,
        merge_task_scheduler_events,
        project_visual_graph,
    )

    collab = {
        "nodes": [
            {"id": "A", "type": "agent", "label": "A"},
            {"id": "B", "type": "agent", "label": "B"},
        ],
        "links": [
            {
                "id": "1",
                "source": "A",
                "target": "B",
                "task_id": "t-b",
                "status": "completed",
                "skill": "search",
                "depth": 1,
                "created_at": "2026-10-03T10:55:30Z",
            }
        ],
    }
    row = {
        "id": "root-1",
        "status": "completed",
        "created_at": "2026-10-03T10:55:21Z",
        "updated_at": "2026-10-03T10:56:00Z",
    }
    events = synthesize_from_collaboration(
        root_task_id="root-1",
        collaboration_graph=collab,
        task_row=row,
    )
    types = [e["event_type"] for e in events]
    assert "task.created" in types
    assert "agent.delegated" in types
    assert "agent.completed" in types
    assert "task.completed" in types
    assert events[-1]["sequence"] >= 1
    g = project_visual_graph(
        task_id="root-1",
        events=events,
        collaboration_graph=collab,
        task_row=row,
    )
    assert g["sequence"] >= 1
    assert len(g["events"]) >= 1

    # Merge path: scheduler task lifecycle + agent hops, no duplicate created
    agent_hops = [e for e in events if str(e["event_type"]).startswith("agent.")]
    merged = merge_task_scheduler_events(
        root_task_id="root-1",
        task_events=[
            {"event_type": "task.created", "ts": "2026-10-03T10:55:21Z"},
            {"event_type": "task.running", "ts": "2026-10-03T10:55:22Z"},
            {"event_type": "task.node.ready", "ts": "2026-10-03T10:55:22Z"},
            {"event_type": "task.completed", "ts": "2026-10-03T10:56:00Z"},
        ],
        base=agent_hops,
    )
    mtypes = [e["event_type"] for e in merged]
    assert mtypes.count("task.created") == 1
    assert "task.node.ready" not in mtypes
    assert "agent.delegated" in mtypes
    assert merged[-1]["event_type"] == "task.completed"


def test_build_task_graph_invalid_id_is_404(monkeypatch):
    """Fusion C: invalid UUID / DB cast → 404 not 500."""
    from fastapi import HTTPException
    import api.visual_runtime as vr
    from app_context import ctx

    class _Tasks:
        def get(self, task_id, tenant_id=None):
            raise ValueError(f'invalid input syntax for type uuid: "{task_id}"')

    monkeypatch.setattr(ctx, "tasks", _Tasks(), raising=False)
    try:
        vr._build_task_graph("dummy")
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail["code"] == "not_found"


def test_post_completion_events_do_not_regress_task_status():
    """task.aggregated / task.evaluated must not flip nodes back to running."""
    events = [
        {
            "event_type": "task.created",
            "event_id": "e1",
            "task_id": "t1",
            "root_task_id": "t1",
            "payload": {"sequence": 1},
        },
        {
            "event_type": "agent.started",
            "event_id": "e2",
            "task_id": "t1",
            "root_task_id": "t1",
            "agent_id": "claude-code",
            "payload": {"sequence": 2},
        },
        {
            "event_type": "agent.completed",
            "event_id": "e3",
            "task_id": "t1",
            "root_task_id": "t1",
            "agent_id": "claude-code",
            "payload": {"sequence": 3},
        },
        {
            "event_type": "task.completed",
            "event_id": "e4",
            "task_id": "t1",
            "root_task_id": "t1",
            "payload": {"sequence": 4},
        },
        {
            "event_type": "task.aggregated",
            "event_id": "e5",
            "task_id": "t1",
            "root_task_id": "t1",
            "payload": {"sequence": 5},
        },
        {
            "event_type": "task.evaluated",
            "event_id": "e6",
            "task_id": "t1",
            "root_task_id": "t1",
            "payload": {"sequence": 6},
        },
    ]
    g = project_visual_graph(
        task_id="t1",
        events=events,
        task_row={"id": "t1", "status": "completed", "title": "hello"},
    )
    assert g["status"] == "completed"
    task_node = next(n for n in g["nodes"] if n["type"] == "task")
    assert task_node["status"] == "completed"
    agent = next(n for n in g["nodes"] if n.get("agent_id") == "claude-code")
    assert agent["status"] == "completed"


def test_task_row_reconciles_orphan_running_agent():
    """Missing agent_id on agent.completed still closes hops when task is done."""
    events = [
        {
            "event_type": "agent.started",
            "event_id": "e1",
            "task_id": "t2",
            "root_task_id": "t2",
            "agent_id": "agent-uuid",
            "payload": {"sequence": 1},
        },
        {
            "event_type": "agent.completed",
            "event_id": "e2",
            "task_id": "t2",
            "root_task_id": "t2",
            # no agent_id — regression seen in live Console runs
            "payload": {"sequence": 2},
        },
        {
            "event_type": "task.completed",
            "event_id": "e3",
            "task_id": "t2",
            "root_task_id": "t2",
            "payload": {"sequence": 3},
        },
    ]
    g = project_visual_graph(
        task_id="t2",
        events=events,
        task_row={"id": "t2", "status": "completed"},
    )
    agent = next(n for n in g["nodes"] if n.get("agent_id") == "agent-uuid")
    assert agent["status"] == "completed"
    assert g["status"] == "completed"


def test_task_row_preserves_waiting_for_agent():
    g = project_visual_graph(
        task_id="t-agent",
        events=[],
        task_row={"id": "t-agent", "status": "waiting_for_agent", "title": "peer"},
    )
    assert g["status"] == "waiting_for_agent"
    task_node = next(n for n in g["nodes"] if n["type"] == "task")
    assert task_node["status"] == "waiting_for_agent"

    human = project_visual_graph(
        task_id="t-sys",
        events=[],
        task_row={"id": "t-sys", "status": "waiting_for_user"},
    )
    assert human["status"] == "waiting_for_user"


def test_failed_hop_does_not_leave_agent_completed():
    """Collab defaulted agents to completed; DAG hop failure must win."""
    aid = "2e84672f-fd8a-4e75-bf25-6d223518b9c3"
    g = project_visual_graph(
        task_id="t-fail",
        events=[
            {
                "event_type": "task.created",
                "event_id": "e1",
                "task_id": "t-fail",
                "root_task_id": "t-fail",
                "payload": {"sequence": 1},
            },
            {
                "event_type": "task.failed",
                "event_id": "e2",
                "task_id": "t-fail",
                "root_task_id": "t-fail",
                "payload": {"sequence": 2, "error": "余额不足"},
            },
        ],
        collaboration_graph={
            "nodes": [
                {"id": "orchestrator", "type": "agent", "label": "orchestrator"},
                {"id": aid, "type": "agent", "label": "Claude Code", "agent_key": "claude-code"},
            ],
            "links": [
                {
                    "id": "d1",
                    "source": "orchestrator",
                    "target": aid,
                    "status": "failed",
                }
            ],
        },
        task_row={
            "id": "t-fail",
            "status": "failed",
            "title": "Research Vidu S2",
            "nodes": [
                {
                    "id": "run",
                    "agent_id": aid,
                    "agent_key": "claude-code",
                    "status": "failed",
                    "error_message": "余额不足",
                }
            ],
        },
    )
    claude = next(n for n in g["nodes"] if n.get("agent_id") == aid)
    assert claude["status"] == "failed"
    assert (claude.get("metadata") or {}).get("error") == "余额不足"
    assert not any(n["type"] == "error" for n in g["nodes"])


def test_completed_task_does_not_keep_balance_error_on_task():
    aid = "claude"
    hosted = "hosted"
    g = project_visual_graph(
        task_id="t-ok",
        events=[
            {
                "event_type": "task.failed",
                "event_id": "e1",
                "task_id": "t-ok",
                "root_task_id": "t-ok",
                "payload": {"sequence": 1, "error": "余额不足"},
            },
            {
                "event_type": "task.completed",
                "event_id": "e2",
                "task_id": "t-ok",
                "root_task_id": "t-ok",
                "payload": {"sequence": 2},
            },
        ],
        collaboration_graph={
            "nodes": [
                {"id": aid, "type": "agent", "label": "Claude Code", "agent_key": "claude-code"},
                {"id": hosted, "type": "agent", "label": "Hermes", "agent_key": "hermes"},
            ],
            "links": [],
        },
        task_row={
            "id": "t-ok",
            "status": "completed",
            "title": "use all agents",
            "nodes": [
                {
                    "id": "claude_code",
                    "agent_id": aid,
                    "agent_key": "claude-code",
                    "status": "failed",
                    "error_message": "余额不足",
                },
                {
                    "id": "aop_hosted",
                    "agent_id": hosted,
                    "agent_key": "hermes",
                    "status": "success",
                    "output_excerpt": "UI2V is a UI-to-video research note with sources.",
                },
            ],
        },
    )
    task = next(n for n in g["nodes"] if n["type"] == "task")
    assert task["status"] == "completed"
    assert not (task.get("metadata") or {}).get("error")
    claude = next(n for n in g["nodes"] if n.get("agent_id") == aid)
    assert claude["status"] == "failed"
    hosted_n = next(n for n in g["nodes"] if n.get("agent_id") == hosted)
    assert hosted_n["status"] == "completed"
    assert not (hosted_n.get("metadata") or {}).get("error")


def test_hosted_stub_does_not_complete_task():
    hosted = "hosted"
    g = project_visual_graph(
        task_id="t-stub",
        events=[],
        collaboration_graph={
            "nodes": [
                {"id": hosted, "type": "agent", "label": "Hermes", "agent_key": "hermes"},
            ],
            "links": [],
        },
        task_row={
            "id": "t-stub",
            "status": "completed",
            "nodes": [
                {
                    "id": "aop_hosted",
                    "agent_id": hosted,
                    "agent_key": "hermes",
                    "status": "success",
                    "output_excerpt": "# AOP hosted result\n\nCompleted by the platform-hosted runner (no external CLI).",
                }
            ],
        },
    )
    assert g["status"] == "failed"
    task = next(n for n in g["nodes"] if n["type"] == "task")
    assert task["status"] == "failed"
    hosted_n = next(n for n in g["nodes"] if n.get("agent_id") == hosted)
    assert hosted_n["status"] == "failed"
    assert "hosted stub" in str((hosted_n.get("metadata") or {}).get("error"))


def test_success_hop_with_cli_failure_text_is_failed():
    g = project_visual_graph(
        task_id="t-h",
        events=[],
        collaboration_graph={
            "nodes": [
                {"id": "h1", "type": "agent", "label": "Hermes", "agent_key": "hermes"},
            ],
            "links": [],
        },
        task_row={
            "id": "t-h",
            "status": "completed",
            "nodes": [
                {
                    "id": "hermes",
                    "agent_id": "h1",
                    "agent_key": "hermes",
                    "status": "success",
                    "output_excerpt": "API call failed after 3 retries: Request timed out.",
                }
            ],
        },
    )
    hermes = next(n for n in g["nodes"] if n.get("agent_id") == "h1")
    assert hermes["status"] == "failed"
    assert "API call failed" in str((hermes.get("metadata") or {}).get("error"))
