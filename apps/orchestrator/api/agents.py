"""Agent lifecycle / runtime health routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app_context import ctx
from execution.state_machine import IllegalTransition
from pulse import beat_node, list_nodes

router = APIRouter(tags=["agents"])


class AgentHeartbeatRequest(BaseModel):
    active_tasks: int | None = None
    load: float | None = None
    version: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class NodeHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    client_version: str | None = None
    node_id: str | None = None
    children: list[Any] = Field(default_factory=list)


@router.get("/v1/agents/{agent_id}/health")
@router.get("/v1/agent-runtime/{agent_id}/health")
def agent_health(agent_id: str) -> dict[str, Any]:
    """Lifecycle health. Prefer ``/v1/agent-runtime/...``; ``/v1/agents/...`` kept for Gateway GET proxy."""
    return ctx.agent_lifecycle.health(agent_id)


@router.get("/v1/nodes")
def list_edge_nodes() -> dict[str, Any]:
    """Edge supervisors (aop-node). Not A2A agents."""
    nodes = list_nodes()
    return {"nodes": nodes, "count": len(nodes)}


@router.post("/v1/nodes/{node_id}/heartbeat")
def node_heartbeat(node_id: str, body: NodeHeartbeatRequest | None = None) -> dict[str, Any]:
    body = body or NodeHeartbeatRequest()
    payload = body.model_dump(exclude_none=True)
    beat_node(node_id, json.dumps(payload, default=str))
    return {"ok": True, "node_id": node_id, "kind": "node"}


@router.post("/v1/agents/{agent_id}/heartbeat")
@router.post("/v1/agent-runtime/{agent_id}/heartbeat")
def agent_heartbeat(
    agent_id: str, body: AgentHeartbeatRequest | None = None
) -> dict[str, Any]:
    body = body or AgentHeartbeatRequest()
    rec = ctx.agent_lifecycle.heartbeat(
        agent_id,
        active_tasks=body.active_tasks,
        load=body.load,
        version=body.version,
        capabilities=body.capabilities or None,
    )
    return rec.to_dict()


@router.post("/v1/agent-runtime/{agent_id}/drain")
def agent_drain(agent_id: str) -> dict[str, Any]:
    """Stop accepting new work; finish in-flight then OFFLINE."""
    try:
        if ctx.agent_lifecycle.get(agent_id) is None:
            ctx.agent_lifecycle.register(agent_id)
            ctx.agent_lifecycle.mark_ready(agent_id)
        rec = ctx.agent_lifecycle.drain(agent_id)
    except (KeyError, IllegalTransition) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "lifecycle_error", "message": str(exc)},
        ) from exc
    return rec.to_dict()
