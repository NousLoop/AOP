"""Orchestrator HTTP API (Tasks + Artifacts + Workflows)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect

from service import TaskService
from governance import GovernanceService
from workflows import WorkflowService
from marketplace import MarketplaceService
from health_monitor import HealthMonitor
from observability import start_metrics_server, get_metrics_text
from observability.a2a_metrics import (
    observe_execution_event,
    observe_marketplace_event,
)
from websocket import manager as websocket_manager
from streams import StreamClient
from execution import (
    build_execution_service,
    CapacityQueue,
    RecoveryScheduler,
    ExecutionConfig,
)
from agent_lifecycle import AgentLifecycleService, PostgresAgentLifecycleService
from scheduling import SchedulingService

# Phase 36.8: Recovery and DR integration
from recovery_and_dr import StartupReconciler
from app_context import bind_services
from api import register_api_routes
from api.runtime import _json_safe

# OpenTelemetry tracing
try:
    from tracing import instrument_fastapi, ensure_tracing_initialized
    ensure_tracing_initialized()
    TRACING_AVAILABLE = True
except ImportError:
    TRACING_AVAILABLE = False

logger = logging.getLogger(__name__)

app = FastAPI(title="AOP Orchestrator", version="0.16.0")
tasks = TaskService()
# Phase 2.2: OS-level governance authority. Reuses the runtime graph so depth /
# visits are reconstructed from recorded edges (anti-tamper), never trusted from
# the caller. Agents call POST /v1/governance/check before delegating.
governance = GovernanceService(runtime_graph=tasks.runtime_graph)
workflows = WorkflowService()
# Phase 4: Postgres Execution Store by default (EXECUTION_STORE=memory for tests).
_exec_cfg = ExecutionConfig.from_env()
execution = build_execution_service(_exec_cfg)
tasks.execution = execution
if _exec_cfg.store == "postgres" and not os.getenv("PYTEST_CURRENT_TEST"):
    agent_lifecycle = PostgresAgentLifecycleService(event_bus=execution.events)
    agent_lifecycle.ping()
else:
    agent_lifecycle = AgentLifecycleService(event_bus=execution.events)
marketplace = MarketplaceService(event_bus=execution.events, lifecycle=agent_lifecycle)
streams = StreamClient()
execution.events.on_emit(
    lambda ev: observe_marketplace_event(ev.event_type, ev.payload or {})
)
capacity_queue = CapacityQueue(max_depth=_exec_cfg.max_queue_depth)
recovery_scheduler: RecoveryScheduler | None = None
# Phase 5: Intelligent Scheduling façade (Router stays discovery; Scheduler selects)
scheduling = SchedulingService()
execution.cost_service = scheduling.cost  # type: ignore[attr-defined]
execution.events.on_emit(
    lambda ev: observe_execution_event(ev.event_type, {**(ev.payload or {}), "duration_ms": (ev.payload or {}).get("duration_ms")})
)

# P4.15: Router filters by lifecycle + queue depth
try:
    tasks.router.set_lifecycle_hooks(
        lifecycle_checker=agent_lifecycle.router_accepts,
        capacity_queue_depth=capacity_queue.depth,
    )
except Exception as exc:  # noqa: BLE001
    logger.warning("router lifecycle hooks not attached: %s", exc)

# Phase 36.8: Initialize startup reconciler
startup_reconciler = StartupReconciler()

bind_services(
    tasks=tasks,
    execution=execution,
    marketplace=marketplace,
    scheduling=scheduling,
    governance=governance,
    workflows=workflows,
    agent_lifecycle=agent_lifecycle,
    capacity_queue=capacity_queue,
    exec_cfg=_exec_cfg,
)
register_api_routes(app)

# Instrument FastAPI for tracing
if TRACING_AVAILABLE:
    instrument_fastapi(app)


@app.on_event("startup")
async def startup_event():
    """Startup: Phase 36.8 reconciliation + Phase 4 RecoveryScheduler."""
    global recovery_scheduler
    logger.info("Running startup reconciliation...")

    try:
        issues = startup_reconciler.run_full_reconciliation()

        if issues:
            logger.warning(f"Found {len(issues)} consistency issues during startup:")
            for issue in issues:
                logger.warning(f"  - {issue.issue_type.value}: {issue.description} (severity: {issue.severity})")

            # Opt-in only: auto-failing "orphaned"/stale rows can kill live work.
            auto_fix = os.getenv("RECOVERY_AUTO_FIX", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if auto_fix:
                fixed = startup_reconciler.auto_fix_critical_issues()
                logger.info(f"Auto-fixed {sum(fixed.values())} critical issues: {fixed}")
            else:
                logger.warning(
                    "Skipping auto-fix (set RECOVERY_AUTO_FIX=1 to enable). "
                    "Lease-based RecoveryScheduler remains the live recovery path."
                )
        else:
            logger.info("No consistency issues found during startup")

    except Exception as e:
        logger.error(f"Startup reconciliation failed: {e}")
        # Don't fail startup if reconciliation fails

    if _exec_cfg.recovery_enabled:
        recovery_scheduler = RecoveryScheduler(
            execution,
            interval_s=_exec_cfg.recovery_interval_s,
            batch_size=_exec_cfg.recovery_batch_size,
            lease_seconds=_exec_cfg.lease_duration_s,
            owner_id=_exec_cfg.owner_id or "orchestrator-recovery",
            agent_lifecycle=agent_lifecycle,
        )
        recovery_scheduler.start()
        logger.info("Phase 4 RecoveryScheduler started")


@app.on_event("shutdown")
async def shutdown_event():
    """P4.20: stop recovery loop; leases expire for peer takeover."""
    global recovery_scheduler
    if recovery_scheduler is not None:
        recovery_scheduler.stop()
        recovery_scheduler = None
        logger.info("Phase 4 RecoveryScheduler stopped (leases retained for takeover)")

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "orchestrator", "phase": "20-tenant-memory"}


@app.get("/v1/metrics")
def metrics_json(hours: float = 24, format: str = "json") -> Any:
    if (format or "json").lower() in {"prom", "prometheus", "text"}:
        # Combine both metrics sources
        observability_metrics = tasks.metrics_prometheus(hours=hours)
        prometheus_metrics = get_metrics_text()
        combined_metrics = f"{observability_metrics}\n{prometheus_metrics}"
        return Response(
            content=combined_metrics,
            media_type="text/plain; version=0.0.4",
        )
    return tasks.metrics_snapshot(hours=hours)

@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Native Prometheus metrics endpoint for scraping."""
    prometheus_metrics = get_metrics_text()
    return Response(
        content=prometheus_metrics,
        media_type="text/plain; version=0.0.4",
    )


@app.post("/v1/health/probe")
def health_probe() -> dict[str, Any]:
    results = HealthMonitor().probe_all()
    return {
        "probed": len(results),
        "online": sum(1 for r in results if r.get("healthy")),
        "results": results,
    }


async def _pubsub_listen(websocket: WebSocket, pubsub, *, msg_type: str, task_id: str | None = None):
    """Forward Redis Pub/Sub messages to a WebSocket (non-competing fan-out)."""
    try:
        while True:
            msg = await asyncio.to_thread(
                pubsub.get_message, ignore_subscribe_messages=True, timeout=1.0
            )
            if not msg or msg.get("type") != "message":
                await asyncio.sleep(0)
                continue
            data = msg.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            fields: dict[str, Any]
            try:
                import json

                parsed = json.loads(data) if isinstance(data, str) else {}
                fields = parsed if isinstance(parsed, dict) else {"raw": data}
            except Exception:  # noqa: BLE001
                fields = {"raw": str(data)}
            payload = {
                "type": msg_type,
                "event_type": fields.get("event_type"),
                "data": fields,
                "timestamp": time.time(),
            }
            if task_id:
                payload["task_id"] = task_id
            await websocket.send_json(payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("pubsub listen error: %s", exc)


# WebSocket endpoints — Redis Pub/Sub fan-out (Phase 14)
@app.websocket("/v1/tasks/{task_id}/events/ws")
async def websocket_task_events(websocket: WebSocket, task_id: str):
    """Real-time task events via Redis Pub/Sub (each client gets a full copy)."""
    await websocket.accept()
    await websocket_manager.connect(websocket, task_id)
    pubsub = None
    event_listener = None

    try:
        row = tasks.get(task_id)
        if row:
            await websocket.send_json(
                {"type": "initial_state", "task_id": task_id, "data": _json_safe(row)}
            )

        events = tasks.events(task_id)
        if events:
            await websocket.send_json(
                {
                    "type": "initial_events",
                    "task_id": task_id,
                    "data": _json_safe(events),
                }
            )

        pubsub = streams.subscribe_task_events(task_id)
        event_listener = asyncio.create_task(
            _pubsub_listen(websocket, pubsub, msg_type="task_event", task_id=task_id)
        )

        while True:
            data = await websocket.receive_text()
            if data.strip().lower() in {"ping", '{"type":"ping"}'}:
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "echo", "message": f"Received: {data}"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WebSocket error for task %s: %s", task_id, e)
    finally:
        if event_listener:
            event_listener.cancel()
            try:
                await event_listener
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if pubsub is not None:
            try:
                pubsub.unsubscribe()
                pubsub.close()
            except Exception:  # noqa: BLE001
                pass
        await websocket_manager.disconnect(websocket, task_id)


@app.websocket("/v1/events/ws")
async def websocket_global_events(websocket: WebSocket):
    """Global system events via Redis Pub/Sub."""
    await websocket.accept()
    await websocket_manager.connect(websocket)
    pubsub = None
    event_listener = None

    try:
        overview = tasks.overview()
        await websocket.send_json({"type": "initial_state", "data": overview})

        pubsub = streams.subscribe_global_events()
        event_listener = asyncio.create_task(
            _pubsub_listen(websocket, pubsub, msg_type="system_event")
        )

        while True:
            data = await websocket.receive_text()
            if data.strip().lower() in {"ping", '{"type":"ping"}'}:
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "echo", "message": f"Received: {data}"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("Global WebSocket error: %s", e)
    finally:
        if event_listener:
            event_listener.cancel()
            try:
                await event_listener
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if pubsub is not None:
            try:
                pubsub.unsubscribe()
                pubsub.close()
            except Exception:  # noqa: BLE001
                pass
        await websocket_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    import threading
    import os

    # Get orchestrator port from environment or default to 8090
    orchestrator_port = int(os.getenv("ORCHESTRATOR_PORT", "8090"))
    orchestrator_id = os.getenv("ORCHESTRATOR_ID", "orchestrator-default")

    print(f"Starting Orchestrator {orchestrator_id} on port {orchestrator_port}")

    # Start metrics server in a separate thread (optional; /metrics also on main port)
    def start_metrics():
        try:
            # Prefer METRICS_PORT; never use port+1000 (8090→9090 collides with Prometheus)
            metrics_port = int(os.getenv("METRICS_PORT", "9091"))
            start_metrics_server(port=metrics_port)
            print(f"Metrics server started on port {metrics_port}")
        except Exception as e:
            print(f"Failed to start metrics server: {e}")

    metrics_thread = threading.Thread(target=start_metrics, daemon=True)
    metrics_thread.start()

    uvicorn.run("main:app", host="0.0.0.0", port=orchestrator_port, reload=False)
