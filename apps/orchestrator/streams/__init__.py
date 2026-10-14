"""Redis Streams + Pub/Sub helpers for AOP execution and realtime fan-out."""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import redis

logger = logging.getLogger(__name__)

EXECUTION_STREAM = "a2a.execution.queue"
EXECUTION_EVENTS = "a2a.execution.events"
TASK_EVENTS = "a2a.task.events"
EXECUTOR_GROUP = "cg-executor"

# Pub/Sub channels — every WebSocket subscriber receives a copy (no competing group)
CHANNEL_GLOBAL = "aop:events"
CHANNEL_TASK_PREFIX = "aop:task:"

def task_events_channel(task_id: str) -> str:
    return f"{CHANNEL_TASK_PREFIX}{task_id}:events"

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

class StreamClient:
    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
        self.r.ping()
        self.ensure_groups()

    def ensure_groups(self) -> None:
        for stream, group in (
            (EXECUTION_STREAM, EXECUTOR_GROUP),
            (EXECUTION_EVENTS, "cg-aggregator"),
            (TASK_EVENTS, "cg-trace"),
        ):
            try:
                self.r.xgroup_create(stream, group, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def enqueue_execution(
        self,
        *,
        task_id: str,
        node_id: str,
        node_key: str,
        agent_key: str,
        attempt: int = 1,
        priority: int = 100,
        exclude_agent_ids: list[str] | None = None,
        delay_seconds: float = 0,
    ) -> str:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        fields = {
            "event_id": str(uuid.uuid4()),
            "tenant_id": DEFAULT_TENANT_ID,
            "task_id": task_id,
            "node_id": node_id,
            "node_key": node_key,
            "agent_key": agent_key,
            "attempt": str(attempt),
            "priority": str(priority),
            "exclude_agent_ids": ",".join(exclude_agent_ids or []),
            "enqueued_at": _utc_now(),
        }
        return self.r.xadd(EXECUTION_STREAM, fields)

    def publish_execution_event(self, event_type: str, payload: dict[str, Any]) -> str:
        fields = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "ts": _utc_now(),
            **{k: _stringify(v) for k, v in payload.items()},
        }
        msg_id = self.r.xadd(EXECUTION_EVENTS, fields)
        self._fanout_pubsub(fields, task_id=str(payload.get("task_id") or ""))
        return msg_id

    def publish_task_event(self, event_type: str, payload: dict[str, Any]) -> str:
        fields = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "ts": _utc_now(),
            **{k: _stringify(v) for k, v in payload.items()},
        }
        msg_id = self.r.xadd(TASK_EVENTS, fields)
        self._fanout_pubsub(fields, task_id=str(payload.get("task_id") or ""))
        return msg_id

    def _fanout_pubsub(self, fields: dict[str, str], *, task_id: str) -> None:
        body = json.dumps(fields, ensure_ascii=False)
        try:
            self.r.publish(CHANNEL_GLOBAL, body)
            if task_id:
                self.r.publish(task_events_channel(task_id), body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pubsub fan-out failed: %s", exc)

    def subscribe_task_events(self, task_id: str):
        pubsub = self.r.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(task_events_channel(task_id))
        return pubsub

    def subscribe_global_events(self):
        pubsub = self.r.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(CHANNEL_GLOBAL)
        return pubsub

    def read_execution(
        self,
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 2000,
    ) -> list[tuple[str, dict[str, str]]]:
        rows = self.r.xreadgroup(
            EXECUTOR_GROUP,
            consumer,
            {EXECUTION_STREAM: ">"},
            count=count,
            block=block_ms,
        )
        out: list[tuple[str, dict[str, str]]] = []
        if not rows:
            return out
        for _stream, messages in rows:
            for msg_id, fields in messages:
                out.append((msg_id, fields))
        return out

    def ack_execution(self, message_id: str) -> None:
        self.r.xack(EXECUTION_STREAM, EXECUTOR_GROUP, message_id)

    def acquire_task_lock(self, task_id: str, worker_id: str, ttl: int = 30) -> bool:
        return bool(self.r.set(f"lock:task:{task_id}", worker_id, nx=True, ex=ttl))

    def release_task_lock(self, task_id: str, worker_id: str) -> None:
        key = f"lock:task:{task_id}"
        current = self.r.get(key)
        if current == worker_id:
            self.r.delete(key)

def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)
