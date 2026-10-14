"""Preflight classification — hermetic, no Redis/Postgres."""

from __future__ import annotations

from preflight import classify, collect_preflight, probe_agent_health


def test_classify_blocked_without_worker_and_outbox():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0},
        "redis": {"ok": True},
        "worker": {"ok": False},
        "outbox": {"ok": False},
    }
    v = classify(services, [])
    assert v["status"] == "blocked"
    assert v["can_run"] is False
    assert "worker" in v["blocking"]
    assert "outbox" in v["blocking"]
    ids = {s["id"]: s["active"] for s in v["recover_steps"]}
    assert ids["worker"] is True
    assert ids["outbox"] is True
    assert ids["orchestrator"] is False


def test_classify_ready_with_runner():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0},
        "redis": {"ok": True},
        "worker": {"ok": True},
        "outbox": {"ok": True},
    }
    agents = [
        {
            "agent_key": "claude-code",
            "reachable": True,
            "runner_ready": True,
        }
    ]
    v = classify(services, agents)
    assert v["status"] == "ready"
    assert v["can_run"] is True
    assert v["blocking"] == []


def test_classify_hosted_ready_without_cli():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0},
        "redis": {"ok": True},
        "worker": {"ok": True},
        "outbox": {"ok": True},
    }
    agents = [
        {
            "agent_key": "claude-code",
            "reachable": True,
            "runner_ready": False,
            "runner_reason": "missing CLI",
        },
        {
            "agent_key": "hermes",
            "reachable": True,
            "runner_ready": True,
            "runner_mode": "hermes",
        },
    ]
    v = classify(services, agents)
    assert v["can_run"] is True
    assert v["hosted_ready"] is True
    assert "no_runner_ready" not in v["warnings"]
    assert "partial_runners" in v["warnings"]
    ids = {s["id"]: s["active"] for s in v["recover_steps"]}
    assert ids["aopd"] is False


def test_classify_degraded_when_cli_missing():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0},
        "redis": {"ok": True},
        "worker": {"ok": True},
        "outbox": {"ok": True},
    }
    agents = [
        {
            "agent_key": "claude-code",
            "reachable": True,
            "runner_ready": False,
            "runner_reason": "missing CLI",
        }
    ]
    v = classify(services, agents)
    assert v["status"] == "degraded"
    assert v["can_run"] is True
    assert v["hosted_ready"] is False
    assert "no_runner_ready" in v["warnings"]
    ids = {s["id"]: s["active"] for s in v["recover_steps"]}
    assert ids["aopd"] is True


def test_classify_exposes_waiting_hitl_count():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0, "waiting_hitl": 3, "stuck_running": 1},
        "redis": {"ok": True},
        "worker": {"ok": True},
        "outbox": {"ok": True},
    }
    agents = [
        {"agent_key": "claude-code", "reachable": True, "runner_ready": True}
    ]
    v = classify(services, agents)
    assert v["waiting_hitl"] == 3
    assert v.get("waiting_agent", 0) == 0
    assert v["stuck_running"] == 1
    assert "waiting_hitl" not in v["warnings"]
    assert "stuck_running" in v["warnings"]
    assert v["status"] == "degraded"
    ids = {s["id"]: s["active"] for s in v["recover_steps"]}
    assert ids["stuck_running"] is True
    assert ids["worker"] is False


def test_classify_exposes_waiting_agent_count():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0, "waiting_hitl": 1, "waiting_agent": 2},
        "redis": {"ok": True},
        "worker": {"ok": True},
        "outbox": {"ok": True},
    }
    agents = [{"agent_key": "claude-code", "reachable": True, "runner_ready": True}]
    v = classify(services, agents)
    assert v["waiting_agent"] == 2
    assert v["waiting_hitl"] == 1
    assert "waiting_agent" not in v["warnings"]
    assert v["status"] == "ready"


def test_probe_agent_health_parses_runner_ready(monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            return b'{"runner_ready": false, "runner_reason": "no key"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    p = probe_agent_health("http://127.0.0.1:8011/")
    assert p["reachable"] is True
    assert p["runner_ready"] is False
    assert p["runner_reason"] == "no key"


def test_collect_preflight_uses_injected_snapshots(monkeypatch):
    monkeypatch.setattr(
        "preflight._postgres_snapshot",
        lambda: {
            "ok": True,
            "pending_outbox": 2,
            "oldest_pending_s": 5,
            "last_processed_at": None,
            "stuck_running": 0,
        },
    )
    monkeypatch.setattr(
        "preflight._redis_snapshot",
        lambda: {
            "ok": True,
            "queued": 0,
            "worker_ok": True,
            "outbox_ok": True,
            "worker_id": "executor-test",
        },
    )
    monkeypatch.setattr(
        "preflight._list_registered_agents",
        lambda: [{"agent_key": "pi", "name": "Pi", "status": "online", "endpoint": "http://127.0.0.1:8013/"}],
    )
    monkeypatch.setattr(
        "preflight.probe_agent_health",
        lambda endpoint, timeout_s=1.5: {
            "reachable": True,
            "runner_ready": True,
            "runner_reason": None,
        },
    )
    snap = collect_preflight()
    assert snap["status"] == "ready"
    assert snap["can_run"] is True
    assert snap["agents"][0]["agent_key"] == "pi"
    assert snap["services"]["worker"]["ok"] is True
    assert snap["services"]["outbox"]["pending"] == 2


def test_classify_warns_stuck_running():
    services = {
        "postgres": {"ok": True, "pending_outbox": 0, "stuck_running": 3},
        "redis": {"ok": True},
        "worker": {"ok": True},
        "outbox": {"ok": True},
    }
    agents = [{"agent_key": "pi", "reachable": True, "runner_ready": True}]
    v = classify(services, agents)
    assert v["status"] == "degraded"
    assert v["can_run"] is True
    assert "stuck_running" in v["warnings"]
