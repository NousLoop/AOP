"""Redis TTL heartbeats so Console preflight can tell Worker / Outbox are alive.

Empty queues look healthy even when nobody is consuming. These keys close that gap.
"""

from __future__ import annotations

import os
import time
from typing import Any

import redis

WORKER_KEY = "aop:pulse:worker"
OUTBOX_KEY = "aop:pulse:outbox"
NODE_KEY_PREFIX = "aop:pulse:node:"
TTL_S = 15
# aop-node default interval is 30s; keep a longer TTL so preflight does not flap.
NODE_TTL_S = 90

_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        _client = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=2.0
        )
    return _client


def beat_worker(worker_id: str) -> None:
    try:
        _redis().setex(WORKER_KEY, TTL_S, worker_id or "worker")
    except Exception:  # noqa: BLE001
        pass


def beat_outbox() -> None:
    try:
        _redis().setex(OUTBOX_KEY, TTL_S, str(int(time.time())))
    except Exception:  # noqa: BLE001
        pass


def beat_node(node_id: str, payload: str = "") -> None:
    nid = (node_id or "").strip()
    if not nid:
        return
    try:
        _redis().setex(f"{NODE_KEY_PREFIX}{nid}", NODE_TTL_S, payload or nid)
    except Exception:  # noqa: BLE001
        pass


def list_nodes(client: redis.Redis | None = None) -> list[dict[str, Any]]:
    r = client if client is not None else _redis()
    out: list[dict[str, Any]] = []
    try:
        keys = r.keys(f"{NODE_KEY_PREFIX}*") or []
        for key in keys:
            nid = str(key)[len(NODE_KEY_PREFIX) :]
            out.append({"node_id": nid, "payload": r.get(key), "ok": True})
    except Exception:  # noqa: BLE001
        return []
    return out


def read_pulses(client: redis.Redis | None = None) -> dict[str, Any]:
    r = client if client is not None else _redis()
    worker = r.get(WORKER_KEY)
    outbox = r.get(OUTBOX_KEY)
    nodes = list_nodes(r)
    return {
        "worker_id": worker,
        "worker_ok": bool(worker),
        "outbox_ok": bool(outbox),
        "outbox_beat": outbox,
        "ttl_s": TTL_S,
        "nodes": nodes,
        "node_ok": bool(nodes),
    }
