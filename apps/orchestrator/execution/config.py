"""Phase 4 — production configuration for the durable execution plane."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass
class ExecutionConfig:
    """Defaults are production-safe; tests override via env or constructor."""

    store: str = "postgres"  # postgres | memory
    lease_duration_s: float = 30.0
    lease_renewal_s: float = 10.0
    recovery_enabled: bool = True
    recovery_interval_s: float = 5.0
    recovery_batch_size: int = 50
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    outbox_enabled: bool = True
    outbox_poll_interval_s: float = 1.0
    outbox_batch_size: int = 100
    queue_enabled: bool = True
    max_queue_depth: int = 100
    collaboration_stream: bool = True
    owner_id: str = ""

    @classmethod
    def from_env(cls) -> "ExecutionConfig":
        # Hermetic tests / local without DB: EXECUTION_STORE=memory
        store = (os.getenv("EXECUTION_STORE") or "postgres").strip().lower()
        if store not in {"postgres", "memory"}:
            store = "postgres"
        # Postgres is required in process unless EXECUTION_STORE=memory.
        # factory.build_execution_service raises if postgres cannot be probed.
        owner = os.getenv("A2A_OWNER_ID") or os.getenv("HOSTNAME") or f"pid-{os.getpid()}"
        return cls(
            store=store,
            lease_duration_s=_env_float("EXECUTION_LEASE_SECONDS", 30.0),
            lease_renewal_s=_env_float("EXECUTION_LEASE_RENEWAL_SECONDS", 10.0),
            recovery_enabled=_env_bool("EXECUTION_RECOVERY_ENABLED", True),
            recovery_interval_s=_env_float("EXECUTION_RECOVERY_INTERVAL_SECONDS", 5.0),
            recovery_batch_size=_env_int("EXECUTION_RECOVERY_BATCH_SIZE", 50),
            max_retries=_env_int("EXECUTION_MAX_RETRIES", 3),
            base_delay_s=_env_float("EXECUTION_RETRY_BASE_DELAY", 1.0),
            max_delay_s=_env_float("EXECUTION_RETRY_MAX_DELAY", 30.0),
            outbox_enabled=_env_bool("EXECUTION_OUTBOX_ENABLED", True),
            outbox_poll_interval_s=_env_float("OUTBOX_POLL_INTERVAL_SECONDS", 1.0),
            outbox_batch_size=_env_int("OUTBOX_BATCH_SIZE", 100),
            queue_enabled=_env_bool("CAPACITY_QUEUE_ENABLED", True),
            max_queue_depth=_env_int("CAPACITY_MAX_QUEUE_DEPTH", 100),
            collaboration_stream=_env_bool("COLLABORATION_STREAM_ENABLED", True),
            owner_id=owner,
        )


__all__ = ["ExecutionConfig"]
