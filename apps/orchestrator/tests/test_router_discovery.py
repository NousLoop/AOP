"""A2A OS: open Agent discovery & routing pipeline (no database).

Verifies that ``AgentRouter.discover`` / ``route`` run the Router v3 filter
stages (skill -> capability -> IO -> health -> resource -> tenant -> policy)
and return ranked candidates an Agent can act on directly.
"""

from __future__ import annotations

from router import AgentRouter

TENANT = "00000000-0000-0000-0000-000000000001"


def _candidate(agent_key, *, skills, status="online", caps=None, priority=100, endpoint=None):
    return {
        "agent_id": f"id-{agent_key}",
        "agent_key": agent_key,
        "name": agent_key,
        "status": status,
        "priority": priority,
        "tenant_id": TENANT,
        "endpoint": endpoint if endpoint is not None else f"http://{agent_key}:8000/",
        "skills": skills,
        "card_json": {"capabilities": caps or {}},
    }


def _router_with(monkeypatch, candidates):
    router = AgentRouter(database_url="postgresql://invalid/invalid")
    router.smart = True
    monkeypatch.setattr(router, "_all_candidates_with_cards", lambda *, tenant_id=None: candidates)
    monkeypatch.setattr(router, "_load_metrics", lambda ids: {})
    return router


def test_discover_matches_by_skill_union(monkeypatch):
    router = _router_with(
        monkeypatch,
        [
            _candidate("rag-agent", skills=["knowledge-search"]),
            _candidate("search-agent", skills=["web-search"]),
            _candidate("image-agent", skills=["text-to-image"]),
        ],
    )
    result = router.discover(required_skills=["knowledge-search", "web-search"])
    keys = {c["agent_key"] for c in result["candidates"]}
    assert keys == {"rag-agent", "search-agent"}
    assert result["selected"] in {"id-rag-agent", "id-search-agent"}


def test_discover_match_all_skills_requires_subset(monkeypatch):
    router = _router_with(
        monkeypatch,
        [
            _candidate("multi", skills=["a", "b"]),
            _candidate("single", skills=["a"]),
        ],
    )
    result = router.discover(required_skills=["a", "b"], match_all_skills=True)
    assert [c["agent_key"] for c in result["candidates"]] == ["multi"]


def test_discover_capability_streaming_filter(monkeypatch):
    router = _router_with(
        monkeypatch,
        [
            _candidate("streamer", skills=["chat"], caps={"streaming": True}),
            _candidate("plain", skills=["chat"], caps={"streaming": False}),
        ],
    )
    result = router.discover(required_skills=["chat"], capabilities={"streaming": True})
    assert [c["agent_key"] for c in result["candidates"]] == ["streamer"]
    assert result["filter_stats"]["capability_match"]["passed"] == 1


def test_discover_excludes_offline_and_endpointless(monkeypatch):
    router = _router_with(
        monkeypatch,
        [
            _candidate("up", skills=["s"], status="online"),
            _candidate("down", skills=["s"], status="offline"),
            _candidate("noendpoint", skills=["s"], status="online", endpoint=""),
        ],
    )
    result = router.discover(required_skills=["s"])
    assert [c["agent_key"] for c in result["candidates"]] == ["up"]


def test_discover_exclude_agent_ids(monkeypatch):
    router = _router_with(
        monkeypatch,
        [
            _candidate("a", skills=["s"]),
            _candidate("b", skills=["s"]),
        ],
    )
    result = router.discover(required_skills=["s"], exclude_agent_ids=["id-a"])
    assert [c["agent_key"] for c in result["candidates"]] == ["b"]


def test_discover_normalizes_endpoint_for_host_runtime(monkeypatch):
    monkeypatch.setenv("AOP_RUNTIME", "host")
    router = _router_with(
        monkeypatch,
        [
            _candidate(
                "claude-code",
                skills=["knowledge-search"],
                endpoint="http://claude-code:8011/",
            ),
            _candidate(
                "rag-agent",
                skills=["knowledge-search"],
                endpoint="http://rag-agent:8002/",
            ),
        ],
    )
    result = router.discover(required_skills=["knowledge-search"])
    by_key = {c["agent_key"]: c["endpoint"] for c in result["candidates"]}
    # Harness profiles remap known docker DNS → host gateway path
    assert by_key["claude-code"] == "http://127.0.0.1:8020/agents/claude-code/"
    # Unknown service hostnames stay as registered
    assert by_key["rag-agent"] == "http://rag-agent:8002/"


def test_route_returns_selected_agent(monkeypatch):
    router = _router_with(
        monkeypatch,
        [
            _candidate("rag-agent", skills=["knowledge-search"], priority=10),
            _candidate("other", skills=["knowledge-search"], priority=90),
        ],
    )
    result = router.route(skill="knowledge-search")
    assert result["selected_agent"] is not None
    assert result["selected_agent"]["agent_key"] == result["candidates"][0]["agent_key"]


def test_route_with_no_match_returns_none(monkeypatch):
    router = _router_with(monkeypatch, [_candidate("rag-agent", skills=["knowledge-search"])])
    result = router.route(required_skills=["nonexistent-skill"])
    assert result["selected_agent"] is None
    assert result["candidates"] == []
