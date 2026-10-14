"""ExecutionEngine: consume Redis Stream → route → A2A → unlock DAG."""

from __future__ import annotations

import os
import socket
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from aggregator import Aggregator
from artifacts import ArtifactStore, LEGACY_TEXT_NAMES, is_primary_text_artifact
from evaluation import EvaluationService
from . import A2AExecutor, ExecutionResult
from handoff import (
    artifact_uris_from_refs,
    build_handoff,
    parse_handoff,
    plan_dependents,
    prune_upstream_slices,
    select_upstream_nodes,
)
from memory import MemoryService
from observability import record_agent_call, record_task_finished
from router import AgentRouter, RouterError, normalize_endpoint
from sandbox import SandboxViolation, check_endpoint, check_skill
from scheduler import Scheduler, is_cancel_error
from streams import StreamClient

# OpenTelemetry tracing
try:
    from tracing import get_tracer, trace_operation, set_span_attribute, record_span_exception
    TRACING_AVAILABLE = True
    tracer = get_tracer("execution")
except ImportError:
    TRACING_AVAILABLE = False
    def get_tracer(name): return None
    def trace_operation(name, **kwargs): 
        from contextlib import contextmanager
        @contextmanager
        def dummy_context():
            yield None
        return dummy_context()
    def set_span_attribute(key, value): pass
    def record_span_exception(exception): pass
# Error handling imports
try:
    from error_handling import (
        CircuitBreakerOpenError,
        handle_a2a_error,
        RetryPolicy,
        with_retry,
        AOPError,
        get_circuit_breaker,
    )
    ERROR_HANDLING_AVAILABLE = True
except ImportError:
    ERROR_HANDLING_AVAILABLE = False
    CircuitBreakerOpenError = Exception  # type: ignore
    handle_a2a_error = lambda e: e
    RetryPolicy = None
    with_retry = None
    AOPError = Exception
    def get_circuit_breaker(service: str):  # type: ignore
        return None

# Enhanced failure handling imports (P36.3)
try:
    from enhanced_failure_handling import (
        ErrorCategory as EnhancedErrorCategory,
        RecoveryStrategy,
        FailureContext,
        classify_error_module as classify_error,
        determine_recovery_strategy_module as determine_recovery_strategy,
    )
    ENHANCED_FAILURE_HANDLING_AVAILABLE = True
except ImportError:
    ENHANCED_FAILURE_HANDLING_AVAILABLE = False
    EnhancedErrorCategory = None
    RecoveryStrategy = None
    FailureContext = None
    classify_error = lambda e: "unknown"
    determine_recovery_strategy = lambda ec, ma: "retry"

# Request tracking for P36.1 idempotency (lazy import to avoid circular dependency)
REQUEST_TRACKING_AVAILABLE = False

# Enhanced retry policy for worker operations.
# Inner attempts stay at 1 — node-level scheduler already retries up to max_retry.
# Nested retries + circuit breaker burned the budget during long DeerFlow calls.
if ERROR_HANDLING_AVAILABLE:
    WORKER_RETRY_POLICY = RetryPolicy(
        max_attempts=1,
        base_delay=2.0,
        max_delay=30.0,
        exponential_base=2.0,
        jitter=True,
    )
else:
    WORKER_RETRY_POLICY = None

# attempt 1→2: 1s, 2→3: 3s; circuit-breaker open uses recovery window below
BACKOFF_SECONDS = {1: 1.0, 2: 3.0, 3: 10.0}
CIRCUIT_BREAKER_RETRY_DELAY = float(os.getenv("CIRCUIT_BREAKER_RETRY_DELAY", "65"))
# Long-running node stale reclaim (seconds). Must exceed CLI + join budgets.
# 0 disables. Default 2400s so research nodes aren't reclaimed mid-flight.
NODE_STALE_SECONDS = float(os.getenv("NODE_STALE_SECONDS", "2400"))


def _a2a_exec_async_enabled() -> bool:
    """Platform async: submit A2A then join in a side thread (frees Redis consumer)."""
    return os.getenv("A2A_EXEC_ASYNC", "1").lower() not in {"0", "false", "off", "no"}


def _a2a_async_join_timeout_s() -> float:
    # Must exceed CLI wall timeouts (default 1800s) so join doesn't race the runner.
    return float(os.getenv("A2A_ASYNC_JOIN_TIMEOUT", os.getenv("A2A_TIMEOUT", "1920")))


def _task_status_str(status: Any) -> str:
    """Normalize Task.status whether Enum or plain str (cached reconstructions)."""
    if status is None:
        return ""
    return str(getattr(status, "value", status) or "").lower()


def _a2a_async_poll_s() -> float:
    return float(os.getenv("A2A_ASYNC_POLL_INTERVAL", "2"))


def _a2a_async_workers() -> int:
    return max(1, int(os.getenv("A2A_ASYNC_WORKERS", "8")))


class ExecutionEngine:
    """Execution pipeline for a single worker process / consumer."""

    def __init__(self, consumer_name: str | None = None):
        self.worker_id = consumer_name or f"executor-{socket.gethostname()}-{os.getpid()}"
        self.streams = StreamClient()
        self.scheduler = Scheduler()
        self.router = AgentRouter()
        self.executor = A2AExecutor(timeout=float(os.getenv("A2A_TIMEOUT", "60")))
        self.aggregator = Aggregator()
        self.artifacts = ArtifactStore()
        self.evaluations = EvaluationService()
        self.memory = MemoryService()
        self._last_stale_check = 0.0
        self._request_tracking_service = None  # Lazy-loaded
        self._execution = None  # OS ExecutionService; lazy postgres
        self._async_pool: ThreadPoolExecutor | None = None
        if _a2a_exec_async_enabled():
            self._async_pool = ThreadPoolExecutor(
                max_workers=_a2a_async_workers(),
                thread_name_prefix="a2a-await",
            )
            print(
                f"[worker] A2A async join enabled "
                f"(workers={_a2a_async_workers()} timeout={_a2a_async_join_timeout_s()}s)"
            )

    def _get_request_tracking_service(self):
        """Lazy import of request tracking service."""
        if self._request_tracking_service is None:
            try:
                from request_tracking import get_request_tracking_service
                self._request_tracking_service = get_request_tracking_service()
            except ImportError:
                self._request_tracking_service = None
        return self._request_tracking_service

    def _os_execution(self):
        """Shared Postgres ExecutionRecord store (same SoT as Orchestrator HTTP)."""
        if self._execution is False:
            return None
        if self._execution is None:
            try:
                from execution import build_execution_service

                self._execution = build_execution_service()
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] execution store unavailable: {exc}")
                self._execution = False
                return None
        return self._execution

    def _sync_os(
        self,
        task_id: str,
        dag_status: str | None = None,
        *,
        error: str | None = None,
    ) -> None:
        try:
            from execution.dag_bridge import sync_from_dag

            svc = self._os_execution()
            if svc is None:
                return
            status = dag_status
            if status is None:
                row = self.scheduler.get_task(task_id)
                status = (row or {}).get("status")
            sync_from_dag(svc, task_id, status, error=error)
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] os execution sync skipped: {exc}")

    def run_forever(self) -> None:
        print(f"[worker] started consumer={self.worker_id}")
        while True:
            try:
                try:
                    from pulse import beat_worker

                    beat_worker(self.worker_id)
                except Exception:  # noqa: BLE001
                    pass
                self._maybe_reclaim_stale()
                messages = self.streams.read_execution(self.worker_id, count=1, block_ms=2000)
                if not messages:
                    continue
                for msg_id, fields in messages:
                    try:
                        self.handle(fields)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[worker] handle error: {exc}")
                        traceback.print_exc()
                    finally:
                        self.streams.ack_execution(msg_id)
            except KeyboardInterrupt:
                print("[worker] stopping")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] loop error: {exc}")
                time.sleep(1)

    def _execute_with_retry(
        self,
        endpoint: str,
        query: str,
        agent_key: str,
        idempotency_key: str | None = None,
        *,
        task_id: str | None = None,
        target_agent_id: str | None = None,
        async_mode: bool = False,
    ):
        """Execute A2A call with circuit breaker + retry.

        Circuit breaker only counts transport failures (timeout / network).
        Application-level ``task.status=failed`` (e.g. empty DeerFlow) must
        not trip the breaker — otherwise one long research + empty result
        burns the failure budget and blocks legitimate retries.
        """
        def execute_a2a():
            # Raise only on transport errors; return failed Task to caller.
            return self.executor.execute(
                endpoint,
                query,
                skill_id=agent_key,
                idempotency_key=idempotency_key,
                task_id=task_id,
                correlation_id=task_id,
                target_agent_id=target_agent_id,
                async_mode=async_mode,
            )

        def guarded():
            breaker = get_circuit_breaker(f"a2a:{endpoint}") if get_circuit_breaker else None
            if breaker is not None:
                result = breaker.call(execute_a2a)
            else:
                result = execute_a2a()
            # Async submit may return submitted/working — not a transport failure.
            if async_mode:
                state = _task_status_str(result.task.status)
                if state in {"submitted", "working"}:
                    return result
            if not result.ok:
                detail = ""
                try:
                    # Task.from_dict already promotes status.message text → task.error
                    detail = str(getattr(result.task, "error", None) or "").strip()
                    if not detail and isinstance(result.data, dict):
                        detail = str(
                            result.data.get("error")
                            or result.data.get("message")
                            or ""
                        ).strip()
                        harness_st = result.data.get("status")
                        if harness_st and harness_st != "ok":
                            detail = (
                                f"[{harness_st}] {detail}" if detail else str(harness_st)
                            )
                    if not detail and result.text:
                        detail = str(result.text).strip()[:500]
                except Exception:  # noqa: BLE001
                    detail = ""
                error_msg = (
                    f"A2A execution failed with status {_task_status_str(result.task.status)}"
                )
                if detail:
                    error_msg = f"{error_msg}: {detail[:500]}"
                try:
                    from agent_runtime.harness.runners._cli_common import provider_balance_error

                    bal = provider_balance_error(error_msg, result.text, detail)
                    if bal:
                        error_msg = bal
                except ImportError:
                    pass
                raise handle_a2a_error(RuntimeError(error_msg))
            return result
        if with_retry:
            @with_retry(policy=WORKER_RETRY_POLICY)
            def retry_execute():
                return guarded()

            return retry_execute()
        return guarded()

    def _maybe_reclaim_stale(self) -> None:
        if NODE_STALE_SECONDS <= 0:
            return
        now = time.time()
        if now - self._last_stale_check < 30:
            return
        self._last_stale_check = now
        try:
            reclaimed = self.scheduler.reclaim_stale_nodes(stale_seconds=NODE_STALE_SECONDS)
            for job in reclaimed:
                self.streams.enqueue_execution(**job)
                self._sync_os(str(job.get("task_id") or ""), "running")
                print(
                    f"[worker] reclaimed stale node={job['node_key']} "
                    f"task={job['task_id'][:8]} attempt={job.get('attempt')}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] stale reclaim error: {exc}")

    def _route_sandbox_and_claim(
        self,
        *,
        fields: dict[str, str],
        task_id: str,
        node_key: str,
        agent_key: str,
        attempt: int,
        exclude: set[str],
    ):
        """Select agent, sandbox-check endpoint, claim node.

        Returns ``(routed, endpoint, idempotency_key)`` or ``None`` when the
        worker should skip (already failed / not claimable).
        Raises ``RouterError`` after recording failure so the caller can trace.
        """
        try:
            routed = self.router.select(agent_key, exclude_agent_ids=exclude)
        except RouterError as exc:
            self._on_failure(
                fields,
                error=str(exc),
                attempt=attempt,
                agent_id=None,
                exclude=exclude,
                idempotency_key=None,
            )
            raise

        print(
            f"[worker] routed agent={routed.agent_key} score={routed.score} "
            f"breakdown={routed.score_breakdown}"
        )
        if TRACING_AVAILABLE:
            set_span_attribute("routing.agent_id", routed.agent_id)
            set_span_attribute("routing.score", routed.score)

        endpoint = normalize_endpoint(routed.endpoint)
        try:
            check_skill(agent_key)
            check_endpoint(endpoint)
        except SandboxViolation as exc:
            self._on_failure(
                fields,
                error=f"sandbox: {exc}",
                attempt=attempt,
                agent_id=routed.agent_id,
                exclude=exclude | {routed.agent_id},
                idempotency_key=None,
            )
            return None

        claimed = self.scheduler.claim_running(task_id, node_key, routed.agent_id, attempt)
        if not claimed:
            print(f"[worker] skip claim node={node_key} (not ready/retrying)")
            return None

        idempotency_key = self.scheduler.get_node_idempotency_key(task_id, node_key)
        if not idempotency_key:
            idempotency_key = f"req_{task_id[:8]}_{node_key}"
        idempotency_key = f"{idempotency_key}:a{attempt}"
        return routed, endpoint, idempotency_key

    def handle(self, fields: dict[str, str]) -> None:
        task_id = fields["task_id"]
        node_key = fields["node_key"]
        agent_key = fields["agent_key"]
        attempt = int(fields.get("attempt") or "1")
        exclude = {x for x in (fields.get("exclude_agent_ids") or "").split(",") if x}

        print(f"[worker] execute task={task_id} node={node_key} agent_key={agent_key} attempt={attempt}")

        # Start tracing span for this operation
        with trace_operation(
            "worker.handle",
            task_id=task_id,
            node_key=node_key,
            agent_key=agent_key,
            attempt=attempt
        ) as span:
            # Build query: goal + upstream outputs
            goal = self.scheduler.get_task_goal(task_id)
            query = self._compose_query(task_id, node_key, goal)
            
            # Add attributes to span
            if TRACING_AVAILABLE and span:
                set_span_attribute("worker.worker_id", self.worker_id)
                set_span_attribute("worker.goal_length", len(goal))

            try:
                prepared = self._route_sandbox_and_claim(
                    fields=fields,
                    task_id=task_id,
                    node_key=node_key,
                    agent_key=agent_key,
                    attempt=attempt,
                    exclude=exclude,
                )
            except RouterError as exc:
                if TRACING_AVAILABLE:
                    record_span_exception(exc)
                return
            if prepared is None:
                return
            routed, endpoint, idempotency_key = prepared

            self._sync_os(task_id, "running")

            # P36.1: Track the request before execution
            tracking_service = self._get_request_tracking_service()
            if tracking_service:
                try:
                    # Get node_id from scheduler
                    node_info = self.scheduler.get_node_info(task_id, node_key)
                    node_id = node_info.get("id") if node_info else str(uuid.uuid4())
                    
                    tracking_service.track_request(
                        idempotency_key,
                        task_id,
                        node_id,
                        routed.agent_id,
                        {"query": query, "agent_key": agent_key, "attempt": attempt},
                    )
                    tracking_service.mark_request_running(idempotency_key)
                except Exception as e:
                    print(f"[worker] request tracking error: {e}")

            self.streams.publish_execution_event(
                "agent.task.started",
                {
                    "task_id": task_id,
                    "node_key": node_key,
                    "agent_id": routed.agent_id,
                    "attempt": attempt,
                },
            )

            started = time.time()
            try:
                with trace_operation(
                    "worker.execute_a2a", agent_id=routed.agent_id, endpoint=endpoint
                ) as exec_span:
                    use_async = self._async_pool is not None
                    if ERROR_HANDLING_AVAILABLE and WORKER_RETRY_POLICY:
                        result = self._execute_with_retry(
                            endpoint,
                            query,
                            agent_key,
                            idempotency_key=idempotency_key,
                            task_id=task_id,
                            target_agent_id=routed.agent_id,
                            async_mode=use_async,
                        )
                    else:
                        result = self.executor.execute(
                            endpoint,
                            query,
                            skill_id=agent_key,
                            idempotency_key=idempotency_key,
                            task_id=task_id,
                            correlation_id=task_id,
                            target_agent_id=routed.agent_id,
                            async_mode=use_async,
                        )

                    state = _task_status_str(result.task.status)
                    if use_async and state in {"submitted", "working"}:
                        print(
                            f"[worker] async A2A submitted task={result.task.id} "
                            f"node={node_key} — join in background"
                        )
                        self.streams.publish_execution_event(
                            "agent.task.submitted",
                            {
                                "task_id": task_id,
                                "node_key": node_key,
                                "agent_id": routed.agent_id,
                                "a2a_task_id": result.task.id,
                            },
                        )
                        self._async_pool.submit(
                            self._await_a2a_and_finish,
                            fields=dict(fields),
                            endpoint=endpoint,
                            routed=routed,
                            agent_key=agent_key,
                            idempotency_key=idempotency_key,
                            started=started,
                            a2a_task_id=str(result.task.id),
                            attempt=attempt,
                            exclude=set(exclude),
                        )
                        return

                    if TRACING_AVAILABLE and exec_span:
                        set_span_attribute(
                            "execution.artifact_count",
                            len(result.task.artifacts or []),
                        )
                self._finalize_node_result(
                    task_id=task_id,
                    node_key=node_key,
                    agent_key=agent_key,
                    routed=routed,
                    endpoint=endpoint,
                    result=result,
                    started=started,
                    idempotency_key=idempotency_key,
                )

            except CircuitBreakerOpenError as exc:
                if TRACING_AVAILABLE:
                    record_span_exception(exc)
                # Wait for breaker recovery; do NOT exclude the only agent —
                # otherwise the next attempt fails with "no online agent".
                self._on_failure(
                    fields,
                    error=str(exc),
                    attempt=attempt,
                    agent_id=routed.agent_id,
                    exclude=exclude,
                    idempotency_key=idempotency_key,
                    force_delay=CIRCUIT_BREAKER_RETRY_DELAY,
                )
            except Exception as exc:  # noqa: BLE001
                if TRACING_AVAILABLE:
                    record_span_exception(exc)
                self._on_failure(
                    fields,
                    error=str(exc),
                    attempt=attempt,
                    agent_id=routed.agent_id,
                    exclude=exclude | {routed.agent_id},
                    idempotency_key=idempotency_key,
                )

    def _await_a2a_and_finish(
        self,
        *,
        fields: dict[str, str],
        endpoint: str,
        routed: Any,
        agent_key: str,
        idempotency_key: str | None,
        started: float,
        a2a_task_id: str,
        attempt: int,
        exclude: set[str],
    ) -> None:
        """Background join after async submit — keeps Redis consumer free."""
        task_id = fields["task_id"]
        node_key = fields["node_key"]
        try:
            result = self.executor.join_task(
                endpoint,
                a2a_task_id,
                timeout_s=_a2a_async_join_timeout_s(),
                poll_interval_s=_a2a_async_poll_s(),
            )
            # Preserve skill_id from the original routing intent.
            if result.skill_id is None:
                result = ExecutionResult(
                    agent_name=result.agent_name,
                    agent_url=result.agent_url,
                    skill_id=agent_key,
                    task=result.task,
                    text=result.text,
                    data=result.data,
                )
            self._finalize_node_result(
                task_id=task_id,
                node_key=node_key,
                agent_key=agent_key,
                routed=routed,
                endpoint=endpoint,
                result=result,
                started=started,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] async join failed node={node_key}: {exc}")
            traceback.print_exc()
            self._on_failure(
                fields,
                error=str(exc),
                attempt=attempt,
                agent_id=getattr(routed, "agent_id", None),
                exclude=exclude | ({routed.agent_id} if getattr(routed, "agent_id", None) else set()),
                idempotency_key=idempotency_key,
            )

    def _finalize_node_result(
        self,
        *,
        task_id: str,
        node_key: str,
        agent_key: str,
        routed: Any,
        endpoint: str,
        result: ExecutionResult,
        started: float,
        idempotency_key: str | None,
    ) -> None:
        """Persist artifacts, mark success, enqueue dependents (sync or after async join)."""
        latency_ms = int((time.time() - started) * 1000)
        tracking_service = self._get_request_tracking_service()

        refs = self.artifacts.persist_node_output(
            task_id=task_id,
            node_key=node_key,
            text=result.text,
            data=result.data,
        )
        gate = self._quality_gate(agent_key=agent_key, text=result.text, data=result.data)
        if not result.ok or not gate["ok"]:
            reason = gate.get("message")
            if not reason:
                detail = ""
                task_err = getattr(result.task, "error", None)
                if task_err:
                    detail = str(task_err).strip()
                if not detail and isinstance(result.data, dict):
                    detail = str(
                        result.data.get("error")
                        or result.data.get("message")
                        or ""
                    ).strip()
                    harness_st = result.data.get("status")
                    if harness_st and harness_st != "ok" and detail:
                        detail = f"[{harness_st}] {detail}"
                    elif harness_st and harness_st != "ok" and not detail:
                        detail = str(harness_st)
                if not detail and result.text:
                    detail = str(result.text).strip()[:500]
                reason = f"a2a status={_task_status_str(result.task.status)}"
                if detail:
                    reason = f"{reason}: {detail[:500]}"
            try:
                from agent_runtime.harness.runners._cli_common import provider_balance_error

                bal = provider_balance_error(reason, result.text)
                if bal:
                    reason = bal
            except ImportError:
                pass
            raise RuntimeError(reason)
        task_row = self.scheduler.get_task(task_id) or {}
        to_nodes = plan_dependents(task_row.get("plan_json"), node_key)
        conf = float(gate.get("confidence") or 0.5)
        handoff = build_handoff(
            from_node=node_key,
            to_nodes=to_nodes,
            artifact_ids=artifact_uris_from_refs(refs),
            confidence=conf,
            agent_key=agent_key,
            agent_id=routed.agent_id,
            reason=(
                gate.get("reason")
                or (
                    f"Completed {agent_key or node_key}"
                    + (f"; unlock {', '.join(to_nodes)}" if to_nodes else "")
                )
            ),
        )
        output = {
            "text": result.text,
            "data": result.data,
            "artifacts": [r.to_dict() for r in refs],
            "agent": {
                "id": routed.agent_id,
                "key": routed.agent_key,
                "endpoint": endpoint,
            },
            "a2a_task_id": result.task.id,
            "status": gate.get("status") or "ok",
            "confidence": conf,
            "handoff": handoff,
        }

        if tracking_service and idempotency_key:
            try:
                tracking_service.mark_request_completed(idempotency_key, output)
            except Exception as e:
                print(f"[worker] request completion tracking error: {e}")

        ready_jobs = self._with_task_lock(
            task_id,
            lambda: self.scheduler.mark_success(
                task_id,
                node_key,
                output=output,
                a2a_task_id=result.task.id,
                latency_ms=latency_ms,
            ),
        )
        self.streams.publish_execution_event(
            "agent.task.completed",
            {
                "task_id": task_id,
                "node_key": node_key,
                "agent_id": routed.agent_id,
                "latency_ms": latency_ms,
                "artifact_count": len(refs),
            },
        )
        try:
            record_agent_call(
                routed.agent_key,
                status="success",
                latency_seconds=latency_ms / 1000.0,
            )
        except Exception:  # noqa: BLE001
            pass
        print(
            f"[worker] stored {len(refs)} artifacts for {node_key}: "
            + ", ".join(r.uri for r in refs if r.name != "meta.json")
        )
        try:
            self.memory.remember_node(
                task_id,
                node_key=node_key,
                agent_key=agent_key,
                text=result.text or "",
                agent_id=routed.agent_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] memory write skipped: {exc}")
        for job in ready_jobs or []:
            self.streams.enqueue_execution(**job)
            print(f"[worker] enqueued downstream {job['node_key']}")

        task = self.scheduler.get_task(task_id)
        if task and task.get("status") in {"waiting_for_user", "waiting_for_agent"}:
            self.streams.publish_task_event(
                str(task.get("status")),
                {"task_id": task_id, "node_key": node_key},
            )
            print(f"[worker] task {task.get('status')} {task_id} at {node_key}")
            self._sync_os(task_id, str(task.get("status")))
        elif task and task.get("status") == "completed":
            result_json = self.aggregator.build_result(task_id)
            self.streams.publish_task_event(
                "task.completed",
                {
                    "task_id": task_id,
                    "summary_len": len(result_json.get("summary") or ""),
                },
            )
            print(f"[worker] task completed {task_id}")
            self._sync_os(task_id, "completed")
            try:
                record_task_finished("completed")
            except Exception:  # noqa: BLE001
                pass
            try:
                if os.getenv("TENANT_MEMORY_PROMOTE", "1").lower() not in {
                    "0",
                    "false",
                    "no",
                    "off",
                }:
                    promoted = self.memory.promote_task(task_id)
                    if promoted:
                        print(
                            f"[worker] promoted {len(promoted)} tenant memories "
                            f"from {task_id}"
                        )
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] tenant memory promote skipped: {exc}")
            self._auto_evaluate(task_id)

    def _on_failure(
        self,
        fields: dict[str, str],
        *,
        error: str,
        attempt: int,
        agent_id: str | None,
        exclude: set[str],
        idempotency_key: str | None = None,
        force_delay: float | None = None,
    ) -> None:
        task_id = fields["task_id"]
        node_key = fields["node_key"]
        agent_key = fields["agent_key"]
        node_id = fields.get("node_id") or ""

        print(f"[worker] failure node={node_key} attempt={attempt}: {error}")
        
        # P36.3: Enhanced error classification
        if ENHANCED_FAILURE_HANDLING_AVAILABLE:
            try:
                error_category = classify_error(error)
                recovery_strategy = determine_recovery_strategy(error_category)
                print(f"[worker] error classification: {error_category.value}, strategy: {recovery_strategy.value}")
            except Exception as e:
                print(f"[worker] enhanced failure handling error: {e}")
        
        # P36.1: Mark request as failed or unknown (P1-006)
        tracking_service = self._get_request_tracking_service()
        if tracking_service and idempotency_key:
            try:
                # Mark as unknown on timeout, otherwise failed
                if "timeout" in error.lower():
                    tracking_service.mark_request_unknown(idempotency_key, reason=error)
                else:
                    tracking_service.mark_request_failed(idempotency_key, error)
            except Exception as e:
                print(f"[worker] request failure tracking error: {e}")
        
        try:
            record_agent_call(
                agent_key or (agent_id or "unknown"),
                status="failed",
                error_type=error.split(":", 1)[0][:48] or "error",
            )
        except Exception:  # noqa: BLE001
            pass
        # Cancelled tasks must not enter the retry queue (late A2A canceled callback).
        if is_cancel_error(error):
            print(f"[worker] cancel end node={node_key} — skip retry: {error}")

        decision = self.scheduler.mark_failure(
            task_id,
            node_key,
            error=error,
            attempt=attempt,
            agent_id=agent_id,
        )
        if decision.get("decision") == "cancelled":
            self.streams.publish_execution_event(
                "agent.task.cancelled",
                {
                    "task_id": task_id,
                    "node_key": node_key,
                    "error": error,
                    "attempt": attempt,
                    "decision": "cancelled",
                },
            )
            print(f"[worker] node={node_key} cancelled — not retrying")
            self._sync_os(task_id, "cancelled", error=error)
            return

        self.streams.publish_execution_event(
            "agent.task.failed",
            {
                "task_id": task_id,
                "node_key": node_key,
                "error": error,
                "attempt": attempt,
                "decision": decision.get("decision"),
            },
        )
        if decision.get("decision") == "retry":
            delay = (
                float(force_delay)
                if force_delay is not None
                else BACKOFF_SECONDS.get(attempt, 10.0)
            )
            next_attempt = int(decision["next_attempt"])
            print(
                f"[worker] retry node={node_key} next_attempt={next_attempt} "
                f"delay={delay}s exclude={sorted(exclude)}"
            )
            self.streams.enqueue_execution(
                task_id=task_id,
                node_id=node_id or decision.get("node_id") or "",
                node_key=node_key,
                agent_key=agent_key,
                attempt=next_attempt,
                exclude_agent_ids=sorted(exclude),
                delay_seconds=delay,
            )
            self._sync_os(task_id, "retrying", error=error)
        elif decision.get("decision") == "failed":
            try:
                record_task_finished("failed")
            except Exception:  # noqa: BLE001
                pass
            self._sync_os(task_id, "failed", error=error)
            self._auto_evaluate(task_id)

    def _auto_evaluate(self, task_id: str) -> None:
        try:
            ev = self.evaluations.evaluate(task_id)
            self.streams.publish_task_event(
                "task.evaluated",
                {
                    "task_id": task_id,
                    "score": ev.get("score"),
                    "grade": ev.get("grade"),
                    "method": ev.get("method"),
                },
            )
            print(
                f"[worker] evaluated {task_id} grade={ev.get('grade')} score={ev.get('score')}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] evaluate skipped {task_id}: {exc}")

    def _node_instruction(self, task_id: str, node_key: str) -> str | None:
        """Per-node dispatch brief from planner (preferred over raw user goal)."""
        detail = self.scheduler.get_node(task_id, node_key)
        if not detail:
            return None
        inj = detail.get("input_json") or {}
        if isinstance(inj, str):
            import json

            try:
                inj = json.loads(inj)
            except Exception:
                return None
        if not isinstance(inj, dict):
            return None
        text = inj.get("instruction")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return None

    def _compose_query(self, task_id: str, node_key: str, goal: str) -> str:
        """Build the A2A text query with dependency-scoped upstream handoffs.

        Prefer structured ``handoff.artifact_ids`` over dumping every success
        node's full prose. Only inject nodes listed in ``depends_on``.

        When the planner attached a per-node ``instruction`` (multi-agent
        dispatch), send that brief instead of the raw user goal so agents do
        not see orchestration phrases like 「使用目前所有的 agent」.
        """
        instruction = self._node_instruction(task_id, node_key)
        primary = instruction or goal

        task = self.scheduler.get_task(task_id)
        if not task:
            return primary
        parts = [primary, f"Current node: {node_key}"]
        if instruction and goal and goal.strip() != instruction:
            # Keep original user wording only as light context (truncated).
            raw = goal.strip()
            if len(raw) > 240:
                raw = raw[:240] + "…"
            parts.insert(1, f"(Original user request for context: {raw})")
        try:
            mem = self.memory.compose_context(task_id)
            if mem:
                parts.append(mem)
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] memory read skipped: {exc}")

        upstream = select_upstream_nodes(
            nodes=list(task.get("nodes") or []),
            current_node_key=node_key,
            plan_json=task.get("plan_json"),
        )
        if not upstream:
            # Fan-out leaf: send the dispatch brief alone (+ optional memory).
            if instruction:
                extras = [p for p in parts[1:] if not p.startswith("Current node:")]
                extras = [p for p in extras if not p.startswith("(Original user request")]
                if extras:
                    return "\n\n".join([instruction, *extras])
                return instruction
            return primary if len(parts) <= 2 else "\n".join(parts)

        # Collect dep-scoped slices, then reducer-prune under a shared budget.
        raw_slices: list[dict[str, Any]] = []
        for n in upstream:
            nid = str(n.get("id") or "")
            detail = self.scheduler.get_node(task_id, nid)
            if not detail:
                continue
            out = detail.get("output_json") or {}
            if isinstance(out, str):
                import json

                try:
                    out = json.loads(out)
                except Exception:
                    out = {}
            if not isinstance(out, dict):
                continue

            handoff = parse_handoff(out.get("handoff"))
            text = out.get("text")
            primary_uri = ""

            artifact_candidates: list[str] = []
            if handoff and handoff.get("artifact_ids"):
                artifact_candidates.extend(str(u) for u in handoff["artifact_ids"])

            by_name: dict[str, str] = {}
            for art in out.get("artifacts") or []:
                if not isinstance(art, dict):
                    continue
                name = str(art.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                uri = art.get("uri")
                if uri and name in LEGACY_TEXT_NAMES:
                    by_name[name] = str(uri)
            # Prefer output.md over legacy output.txt, then other handoff URIs
            preferred = [by_name[n] for n in LEGACY_TEXT_NAMES if n in by_name]
            ordered: list[str] = []
            for uri in preferred + artifact_candidates:
                if uri not in ordered:
                    ordered.append(uri)
            artifact_candidates = ordered

            for uri in artifact_candidates:
                if not is_primary_text_artifact(uri=uri):
                    continue
                try:
                    text = self.artifacts.get_text(uri)
                    primary_uri = uri
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"[worker] warn load handoff artifact {uri}: {exc}")

            if not primary_uri:
                for art in out.get("artifacts") or []:
                    if art.get("name") == "output.json" and art.get("uri"):
                        primary_uri = str(art.get("uri"))
                        if primary_uri and primary_uri not in artifact_candidates:
                            artifact_candidates.append(primary_uri)
                        break

            hitl = out.get("hitl") if isinstance(out.get("hitl"), dict) else None
            human = (hitl or {}).get("input") if hitl else None
            human_text = human.strip() if isinstance(human, str) else ""

            if not text and not human_text:
                continue
            raw_slices.append(
                {
                    "node_key": nid,
                    "agent_key": n.get("agent_key"),
                    "reason": (handoff or {}).get("reason") or "",
                    "confidence": (handoff or {}).get("confidence"),
                    "artifact_ids": artifact_candidates,
                    "primary_uri": primary_uri,
                    "body": str(text) if text else "",
                    "human": human_text,
                }
            )

        pruned = prune_upstream_slices(raw_slices)
        if not pruned:
            return primary if len(parts) <= 2 else "\n".join(parts)

        parts.append("Upstream results (via handoff):")
        for sl in pruned:
            nid = sl["node_key"]
            conf = sl.get("confidence")
            conf_line = (
                f" (confidence={float(conf):.2f})"
                if isinstance(conf, (int, float))
                else ""
            )
            reason = sl.get("reason") or ""
            reason_line = f"\nhandoff: {reason}" if reason else ""
            uri_hint = f"\n(artifact: {sl['primary_uri']})" if sl.get("primary_uri") else ""
            body = sl.get("body") or ""
            if body:
                parts.append(
                    f"\n### {nid} ({sl.get('agent_key')}){conf_line}{reason_line}{uri_hint}\n{body}"
                )
            human_text = sl.get("human") or ""
            if human_text:
                parts.append(f"\n### Human guidance (from {nid})\n{human_text}")

        parts.append("\nPlease continue based on the upstream results.")
        return "\n".join(parts)

    def _quality_gate(
        self,
        *,
        agent_key: str | None,
        text: str | None,
        data: dict | None,
    ) -> dict:
        """Fail-fast before unlocking dependents when upstream research is unusable."""
        payload = data if isinstance(data, dict) else {}
        try:
            from agent_runtime.harness.runners._cli_common import harness_output_error
        except ImportError:
            harness_output_error = lambda *a: None  # noqa: E731
        extra = " ".join(
            str(payload.get(k) or "") for k in ("error", "stderr", "message", "status", "ok")
        )
        if payload.get("ok") is False:
            extra = f"{extra} ok=false"
        if payload.get("stub") is True or (
            payload.get("hosted") is True and not payload.get("model_invoked")
        ):
            extra = f"{extra} hosted stub"
        obvious = harness_output_error(text, extra)
        if obvious:
            return {
                "ok": False,
                "status": "error",
                "confidence": 0.0,
                "message": obvious,
                "reason": obvious,
            }
        status = str(payload.get("status") or "").lower()
        conf_raw = payload.get("confidence")
        try:
            conf = float(conf_raw) if conf_raw is not None else None
        except (TypeError, ValueError):
            conf = None

        # Explicit agent failure statuses
        if status in {"no_relevant_results", "insufficient_research", "error", "failed"}:
            msg = (
                str(payload.get("error") or text or status)
                [:400]
            )
            return {
                "ok": False,
                "status": status,
                "confidence": conf if conf is not None else 0.0,
                "message": msg,
                "reason": msg,
            }

        # Search-shaped payloads (results[]) — hop identity is agent_key, not a specialty.
        if isinstance(payload.get("results"), list):
            results = payload.get("results")
            source = str(payload.get("source") or "").lower()
            if isinstance(results, list) and not results and source not in {"mock"}:
                return {
                    "ok": False,
                    "status": "no_relevant_results",
                    "confidence": 0.0,
                    "message": "Search returned zero results; refusing to unlock downstream nodes.",
                    "reason": "search empty results",
                }
            if conf is not None and conf < 0.25:
                return {
                    "ok": False,
                    "status": "low_confidence",
                    "confidence": conf,
                    "message": f"Search confidence too low ({conf:.2f}); refusing downstream unlock.",
                    "reason": "search low confidence",
                }
            # Heuristic: classic junk SERP dump
            blob = f"{text or ''} {payload.get('summary') or ''}".lower()
            junk_hits = sum(
                1
                for h in (
                    "sogou.com/",
                    "cn.bing.com/",
                    "so.com/",
                    "17so.cn/",
                    "hgcha.com",
                    "zdic.net",
                    "拼音",
                    "组词",
                    "笔画",
                    "康熙字典",
                )
                if h in blob
            )
            topic_hint = str(payload.get("query") or "")
            subject_missing = False
            if topic_hint and len(topic_hint) >= 2:
                # Subject must appear in the summary when we have a focused query
                if topic_hint.lower() not in blob and not any(
                    c.isascii() and c.isalpha() for c in topic_hint
                ):
                    # CJK topic absent from search output
                    subject_missing = topic_hint not in (text or "") and topic_hint not in str(
                        payload.get("summary") or ""
                    )
            if junk_hits >= 2 or (junk_hits >= 1 and subject_missing):
                return {
                    "ok": False,
                    "status": "no_relevant_results",
                    "confidence": 0.0,
                    "message": "Search output looks like dictionary/portal junk, not the research subject.",
                    "reason": "serp junk detected",
                }
            if subject_missing and conf is not None and conf < 0.7:
                return {
                    "ok": False,
                    "status": "no_relevant_results",
                    "confidence": conf,
                    "message": f"Search results do not mention subject {topic_hint!r}.",
                    "reason": "subject missing from results",
                }

        return {
            "ok": True,
            "status": status or "ok",
            "confidence": conf if conf is not None else (0.85 if (text or payload) else 0.4),
            "message": "",
            "reason": "",
        }

    def _with_task_lock(self, task_id: str, fn):
        # Best-effort lock; proceed even if lock busy after short wait.
        for _ in range(20):
            if self.streams.acquire_task_lock(task_id, self.worker_id, ttl=30):
                try:
                    return fn()
                finally:
                    self.streams.release_task_lock(task_id, self.worker_id)
            time.sleep(0.1)
        return fn()
