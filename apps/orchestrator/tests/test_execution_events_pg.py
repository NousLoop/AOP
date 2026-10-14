"""Postgres execution event log survives a new bus instance."""

from __future__ import annotations

import uuid

from execution.events import ExecutionEvent, PostgresExecutionEventBus
from tests.conftest import requires_pg


@requires_pg
def test_postgres_event_bus_persists_and_is_idempotent():
    root = f"root-{uuid.uuid4().hex[:12]}"
    eid = f"evt-{uuid.uuid4().hex}"
    bus = PostgresExecutionEventBus()
    bus.ping()
    e1 = bus.emit(
        ExecutionEvent(
            event_type="task.created",
            event_id=eid,
            task_id=root,
            root_task_id=root,
        )
    )
    e1b = bus.emit(
        ExecutionEvent(
            event_type="task.created",
            event_id=eid,
            task_id=root,
            root_task_id=root,
        )
    )
    assert e1.sequence == e1b.sequence == 1

    other = PostgresExecutionEventBus()
    rows = other.list_for_root(root)
    assert len(rows) == 1
    assert rows[0]["event_id"] == eid
    assert rows[0]["sequence"] == 1
