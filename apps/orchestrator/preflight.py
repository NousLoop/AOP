"""Operator preflight: can a run actually move, or will it stall at ready?"""

from __future__ import annotations

from typing import Any

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url
from db import connect
from planner import is_work_agent
from pulse import TTL_S, read_pulses

HINT_DEV_UP = "python scripts/dev_up.py"


def _svc(ok: bool, **extra: Any) -> dict[str, Any]:
    return {"ok": bool(ok), **extra}


def _postgres_snapshot() -> dict[str, Any]:
    try:
        with connect(resolve_database_url()) as conn:
            conn.execute("SELECT 1")
            pending = failed = 0
            oldest = None
            last_processed = None
            try:
                row = conn.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                      COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                      EXTRACT(EPOCH FROM (
                        NOW() AT TIME ZONE 'utc' - MIN(created_at)
                          FILTER (WHERE status = 'pending')
                      )) AS oldest_pending_s,
                      MAX(processed_at) FILTER (WHERE status = 'processed') AS last_processed_at
                    FROM outbox_events
                    """
                ).fetchone()
                if row:
                    pending = int(row.get("pending") or 0)
                    failed = int(row.get("failed") or 0)
                    oldest = row.get("oldest_pending_s")
                    if oldest is not None:
                        oldest = float(oldest)
                    lp = row.get("last_processed_at")
                    last_processed = lp.isoformat() if lp is not None else None
            except Exception as exc:  # noqa: BLE001
                return _svc(True, outbox_table="missing", detail=str(exc))
            stuck_running = 0
            waiting_hitl = 0
            waiting_agent = 0
            try:
                trow = conn.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE status IN ('running', 'planning')) AS stuck_running,
                      COUNT(*) FILTER (WHERE status = 'waiting_for_user') AS waiting_hitl,
                      COUNT(*) FILTER (WHERE status = 'waiting_for_agent') AS waiting_agent
                    FROM tasks
                    """
                ).fetchone()
                if trow:
                    stuck_running = int(trow.get("stuck_running") or 0)
                    waiting_hitl = int(trow.get("waiting_hitl") or 0)
                    waiting_agent = int(trow.get("waiting_agent") or 0)
            except Exception:  # noqa: BLE001
                stuck_running = 0
                waiting_hitl = 0
                waiting_agent = 0
        return _svc(
            True,
            pending_outbox=pending,
            failed_outbox=failed,
            oldest_pending_s=oldest,
            last_processed_at=last_processed,
            stuck_running=stuck_running,
            waiting_hitl=waiting_hitl,
            waiting_agent=waiting_agent,
        )
    except Exception as exc:  # noqa: BLE001
        return _svc(False, detail=str(exc), hint="Start Postgres: docker compose -f deployments/docker-compose.yml up -d postgres")


def _redis_snapshot() -> dict[str, Any]:
    try:
        from pulse import _redis

        r = _redis()
        pulses = read_pulses(r)
        queued = 0
        try:
            queued = int(r.xlen("a2a.execution.queue") or 0)
        except Exception:  # noqa: BLE001
            queued = 0
        return _svc(True, queued=queued, **pulses)
    except Exception as exc:  # noqa: BLE001
        return _svc(
            False,
            worker_ok=False,
            outbox_ok=False,
            node_ok=False,
            nodes=[],
            detail=str(exc),
            hint="Start Redis: docker compose -f deployments/docker-compose.yml up -d redis",
        )


def _list_registered_agents() -> list[dict[str, Any]]:
    sql = """
        SELECT a.agent_key, a.name, a.status,
               COALESCE((
                 SELECT e.url FROM agent_endpoints e
                 WHERE e.agent_id = a.id AND e.is_primary = true
                 ORDER BY e.updated_at DESC LIMIT 1
               ), '') AS endpoint
        FROM agents a
        WHERE a.tenant_id = %s::uuid
        ORDER BY a.agent_key
    """
    try:
        with connect(resolve_database_url()) as conn:
            rows = conn.execute(sql, (DEFAULT_TENANT_ID,)).fetchall()
        return [dict(r) for r in rows if is_work_agent(str(r.get("agent_key") or ""))]
    except Exception:  # noqa: BLE001
        return []


def probe_agent_health(endpoint: str, *, timeout_s: float = 0.4) -> dict[str, Any]:
    """Read-only /health — does not mutate registry status."""
    if not endpoint:
        return {
            "reachable": False,
            "runner_ready": False,
            "runner_reason": "missing endpoint",
        }
    url = endpoint.rstrip("/") + "/health"
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
        if status >= 400:
            return {
                "reachable": False,
                "runner_ready": False,
                "runner_reason": f"health HTTP {status}",
            }
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        if not isinstance(data, dict):
            return {"reachable": True, "runner_ready": True, "runner_reason": None}
        if "runner_ready" in data:
            ready = bool(data.get("runner_ready"))
            return {
                "reachable": True,
                "runner_ready": ready,
                "runner_reason": None if ready else (data.get("runner_reason") or "runner not ready"),
                "runner_mode": data.get("runner_mode"),
            }
        return {"reachable": True, "runner_ready": True, "runner_reason": None}
    except Exception as exc:  # noqa: BLE001
        return {
            "reachable": False,
            "runner_ready": False,
            "runner_reason": str(exc),
        }


def classify(
    services: dict[str, dict[str, Any]],
    agents: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking: list[str] = []
    warnings: list[str] = []
    hints: list[str] = []

    for name in ("postgres", "redis"):
        if not services.get(name, {}).get("ok"):
            blocking.append(name)
            detail = services[name].get("hint") or services[name].get("detail")
            if detail:
                hints.append(str(detail))

    worker_ok = bool(services.get("worker", {}).get("ok"))
    outbox_ok = bool(services.get("outbox", {}).get("ok"))
    if not worker_ok:
        blocking.append("worker")
        hints.append("Worker 未心跳：节点会停在 ready。另开进程 python worker.py，或用 " + HINT_DEV_UP)
    if not outbox_ok:
        blocking.append("outbox")
        hints.append(
            "Outbox 未心跳：首批节点会卡在 outbox_events(pending)。运行 python outbox_processor_service.py，或用 "
            + HINT_DEV_UP
        )

    pending = int(services.get("postgres", {}).get("pending_outbox") or 0)
    oldest = services.get("postgres", {}).get("oldest_pending_s")
    if outbox_ok and pending and oldest is not None and float(oldest) > 30:
        warnings.append("outbox_lag")
        hints.append(f"{pending} 条 outbox 事件积压超过 30s")

    stuck = int(services.get("postgres", {}).get("stuck_running") or 0)
    if stuck:
        warnings.append("stuck_running")
        hints.append(
            f"{stuck} 个任务仍在 running，会占满并发配额。打开 Tasks 取消，或在 Network 点 Retry。"
        )
    waiting = int(services.get("postgres", {}).get("waiting_hitl") or 0)
    waiting_agent = int(services.get("postgres", {}).get("waiting_agent") or 0)
    # HITL / peer-approval queues are operator work, not stack health failures.

    ready_agents = [a for a in agents if a.get("runner_ready")]
    reachable = [a for a in agents if a.get("reachable")]
    node_ok = bool(services.get("aop_node", {}).get("ok"))
    if not agents:
        warnings.append("no_agents")
        hints.append("没有已注册 Agent。启动 aopd（python scripts/dev_up.py）或 POST /v1/agents/register")
    elif not ready_agents:
        warnings.append("no_runner_ready")
        hints.append(
            "没有 runner_ready 的 Agent。用 aopd 拉起 harness 子进程："
            "POST http://127.0.0.1:7920/v1/agents/<agent_key>/start，并确认 CLI/密钥可用"
        )
    else:
        skipped = [a for a in agents if a.get("reachable") and not a.get("runner_ready")]
        if skipped:
            warnings.append("partial_runners")
            keys = ", ".join(str(a.get("agent_key")) for a in skipped[:5])
            hints.append(f"部分 Agent 不可执行: {keys}")
        offline = [a for a in agents if not a.get("reachable")]
        if offline:
            warnings.append("agents_offline")

    if blocking:
        status = "blocked"
    elif warnings:
        status = "degraded"
    else:
        status = "ready"

    return {
        "status": status,
        "can_run": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "hints": hints,
        "ready_agents": len(ready_agents),
        "reachable_agents": len(reachable),
        "registered_agents": len(agents),
        "hosted_ready": node_ok or bool(ready_agents),
        "waiting_hitl": waiting,
        "waiting_agent": waiting_agent,
        "stuck_running": stuck,
        "recover_steps": recover_steps(blocking=blocking, warnings=warnings),
    }


def recover_steps(*, blocking: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    """Chaos playbook: kill a process mid-run → what to do in Console."""
    block = set(blocking or [])
    warn = set(warnings or [])
    steps = [
        {
            "id": "orchestrator",
            "title": "Orchestrator 被杀掉",
            "symptom": "预检失败、Gateway 502、页面像掉线",
            "fix": "重启 apps/orchestrator：python -m uvicorn main:app --port 8090，或 " + HINT_DEV_UP,
            "console": "起来后打开原任务（URL 仍带 ?task=），点 Retry。计划在 Postgres 里，不会丢。",
        },
        {
            "id": "worker",
            "title": "Worker 被杀掉",
            "symptom": "节点停在 ready / running，预检 Worker down",
            "fix": "在 apps/orchestrator 运行 python worker.py，或 " + HINT_DEV_UP,
            "console": "预检 Worker 变绿后，在 Network 或 Tasks 对该任务点 Retry。",
        },
        {
            "id": "outbox",
            "title": "Outbox 被杀掉",
            "symptom": "刚创建的任务卡在 ready，outbox_events 一直 pending",
            "fix": "在 apps/orchestrator 运行 python outbox_processor_service.py，或 " + HINT_DEV_UP,
            "console": "Outbox 心跳恢复后会自动投递；仍卡住则点 Retry。",
        },
        {
            "id": "stuck_running",
            "title": "任务卡在 running",
            "symptom": "进程已死但任务仍占并发配额",
            "fix": "不必改数据库。",
            "console": "打开 Tasks 或 Network，点 Retry / Recover；或 Cancel 后再跑。",
        },
        {
            "id": "aopd",
            "title": "aopd / harness 没起来",
            "symptom": "预检 no_runner_ready，或 Agents 全是 stub/offline",
            "fix": "python scripts/dev_up.py（默认拉 aopd :7920）。仅排障用 --legacy-agents。",
            "console": "确认 aop-node :7920 绿，再 POST /v1/agents/<key>/start 或刷新 Agents。",
        },
        {
            "id": "control_plane",
            "title": "Gateway 连不上",
            "symptom": "顶栏提示连不上控制面 :8080",
            "fix": "重启 Gateway（go run ./cmd）或 " + HINT_DEV_UP,
            "console": "刷新 Console。鉴权关闭时无需重新登录。",
        },
    ]
    for s in steps:
        sid = s["id"]
        s["active"] = (
            sid in block
            or (sid == "stuck_running" and "stuck_running" in warn)
            or (sid == "outbox" and "outbox_lag" in warn)
            or (sid == "aopd" and "no_runner_ready" in warn)
        )
    return steps


def collect_preflight(*, probe_agents: bool = True) -> dict[str, Any]:
    pg = _postgres_snapshot()
    rd = _redis_snapshot()
    worker = _svc(
        bool(rd.get("ok") and rd.get("worker_ok")),
        worker_id=rd.get("worker_id"),
        ttl_s=TTL_S,
        hint=None if rd.get("worker_ok") else "no heartbeat",
    )
    outbox = _svc(
        bool(rd.get("ok") and rd.get("outbox_ok")),
        pending=pg.get("pending_outbox"),
        oldest_pending_s=pg.get("oldest_pending_s"),
        last_processed_at=pg.get("last_processed_at"),
        ttl_s=TTL_S,
        hint=None if rd.get("outbox_ok") else "no heartbeat",
    )
    nodes = rd.get("nodes") or []
    edge = _svc(
        bool(rd.get("ok") and rd.get("node_ok")),
        nodes=nodes,
        hint=None if rd.get("node_ok") else "no aop-node heartbeat (optional if using legacy uvicorn)",
    )
    services = {
        "orchestrator": _svc(True, service="orchestrator"),
        "postgres": pg,
        "redis": _svc(
            bool(rd.get("ok")),
            queued=rd.get("queued"),
            detail=rd.get("detail"),
            hint=rd.get("hint"),
        ),
        "worker": worker,
        "outbox": outbox,
        "aop_node": edge,
    }

    agents: list[dict[str, Any]] = []
    if probe_agents:
        rows = _list_registered_agents()

        def _probe_row(row: dict[str, Any]) -> dict[str, Any]:
            probe = probe_agent_health(row.get("endpoint") or "")
            return {
                "agent_key": row.get("agent_key"),
                "name": row.get("name"),
                "registry_status": row.get("status"),
                "endpoint": row.get("endpoint"),
                **probe,
            }

        if len(rows) <= 1:
            agents = [_probe_row(r) for r in rows]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(16, len(rows))) as pool:
                agents = list(pool.map(_probe_row, rows))

    verdict = classify(services, agents)
    return {
        **verdict,
        "services": services,
        "agents": agents,
        "dev_up": HINT_DEV_UP,
    }
