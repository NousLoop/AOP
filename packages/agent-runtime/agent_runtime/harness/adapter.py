"""FastAPI factory: A2A shell that delegates execution to a HarnessRunner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from ..agent_collab import AgentCollaborator
from ..a2a_server import extract_lineage, find_cancel_targets, format_sse_frame
from ..collaboration import CallContext
from .cost import attach_usage, report_usage_to_os
from .approvals import (
    ApprovalInbox,
    approval_prompt,
    parse_approval_decision,
)
from .events import apply_event, finalize_task, new_working_task, utc_now
from .platform_role import enrich_system_prompt_for_agent
from .runners._cli_common import apply_provider_balance_failure
from .protocol import (
    HarnessEvent,
    HarnessEventType,
    HarnessResult,
    HarnessRunner,
    HarnessStatus,
)

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError:  # pragma: no cover
    FastAPI = None  # type: ignore[misc, assignment]
    Request = None  # type: ignore[misc, assignment]
    JSONResponse = None  # type: ignore[misc, assignment]
    StreamingResponse = None  # type: ignore[misc, assignment]


def _collab_authorized(request: Any) -> bool:
    """Gate mid-run collab HTTP. Prefer shared token; else loopback-only."""
    expected = (os.getenv("AOP_COLLAB_TOKEN") or "").strip()
    if expected:
        got = (request.headers.get("X-AOP-Collab-Token") or "").strip()
        return got == expected and bool(got)
    if (os.getenv("AOP_COLLAB_OPEN") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def load_agent_card(card_path: str | Path, *, agent_url: Optional[str] = None) -> dict[str, Any]:
    path = Path(card_path)
    card = json.loads(path.read_text(encoding="utf-8"))
    override = agent_url or os.getenv("AGENT_URL")
    if override:
        card["url"] = override if override.endswith("/") else f"{override}/"
    display = (os.getenv("AGENT_DISPLAY_NAME") or "").strip()
    if display:
        card["name"] = display
    # Official-shaped defaults (a2aproject/A2A).
    card.setdefault("protocolVersion", "0.3.0")
    card.setdefault("preferredTransport", "JSONRPC")
    caps = card.setdefault("capabilities", {})
    if isinstance(caps, dict):
        caps.setdefault("streaming", False)
        caps.setdefault("pushNotifications", False)
    return card


def _extract_text(message: dict[str, Any]) -> str:
    from .runners._cli_common import message_text

    text = message_text(message)
    if not text:
        raise ValueError("message.parts must include at least one text part")
    return text


def _skill_id(params: dict[str, Any], card: dict[str, Any]) -> str:
    """Resolve skill id for runners.

    Skill-less virtual agents (empty ``card.skills``) return ``\"\"`` unless the
    caller explicitly sets ``metadata.skillId`` — avoid inventing fake skills.
    """
    metadata = params.get("metadata") or {}
    sid = metadata.get("skillId") or metadata.get("skill_id")
    if sid:
        return str(sid)
    skills = card.get("skills") or []
    if skills:
        return str(skills[0].get("id") or "default")
    return ""


def _skill_allowed(skill_id: str, card: dict[str, Any]) -> bool:
    skills = card.get("skills") or []
    if not skills:
        return True  # skill-less virtual agents accept any / no skillId
    sid = (skill_id or "").strip()
    if not sid or sid in {"peer-approval", "peer_approval"}:
        return True
    allowed = {str(s.get("id")) for s in skills if s.get("id")}
    return sid in allowed


def _truthy_async_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "async"}:
        return True
    return False


def wants_async_send(params: dict[str, Any]) -> bool:
    """True when message/send should return immediately and run in background.

    Trigger: ``metadata.async`` / ``metadata.asyncMode`` **or** a ``callbackUrl``.
    """
    lin = extract_lineage(params)
    if lin.get("callback_url"):
        return True
    meta = params.get("metadata") or {}
    if isinstance(meta, dict):
        if _truthy_async_flag(meta.get("async")) or _truthy_async_flag(meta.get("asyncMode")):
            return True
    message = params.get("message") or {}
    msg_meta = message.get("metadata") if isinstance(message, dict) else None
    if isinstance(msg_meta, dict):
        if _truthy_async_flag(msg_meta.get("async")) or _truthy_async_flag(msg_meta.get("asyncMode")):
            return True
    return False


def create_harness_app(
    *,
    agent_id: str,
    runner: HarnessRunner,
    card: dict[str, Any] | None = None,
    card_path: str | Path | None = None,
    system_prompt: str | None = None,
    enable_heartbeat: bool = True,
    heartbeat_interval_s: float = 15.0,
    title: str | None = None,
    version: str = "0.1.0",
) -> Any:
    """Build a FastAPI app that speaks A2A and runs tasks via ``runner``."""
    if FastAPI is None or Request is None or JSONResponse is None or StreamingResponse is None:
        raise ImportError(
            "create_harness_app requires fastapi — pip install fastapi uvicorn"
        )

    if card is None:
        if not card_path:
            raise ValueError("card or card_path is required")
        card = load_agent_card(card_path)
    else:
        card = dict(card)
        card.setdefault("protocolVersion", "0.3.0")
        card.setdefault("preferredTransport", "JSONRPC")
        caps = card.setdefault("capabilities", {})
        if isinstance(caps, dict):
            # Harness implements message/stream — advertise honestly.
            caps.setdefault("streaming", True)
            caps.setdefault("pushNotifications", bool(caps.get("pushNotifications")))

    tasks: dict[str, dict[str, Any]] = {}
    idempotency_cache: dict[str, dict[str, Any]] = {}
    background_jobs: set[asyncio.Task[Any]] = set()
    collab = AgentCollaborator(agent_id=agent_id)
    sys_prompt = system_prompt or ""

    def _runner_readiness() -> dict[str, Any]:
        probe = getattr(runner, "readiness", None)
        if callable(probe):
            try:
                data = probe()
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                return {"ready": False, "reason": str(exc)}
        return {"ready": True, "mode": "unknown"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ready = _runner_readiness()
        if enable_heartbeat:
            collab.start_heartbeat(
                interval_s=heartbeat_interval_s,
                version=version,
                capabilities={
                    "harness": True,
                    "streaming": bool((card.get("capabilities") or {}).get("streaming")),
                    "asyncSpawn": True,
                    "peerApproval": True,
                    "runnerReady": bool(ready.get("ready")),
                    "runnerMode": ready.get("mode"),
                    "runnerReason": ready.get("reason"),
                },
            )
        yield
        collab.stop_heartbeat()
        for job in list(background_jobs):
            job.cancel()
        if background_jobs:
            await asyncio.gather(*background_jobs, return_exceptions=True)
        background_jobs.clear()

    app = FastAPI(
        title=title or f"AOP Harness ({agent_id})",
        version=version,
        lifespan=lifespan,
    )
    app.state.agent_id = agent_id
    app.state.card = card
    app.state.runner = runner
    app.state.tasks = tasks
    app.state.collab = collab
    app.state.system_prompt = sys_prompt
    app.state.runner_readiness = _runner_readiness
    inbox = ApprovalInbox(agent_id=agent_id)
    app.state.approvals = inbox

    async def _finalize_run(
        *,
        task_id: str,
        task: dict[str, Any],
        params: dict[str, Any],
        skill: str,
        run_message: dict[str, Any],
        idem: Any,
    ) -> dict[str, Any]:
        async def _on_event(event: HarnessEvent) -> None:
            apply_event(tasks[task_id], event)

        # Isolate CLI cwd per platform task × agent (runners read these keys).
        meta = run_message.setdefault("metadata", {})
        if isinstance(meta, dict):
            root = task.get("rootTaskId") or task.get("correlationId") or task_id
            meta.setdefault("rootTaskId", root)
            meta.setdefault("agentId", agent_id)

        try:
            result = await runner.run(
                task_id=task_id,
                message=run_message,
                skill_id=skill,
                on_event=_on_event,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] harness run failed", agent_id)
            result = HarnessResult(
                text="",
                status=HarnessStatus.FAILED,
                error=str(exc),
                skill_id=skill,
            )
            await _on_event(
                HarnessEvent(
                    type=HarnessEventType.ERROR,
                    task_id=task_id,
                    timestamp=utc_now(),
                    payload={"error": str(exc)},
                )
            )

        current = tasks.get(task_id) or task
        state = (
            (current.get("status") or {}).get("state")
            if isinstance(current.get("status"), dict)
            else None
        )
        if state in {"canceled", "cancelled"}:
            result = HarnessResult(
                text=result.text,
                data=result.data,
                usage=result.usage,
                status=HarnessStatus.CANCELED,
                error=result.error or "canceled",
                skill_id=skill,
            )
        elif result.status == HarnessStatus.CANCELED:
            if isinstance(current.get("status"), dict):
                current["status"]["state"] = "canceled"
            else:
                current["status"] = {"state": "canceled"}
        else:
            result = apply_provider_balance_failure(result)
            if result.status == HarnessStatus.FAILED and result.error:
                await _on_event(
                    HarnessEvent(
                        type=HarnessEventType.ERROR,
                        task_id=task_id,
                        timestamp=utc_now(),
                        payload={"error": result.error},
                    )
                )

        finalize_task(current, result)
        attach_usage(current, result.usage)
        report_usage_to_os(
            task_id=task_id,
            agent_id=agent_id,
            usage=result.usage,
            root_task_id=current.get("rootTaskId"),
            correlation_id=current.get("correlationId"),
            os_url=collab.os_url or None,
            api_key=collab.api_key,
        )
        collab.bump_active(-1)

        if idem:
            idempotency_cache[idem] = current
        collab.after_complete(params, current)
        return current

    @app.get("/health")
    async def health() -> dict[str, Any]:
        ready = _runner_readiness()
        return {
            "status": "ok" if ready.get("ready") else "degraded",
            "agent": agent_id,
            "version": version,
            "harness": True,
            "runner": type(runner).__name__,
            "asyncSpawn": True,
            "runner_ready": bool(ready.get("ready")),
            "runner_mode": ready.get("mode"),
            "runner_reason": ready.get("reason"),
            "peer_approval": True,
            "pending_approvals": len(inbox.list_pending()),
        }

    def _review_enabled() -> bool:
        raw = (os.getenv("HARNESS_APPROVAL_REVIEW") or "1").strip().lower()
        return raw not in {"0", "false", "off", "no"}

    async def _run_peer_review(approval_id: str) -> None:
        rec = inbox.get(approval_id)
        if not rec or rec.get("status") != "pending":
            return
        ready = _runner_readiness()
        if not ready.get("ready"):
            logger.info(
                "[%s] peer approval %s queued (runner not ready)",
                agent_id,
                approval_id,
            )
            return
        message = {
            "role": "user",
            "parts": [{"type": "text", "text": approval_prompt(rec)}],
            "metadata": {
                "kind": "peer_approval",
                "approvalId": approval_id,
                "taskId": rec.get("task_id"),
                "agentId": agent_id,
            },
        }

        async def _noop(_event: HarnessEvent) -> None:
            return

        try:
            result = await runner.run(
                task_id=f"approval-{approval_id}",
                message=message,
                skill_id="peer-approval",
                on_event=_noop,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] peer approval review failed: %s", agent_id, exc)
            return
        parsed = parse_approval_decision(result.text or "")
        if parsed is None and result.data:
            parsed = parse_approval_decision(json.dumps(result.data))
        if parsed is None:
            logger.info(
                "[%s] peer approval %s left pending (unparsed review)",
                agent_id,
                approval_id,
            )
            rec["review_text"] = (result.text or "")[:2000]
            return
        inbox.decide(
            approval_id,
            approved=bool(parsed["approved"]),
            reason=str(parsed.get("reason") or ""),
            os_url=collab.os_url or None,
            api_key=collab.api_key,
        )

    @app.post("/v1/approvals")
    async def receive_approval(request: Request) -> JSONResponse:
        if not _collab_authorized(request):
            return JSONResponse({"error": "collab unauthorized"}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "object required"}, status_code=400)
        rec = inbox.receive(body)
        if _review_enabled():
            if (os.getenv("HARNESS_APPROVAL_SYNC") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                await _run_peer_review(rec["id"])
            else:
                job = asyncio.create_task(_run_peer_review(rec["id"]))
                background_jobs.add(job)
                job.add_done_callback(background_jobs.discard)
        rec = inbox.get(rec["id"]) or rec
        return JSONResponse(rec, status_code=202)

    @app.get("/v1/approvals")
    async def list_approvals(request: Request) -> JSONResponse:
        if not _collab_authorized(request):
            return JSONResponse({"error": "collab unauthorized"}, status_code=403)
        pending = request.query_params.get("status") == "pending"
        items = inbox.list_pending() if pending else inbox.list_all()
        return JSONResponse({"items": items, "count": len(items)})

    @app.get("/v1/approvals/{approval_id}")
    async def get_approval(approval_id: str, request: Request) -> JSONResponse:
        if not _collab_authorized(request):
            return JSONResponse({"error": "collab unauthorized"}, status_code=403)
        rec = inbox.get(approval_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(rec)

    @app.post("/v1/approvals/{approval_id}/decide")
    async def decide_approval(approval_id: str, request: Request) -> JSONResponse:
        if not _collab_authorized(request):
            return JSONResponse({"error": "collab unauthorized"}, status_code=403)
        rec = inbox.get(approval_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        approved = body.get("approved")
        if approved is None:
            dec = str(body.get("decision") or "").strip().lower()
            approved = dec in {"approve", "approved", "yes", "true"}
        reason = str(body.get("reason") or body.get("input") or "")
        try:
            out = inbox.decide(
                approval_id,
                approved=bool(approved),
                reason=reason,
                os_url=collab.os_url or None,
                api_key=collab.api_key,
            )
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(out)

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    async def agent_card() -> JSONResponse:
        live = dict(card)
        override = os.getenv("AGENT_URL")
        if override:
            live["url"] = override if override.endswith("/") else f"{override}/"
        return JSONResponse(live, media_type="application/json")

    @app.post("/v1/collab/spawn")
    async def collab_spawn(request: Request) -> JSONResponse:
        """Fire-and-forget peer spawn for mid-run tool use (Claude Bash etc.)."""
        if not _collab_authorized(request):
            return JSONResponse({"error": "collab unauthorized"}, status_code=403)
        body = await request.json()
        goal = str(body.get("goal") or "").strip()
        if not goal:
            return JSONResponse({"error": "goal is required"}, status_code=400)
        parent_task_id = body.get("parent_task_id") or body.get("parentTaskId")
        root_task_id = body.get("root_task_id") or body.get("rootTaskId")
        correlation_id = body.get("correlation_id") or body.get("correlationId")
        if parent_task_id and parent_task_id in tasks:
            parent = tasks[parent_task_id]
            root_task_id = root_task_id or parent.get("rootTaskId")
            correlation_id = correlation_id or parent.get("correlationId")
        ctx = CallContext(
            correlation_id=str(correlation_id or uuid.uuid4()),
            root_task_id=str(root_task_id or parent_task_id or uuid.uuid4()),
            parent_task_id=str(parent_task_id) if parent_task_id else None,
            self_task_id=str(parent_task_id) if parent_task_id else None,
            caller_agent_id=agent_id,
            depth=int(body.get("depth") or 0),
        )
        collab_obj: AgentCollaborator = getattr(request.app.state, "collab", None) or collab
        spawned = collab_obj.spawn(
            goal,
            ctx,
            skill=body.get("skill"),
            agent_key=body.get("agent_key") or body.get("agentKey"),
            callback_url=body.get("callback_url") or body.get("callbackUrl"),
        )
        if spawned is None:
            return JSONResponse(
                {"error": "spawn failed or collaboration disabled"},
                status_code=503,
            )
        return JSONResponse(spawned)

    @app.post("/v1/collab/join")
    async def collab_join(request: Request) -> JSONResponse:
        if not _collab_authorized(request):
            return JSONResponse({"error": "collab unauthorized"}, status_code=403)
        body = await request.json()
        endpoint = str(body.get("endpoint") or "").strip()
        task_id = str(body.get("task_id") or body.get("taskId") or "").strip()
        if not endpoint or not task_id:
            return JSONResponse(
                {"error": "endpoint and task_id are required"},
                status_code=400,
            )
        timeout_s = float(body.get("timeout_s") or body.get("timeout") or 120.0)
        collab_obj: AgentCollaborator = getattr(request.app.state, "collab", None) or collab
        joined = collab_obj.join(endpoint, task_id, timeout_s=timeout_s)
        if joined is None:
            return JSONResponse(
                {"error": "join timed out or failed", "task_id": task_id},
                status_code=504,
            )
        if hasattr(joined, "status") and not isinstance(joined, dict):
            status = getattr(joined, "status", None)
            status_val = getattr(status, "value", status)
            artifacts = []
            for a in getattr(joined, "artifacts", None) or []:
                if isinstance(a, dict):
                    artifacts.append(a)
                else:
                    parts = []
                    for p in getattr(a, "parts", None) or []:
                        parts.append(p.to_dict() if hasattr(p, "to_dict") else p)
                    artifacts.append(
                        {
                            "name": getattr(a, "name", None),
                            "parts": parts,
                            "artifactId": getattr(a, "artifact_id", None),
                        }
                    )
            payload: dict[str, Any] = {
                "id": getattr(joined, "id", task_id),
                "status": {"state": str(status_val)},
                "artifacts": artifacts,
                "metadata": getattr(joined, "metadata", None) or {},
            }
            text = AgentCollaborator.extract_text(joined)
            if text:
                payload["text"] = text
            return JSONResponse(payload)
        return JSONResponse(joined if isinstance(joined, dict) else {"task": joined})

    def _prepare_message_task(
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str, dict[str, Any], Any] | JSONResponse:
        """Validate message/send|stream params and create the working task shell.

        Returns (task_id, task, skill, run_message, idem) or an error JSONResponse.
        """
        meta = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        idem = (
            params.get("idempotencyKey")
            or params.get("idempotency_key")
            or meta.get("idempotencyKey")
            or meta.get("idempotency_key")
        )
        if idem and idem in idempotency_cache:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "result": idempotency_cache[idem]}
            )

        message = params.get("message") or {}
        _extract_text(message)
        skill = _skill_id(params, card)
        if not _skill_allowed(skill, card):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32602, "message": f"Unsupported skill: {skill}"},
                }
            )

        client_id = params.get("id") or params.get("taskId") or params.get("task_id")
        task_id = str(client_id) if client_id else str(uuid.uuid4())
        if idem and task_id in tasks:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "result": tasks[task_id]})

        lin = extract_lineage(params)
        ctx = collab.context(params, task_id=task_id)
        task = new_working_task(
            task_id,
            skill_id=skill,
            correlation_id=lin.get("correlation_id") or (ctx.correlation_id if ctx else None),
            root_task_id=lin.get("root_task_id"),
            parent_task_id=lin.get("parent_task_id"),
        )
        task["metadata"]["agentId"] = agent_id

        run_message = dict(message)
        run_message.setdefault("metadata", {})
        effective_prompt = enrich_system_prompt_for_agent(
            sys_prompt,
            agent_id,
            message=run_message if isinstance(run_message, dict) else None,
            params=params,
        )
        if effective_prompt:
            task["metadata"]["systemPromptBytes"] = len(effective_prompt)
            if isinstance(run_message["metadata"], dict):
                run_message["metadata"]["systemPrompt"] = effective_prompt
        return task_id, task, skill, run_message, idem

    def _spawn_background(
        *,
        task_id: str,
        task: dict[str, Any],
        params: dict[str, Any],
        skill: str,
        run_message: dict[str, Any],
        idem: Any,
        initial_state: str = "submitted",
    ) -> None:
        task["status"] = {"state": initial_state, "timestamp": utc_now()}
        task["metadata"]["async"] = True
        tasks[task_id] = task
        collab.bump_active(1)
        if idem:
            idempotency_cache[idem] = task

        async def _bg() -> None:
            try:
                current = tasks.get(task_id)
                if current and isinstance(current.get("status"), dict):
                    if current["status"].get("state") == "submitted":
                        current["status"]["state"] = "working"
                        current["status"]["timestamp"] = utc_now()
                await _finalize_run(
                    task_id=task_id,
                    task=task,
                    params=params,
                    skill=skill,
                    run_message=run_message,
                    idem=idem,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] async harness job failed task=%s", agent_id, task_id)
            finally:
                job = asyncio.current_task()
                if job is not None:
                    background_jobs.discard(job)

        job = asyncio.create_task(_bg())
        background_jobs.add(job)

    @app.post("/")
    @app.post("/a2a")
    async def a2a_rpc(request: Request):
        body = await request.json()
        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}

        try:
            if method in {"tasks/get", "tasks/cancel", "tasks/subscribe", "tasks/delegate"}:
                if method == "tasks/cancel":
                    tid = params.get("id") or params.get("taskId") or params.get("task_id")
                    cancel_key = str(tid) if tid else ""
                    for target in find_cancel_targets(tasks, cancel_key) or (
                        [cancel_key] if cancel_key else []
                    ):
                        try:
                            await runner.cancel(str(target))
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("[%s] runner.cancel(%s) failed: %s", agent_id, target, exc)
                handled = collab.handle_control(method, params, tasks, req_id)
                if handled is not None:
                    return JSONResponse(handled)

            if method in {"message/send", "message/stream"}:
                prepared = _prepare_message_task(params)
                if isinstance(prepared, JSONResponse):
                    # Patch request id onto cached/error responses.
                    try:
                        payload = json.loads(prepared.body.decode("utf-8"))
                        payload["id"] = req_id
                        return JSONResponse(payload, status_code=prepared.status_code)
                    except Exception:  # noqa: BLE001
                        return prepared

                task_id, task, skill, run_message, idem = prepared

                if method == "message/stream":
                    if not bool((card.get("capabilities") or {}).get("streaming")):
                        return JSONResponse(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "error": {
                                    "code": -32601,
                                    "message": "Streaming not advertised on Agent Card",
                                },
                            }
                        )
                    _spawn_background(
                        task_id=task_id,
                        task=task,
                        params=params,
                        skill=skill,
                        run_message=run_message,
                        idem=idem,
                        initial_state="submitted",
                    )

                    terminal = frozenset(
                        {"completed", "failed", "canceled", "cancelled", "rejected"}
                    )
                    timeout_s = float(os.getenv("A2A_STREAM_TIMEOUT", "600"))

                    async def _event_stream():
                        last_state: str | None = None
                        first = {
                            "event": "status",
                            "taskId": task_id,
                            "status": "submitted",
                            "task": tasks.get(task_id) or task,
                            "jsonrpc": "2.0",
                            "id": req_id,
                        }
                        yield format_sse_frame("status", first)
                        last_state = "submitted"
                        deadline = asyncio.get_event_loop().time() + max(1.0, timeout_s)
                        while asyncio.get_event_loop().time() < deadline:
                            current = tasks.get(task_id)
                            if current is None:
                                miss = {"event": "not_found", "taskId": task_id}
                                yield format_sse_frame("not_found", miss)
                                return
                            status = current.get("status")
                            state = (
                                str(status.get("state") or "submitted")
                                if isinstance(status, dict)
                                else str(status or "submitted")
                            )
                            if state != last_state:
                                last_state = state
                                frame = {
                                    "event": "status",
                                    "taskId": task_id,
                                    "status": state,
                                    "task": current,
                                }
                                yield format_sse_frame("status", frame)
                            if state in terminal:
                                done = {
                                    "event": "final",
                                    "taskId": task_id,
                                    "status": state,
                                    "task": current,
                                }
                                yield format_sse_frame("final", done)
                                return
                            await asyncio.sleep(0.25)
                        timed = {
                            "event": "timeout",
                            "taskId": task_id,
                            "lastStatus": last_state,
                        }
                        yield format_sse_frame("timeout", timed)

                    return StreamingResponse(
                        _event_stream(),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                        },
                    )

                # message/send
                async_mode = wants_async_send(params)
                if async_mode:
                    _spawn_background(
                        task_id=task_id,
                        task=task,
                        params=params,
                        skill=skill,
                        run_message=run_message,
                        idem=idem,
                        initial_state="submitted",
                    )
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": task})

                tasks[task_id] = task
                collab.bump_active(1)
                current = await _finalize_run(
                    task_id=task_id,
                    task=task,
                    params=params,
                    skill=skill,
                    run_message=run_message,
                    idem=idem,
                )
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": current})

            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
        except ValueError as exc:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": str(exc)},
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] rpc error", agent_id)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                }
            )

    return app


__all__ = ["create_harness_app", "load_agent_card", "wants_async_send"]
