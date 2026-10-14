"""Executor: invoke a remote A2A agent and normalize artifacts (sandbox-aware)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from a2a_sdk import A2AClient, AgentCard, Message, Part, Task
from a2a_sdk.client import first_data_artifact, first_text_artifact
from a2a_sdk.protocol import TERMINAL_TASK_STATES

from sandbox import SandboxPolicy, SandboxViolation, clamp_output, guard_call

logger = logging.getLogger(__name__)

try:
    from error_handling import (
        handle_a2a_error,
        NetworkError,
        TimeoutError as AOPTimeoutError,
        AgentError,
    )

    ERROR_HANDLING_AVAILABLE = True
except ImportError:
    ERROR_HANDLING_AVAILABLE = False

    def handle_a2a_error(error):
        return error

    NetworkError = None
    AOPTimeoutError = None
    AgentError = None

# Request tracking for P36.1 idempotency (lazy import to avoid circular dependency)
REQUEST_TRACKING_AVAILABLE = False


@dataclass
class ExecutionResult:
    agent_name: str
    agent_url: str
    skill_id: str | None
    task: Task
    text: str | None
    data: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        status = getattr(self.task.status, "value", self.task.status)
        if str(status or "").lower() != "completed":
            return False
        try:
            from agent_runtime.harness.runners._cli_common import harness_output_error
        except ImportError:
            harness_output_error = lambda *a: None  # noqa: E731
        extra = None
        if isinstance(self.data, dict):
            extra = " ".join(
                str(self.data.get(k) or "")
                for k in ("error", "stderr", "message", "status", "ok")
            )
            if self.data.get("ok") is False:
                extra = f"{extra} ok=false"
            if self.data.get("stub") is True or (
                self.data.get("hosted") is True and not self.data.get("model_invoked")
            ):
                extra = f"{extra} hosted stub"
        task_err = getattr(self.task, "error", None)
        return harness_output_error(self.text, extra, task_err) is None


class A2AExecutor:
    """Sync executor with Phase 22 sandbox guardrails."""

    def __init__(self, timeout: float = 60.0, policy: SandboxPolicy | None = None):
        self.timeout = timeout
        self.policy = policy

    def _get_request_tracking_service(self):
        """Lazy import of request tracking service."""
        try:
            from request_tracking import get_request_tracking_service
            return get_request_tracking_service()
        except ImportError:
            return None

    def discover(self, agent_url: str) -> AgentCard:
        client = A2AClient(agent_url, timeout=self.timeout)
        return client.card

    def execute(
        self,
        agent_url: str,
        query: str,
        *,
        skill_id: str | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        caller_agent_id: str | None = None,
        target_agent_id: str | None = None,
        depth: int = 0,
        visited_agents: list[str] | None = None,
        async_mode: bool = False,
        callback_url: str | None = None,
    ) -> ExecutionResult:
        # P36.1: Use provided idempotency key or generate one
        if idempotency_key is None:
            idempotency_key = f"req_{uuid.uuid4().hex}"

        # P36.1: Check if request already completed (deduplication)
        service = self._get_request_tracking_service()
        if service and service.is_request_completed(idempotency_key):
            cached_response = service.get_cached_response(idempotency_key)
            if cached_response:
                logger.info(f"Returning cached response for idempotency_key={idempotency_key}")
                # Reconstruct ExecutionResult from cached response
                return self._result_from_cache(cached_response, agent_url, skill_id)

        try:
            q, timeout = guard_call(
                endpoint=agent_url,
                skill=skill_id,
                query=query,
                policy=self.policy,
            )
            # Submit path may return immediately; use a short connect timeout for async.
            client_timeout = min(timeout, 30.0) if async_mode else timeout
            client = A2AClient(agent_url, timeout=client_timeout)
            card = client.card
            # Seed A2A lineage from the central OS task so Agent networks inherit
            # the orchestrator task as root_task_id (Phase 2 Task↔Runtime Graph).
            # Stable a2a task id lets OS cancel in-flight work before completion.
            root = task_id
            corr = correlation_id or task_id
            a2a_task_id = str(uuid.uuid4())
            # Prefer message/stream only for real AgentCard with streaming=true.
            if isinstance(card, AgentCard):
                use_stream = (not async_mode) and card.supports_streaming()
            else:
                caps = getattr(card, "capabilities", None)
                use_stream = (not async_mode) and isinstance(caps, dict) and bool(
                    caps.get("streaming")
                )
            if use_stream:
                message = Message(
                    role="user",
                    parts=[Part(type="text", text=q)],
                    message_id=str(uuid.uuid4()),
                    idempotency_key=idempotency_key,
                    correlation_id=corr,
                    root_task_id=root,
                    depth=depth,
                    caller_agent_id=caller_agent_id or "orchestrator",
                    target_agent_id=target_agent_id,
                    visited_agents=list(visited_agents or (["orchestrator"] if root else [])),
                    callback_url=callback_url,
                )
                task = None
                for frame in client.stream_message(
                    message,
                    skill_id=skill_id,
                    task_id=a2a_task_id,
                    correlation_id=corr,
                    root_task_id=root,
                    depth=depth,
                ):
                    raw_task = frame.get("task") if isinstance(frame, dict) else None
                    if isinstance(raw_task, dict):
                        task = Task.from_dict(raw_task)
                    if isinstance(frame, dict) and frame.get("event") in {"final", "timeout"}:
                        break
                if task is None:
                    task = client.poll_task(a2a_task_id, timeout_s=min(timeout, 120.0))
            else:
                task = client.send_text(
                    q,
                    skill_id=skill_id,
                    idempotency_key=idempotency_key,
                    correlation_id=corr,
                    parent_task_id=None,
                    root_task_id=root,
                    depth=depth,
                    caller_agent_id=caller_agent_id or "orchestrator",
                    target_agent_id=target_agent_id,
                    visited_agents=list(visited_agents or (["orchestrator"] if root else [])),
                    task_id=a2a_task_id,
                    async_mode=async_mode,
                    callback_url=callback_url,
                )
                # Sync path that only ACK'd: poll until terminal (async callers use join_task).
                if not async_mode:
                    state = getattr(task.status, "value", str(task.status)).lower()
                    if state == "submitted":
                        task = client.poll_task(
                            task.id or a2a_task_id,
                            timeout_s=max(timeout, 60.0),
                            terminal=TERMINAL_TASK_STATES
                            | {"input-required", "auth-required"},
                        )
            text = clamp_output(first_text_artifact(task), self.policy)
            result = ExecutionResult(
                agent_name=card.name,
                agent_url=agent_url.rstrip("/"),
                skill_id=skill_id,
                task=task,
                text=text,
                data=first_data_artifact(task),
            )
            return result
        except SandboxViolation:
            raise
        except Exception as e:
            if ERROR_HANDLING_AVAILABLE:
                aop_error = handle_a2a_error(e)
                logger.error(f"A2A execution error: {aop_error.to_dict()}")
                raise aop_error from e
            logger.error(f"A2A execution error: {e}")
            raise

    def join_task(
        self,
        agent_url: str,
        a2a_task_id: str,
        *,
        timeout_s: float = 900.0,
        poll_interval_s: float = 2.0,
    ) -> ExecutionResult:
        """Poll ``tasks/get`` until the A2A task reaches a terminal state."""
        client = A2AClient(agent_url, timeout=min(60.0, max(5.0, poll_interval_s * 4)))
        card = client.card
        last = client.poll_task(
            a2a_task_id,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            terminal=TERMINAL_TASK_STATES | {"input-required", "auth-required"},
        )
        text = clamp_output(first_text_artifact(last), self.policy)
        return ExecutionResult(
            agent_name=card.name,
            agent_url=agent_url.rstrip("/"),
            skill_id=None,
            task=last,
            text=text,
            data=first_data_artifact(last),
        )
    def _result_from_cache(
        self,
        cached_response: dict[str, Any],
        agent_url: str,
        skill_id: str | None,
    ) -> ExecutionResult:
        """Reconstruct ExecutionResult from cached response."""
        from a2a_sdk.models import TaskStatus

        task_dict = cached_response.get("task", {})
        if task_dict:
            task = Task.from_dict(task_dict)
        else:
            raw_status = cached_response.get("status", "completed")
            if isinstance(raw_status, dict):
                raw_status = raw_status.get("state", "completed")
            try:
                status = TaskStatus(str(raw_status))
            except ValueError:
                status = TaskStatus.COMPLETED
            task = Task(
                id=str(cached_response.get("task_id") or "cached"),
                status=status,
                artifacts=list(cached_response.get("artifacts") or []),
            )
        return ExecutionResult(
            agent_name=cached_response.get("agent_name", "cached"),
            agent_url=agent_url.rstrip("/"),
            skill_id=skill_id,
            task=task,
            text=cached_response.get("text"),
            data=cached_response.get("data"),
        )
