"""Phase 4 — construct ExecutionService with production-default Postgres store."""

from __future__ import annotations

import logging
import os
from typing import Optional

from .config import ExecutionConfig
from .events import ExecutionEventBus, PostgresExecutionEventBus
from .record import ExecutionRecordStore, PostgresExecutionRecordStore
from .retry import RetryPolicy, TimeoutPolicy
from .service import ExecutionService

logger = logging.getLogger(__name__)


def build_execution_service(
    config: ExecutionConfig | None = None,
    *,
    force_memory: bool | None = None,
) -> ExecutionService:
    """Production default is Postgres; tests set EXECUTION_STORE=memory."""
    cfg = config or ExecutionConfig.from_env()
    use_memory = force_memory if force_memory is not None else (cfg.store == "memory")
    # Pytest / hermetic: prefer memory unless explicitly postgres
    if os.getenv("PYTEST_CURRENT_TEST") and cfg.store != "postgres":
        use_memory = True

    retry = RetryPolicy(
        max_retries=cfg.max_retries,
        base_delay_s=cfg.base_delay_s,
        max_delay_s=cfg.max_delay_s,
    )
    timeout = TimeoutPolicy(
        task_timeout_s=float(os.getenv("EXECUTION_TASK_TIMEOUT", "300")),
        delegation_timeout_s=float(os.getenv("EXECUTION_DELEGATION_TIMEOUT", "120")),
        agent_timeout_s=float(os.getenv("EXECUTION_AGENT_TIMEOUT", "60")),
    )

    store: ExecutionRecordStore | PostgresExecutionRecordStore
    events: ExecutionEventBus
    if use_memory:
        events = ExecutionEventBus()
        store = ExecutionRecordStore()
        # Hermetic: don't hit Postgres outbox from memory path
        if cfg.store != "postgres":
            cfg.outbox_enabled = False
        logger.info("execution store=memory")
    else:
        try:
            events = PostgresExecutionEventBus()
            events.ping()
            store = PostgresExecutionRecordStore()
            store.list_for_root("__probe_nonexistent__")
            logger.info("execution store=postgres events=postgres")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Postgres execution store unavailable; "
                "set EXECUTION_STORE=memory only for hermetic tests"
            ) from exc

    svc = ExecutionService(
        store=store,  # type: ignore[arg-type]
        events=events,
        retry_policy=retry,
        timeout_policy=timeout,
        config=cfg,
    )
    return svc


__all__ = ["build_execution_service"]
