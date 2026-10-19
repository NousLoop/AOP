from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .protocol import PROTOCOL_VERSION, TASK_STATES


class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input-required"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"

    @classmethod
    def parse(cls, value: Any) -> TaskStatus:
        if isinstance(value, cls):
            return value
        raw = value
        if isinstance(value, dict):
            raw = value.get("state") or value.get("status") or "submitted"
        text = str(raw or "submitted").strip().lower()
        # British spelling alias
        if text == "cancelled":
            text = "canceled"
        try:
            return cls(text)
        except ValueError:
            if text in TASK_STATES:
                return cls(text)
            return cls.WORKING


# AOP platform extensions nested under message/params metadata (not top-level RPC).


@dataclass
class Part:
    """A2A Part. Wire discriminator is ``kind`` (official); ``type`` accepted on read."""

    type: str  # text | data | file  (internal name; wire uses kind)
    text: str | None = None
    data: dict[str, Any] | None = None
    file: dict[str, Any] | None = None
    mime_type: str | None = None

    @property
    def kind(self) -> str:
        return self.type

    def to_dict(self) -> dict[str, Any]:
        # Official wire discriminator is ``kind`` only (``type`` accepted on read).
        payload: dict[str, Any] = {"kind": self.type}
        if self.text is not None:
            payload["text"] = self.text
        if self.data is not None:
            payload["data"] = self.data
        if self.file is not None:
            payload["file"] = self.file
        if self.mime_type is not None:
            payload["mimeType"] = self.mime_type
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Part:
        return cls(
            type=raw.get("kind") or raw.get("type") or "text",
            text=raw.get("text"),
            data=raw.get("data"),
            file=raw.get("file"),
            mime_type=raw.get("mimeType") or raw.get("mime_type"),
        )


@dataclass
class Message:
    role: str  # user | agent
    parts: list[Part]
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Platform governance — preferred home is metadata; fields kept for API ergonomics.
    idempotency_key: str | None = None
    correlation_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    depth: int = 0
    caller_agent_id: str | None = None
    target_agent_id: str | None = None
    visited_agents: list[str] = field(default_factory=list)
    governance_policy_id: str | None = None
    deadline: str | None = None
    callback_url: str | None = None

    def _governance_into_metadata(self) -> dict[str, Any]:
        meta = dict(self.metadata or {})
        if self.idempotency_key:
            meta.setdefault("idempotencyKey", self.idempotency_key)
        if self.correlation_id:
            meta.setdefault("correlationId", self.correlation_id)
        if self.parent_task_id:
            meta.setdefault("parentTaskId", self.parent_task_id)
        if self.root_task_id:
            meta.setdefault("rootTaskId", self.root_task_id)
        if self.depth > 0:
            meta.setdefault("depth", self.depth)
        if self.caller_agent_id:
            meta.setdefault("callerAgentId", self.caller_agent_id)
        if self.target_agent_id:
            meta.setdefault("targetAgentId", self.target_agent_id)
        if self.visited_agents:
            meta.setdefault("visitedAgents", list(self.visited_agents))
        if self.governance_policy_id:
            meta.setdefault("governancePolicyId", self.governance_policy_id)
        if self.deadline:
            meta.setdefault("deadline", self.deadline)
        if self.callback_url:
            meta.setdefault("callbackUrl", self.callback_url)
            # Official-shaped push config (Phase 3).
            meta.setdefault(
                "pushNotificationConfig",
                {"url": self.callback_url},
            )
        return meta

    def to_dict(self) -> dict[str, Any]:
        """Serialize for official-shaped wire.

        Governance/lineage live only under ``metadata``. Top-level mirrors are
        no longer written (inbound ``from_dict`` still accepts them).
        """
        meta = self._governance_into_metadata()
        payload: dict[str, Any] = {
            "role": self.role,
            "parts": [p.to_dict() for p in self.parts],
        }
        if self.message_id:
            payload["messageId"] = self.message_id
        if meta:
            payload["metadata"] = meta
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Message:
        parts = [Part.from_dict(p) for p in raw.get("parts", [])]
        meta = dict(raw.get("metadata") or {})

        def pick(*keys: str) -> Any:
            for k in keys:
                if k in meta and meta[k] is not None:
                    return meta[k]
                if k in raw and raw[k] is not None:
                    return raw[k]
            return None

        visited = pick("visitedAgents", "visited_agents")
        if isinstance(visited, str):
            visited = [v for v in visited.split(",") if v]

        push = meta.get("pushNotificationConfig") or raw.get("pushNotificationConfig")
        callback = pick("callbackUrl", "callback_url")
        if not callback and isinstance(push, dict):
            callback = push.get("url")

        depth_raw = pick("depth")
        try:
            depth = int(depth_raw or 0)
        except (TypeError, ValueError):
            depth = 0

        return cls(
            role=raw.get("role", "agent"),
            parts=parts,
            message_id=raw.get("messageId") or raw.get("message_id"),
            metadata=meta,
            idempotency_key=pick("idempotencyKey", "idempotency_key"),
            correlation_id=pick("correlationId", "correlation_id"),
            parent_task_id=pick("parentTaskId", "parent_task_id"),
            root_task_id=pick("rootTaskId", "root_task_id"),
            depth=depth,
            caller_agent_id=pick("callerAgentId", "caller_agent_id"),
            target_agent_id=pick("targetAgentId", "target_agent_id"),
            visited_agents=list(visited or []),
            governance_policy_id=pick("governancePolicyId", "governance_policy_id"),
            deadline=pick("deadline"),
            callback_url=callback,
        )


@dataclass
class Artifact:
    name: str | None
    parts: list[Part]
    artifact_id: str | None = None
    description: str | None = None

    def text(self) -> str:
        chunks = [p.text for p in self.parts if p.type == "text" and p.text]
        return "\n".join(chunks)

    def data(self) -> dict[str, Any] | None:
        for p in self.parts:
            if p.type == "data" and p.data is not None:
                return p.data
        return None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parts": [p.to_dict() for p in self.parts],
        }
        if self.name is not None:
            payload["name"] = self.name
        if self.artifact_id:
            payload["artifactId"] = self.artifact_id
        if self.description is not None:
            payload["description"] = self.description
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Artifact:
        return cls(
            name=raw.get("name"),
            parts=[Part.from_dict(p) for p in raw.get("parts", [])],
            artifact_id=raw.get("artifactId") or raw.get("artifact_id"),
            description=raw.get("description"),
        )


@dataclass
class Task:
    id: str
    status: TaskStatus
    artifacts: list[Artifact] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    correlation_id: str | None = None
    caller_agent_id: str | None = None
    target_agent_id: str | None = None
    depth: int = 0
    context_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextId": self.context_id or self.id,
            "status": {"state": self.status.value},
            "artifacts": [a.to_dict() for a in self.artifacts],
            "history": [m.to_dict() for m in self.history],
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        status_raw = raw.get("status", "submitted")
        status = TaskStatus.parse(status_raw)

        artifacts = [Artifact.from_dict(a) for a in raw.get("artifacts", [])]
        history = [Message.from_dict(m) for m in raw.get("history", [])]
        error = None
        if isinstance(status_raw, dict):
            msg = status_raw.get("message")
            if isinstance(msg, str) and msg.strip():
                error = msg.strip()
            elif isinstance(msg, dict):
                parts = msg.get("parts") or []
                texts = [
                    p.get("text")
                    for p in parts
                    if isinstance(p, dict) and p.get("text")
                ]
                error = "\n".join(texts) if texts else None

        meta = dict(raw.get("metadata") or {})
        if not error:
            meta_err = meta.get("error")
            if isinstance(meta_err, str) and meta_err.strip():
                error = meta_err.strip()

        def pick(*keys: str) -> Any:
            for k in keys:
                if k in meta and meta[k] is not None:
                    return meta[k]
                if k in raw and raw[k] is not None:
                    return raw[k]
            return None

        return cls(
            id=str(raw.get("id") or raw.get("taskId") or ""),
            status=status,
            artifacts=artifacts,
            history=history,
            metadata=meta,
            error=error,
            parent_task_id=pick("parentTaskId", "parent_task_id"),
            root_task_id=pick("rootTaskId", "root_task_id"),
            correlation_id=pick("correlationId", "correlation_id"),
            caller_agent_id=pick("callerAgentId", "caller_agent_id"),
            target_agent_id=pick("targetAgentId", "target_agent_id"),
            depth=int(pick("depth") or 0),
            context_id=raw.get("contextId") or raw.get("context_id"),
        )


__all__ = [
    "Artifact",
    "Message",
    "Part",
    "PROTOCOL_VERSION",
    "Task",
    "TaskStatus",
]
