"""Node pulse keys are distinct from worker/outbox and from A2A agents."""

from __future__ import annotations

import pulse


class _MemRedis:
    def __init__(self):
        self.store: dict[str, tuple[int, str]] = {}

    def setex(self, key, ttl, value):
        self.store[key] = (int(ttl), str(value))

    def keys(self, pattern):
        prefix = str(pattern).rstrip("*")
        return [k for k in self.store if str(k).startswith(prefix)]

    def get(self, key):
        row = self.store.get(key)
        return None if row is None else row[1]


def test_beat_node_listed_separately(monkeypatch):
    mem = _MemRedis()
    monkeypatch.setattr(pulse, "_redis", lambda: mem)
    pulse.beat_worker("w1")
    pulse.beat_outbox()
    pulse.beat_node("node-local-1", '{"status":"online"}')
    data = pulse.read_pulses(mem)
    assert data["worker_ok"] is True
    assert data["outbox_ok"] is True
    assert data["node_ok"] is True
    assert data["nodes"][0]["node_id"] == "node-local-1"
    assert mem.store[f"{pulse.NODE_KEY_PREFIX}node-local-1"][0] == pulse.NODE_TTL_S
