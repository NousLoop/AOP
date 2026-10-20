"""Scheduler-level request tracking integration (via service, not HTTP)."""

from __future__ import annotations

import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from request_tracking import RequestTrackingService


def test_request_tracking_direct(database_url):
    """Track → complete → cache → duplicate detection through RequestTrackingService."""
    tracking = RequestTrackingService(database_url=database_url)

    task_id = str(uuid.uuid4())
    node_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    tenant_id = "00000000-0000-0000-0000-000000000001"
    node_key = "test-node"
    idempotency_key = f"req_{task_id[:8]}_{node_key}"

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO agents (
                  id, tenant_id, agent_key, name, protocol, status, created_at, updated_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, %s, %s, now(), now()
                )
                """,
                (
                    agent_id,
                    tenant_id,
                    f"test-agent-{agent_id[:8]}",
                    "Test Agent",
                    "A2A",
                    "active",
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, tenant_id, title, input_json, status, progress,
                  plan_json, created_at, updated_at, started_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s::jsonb, 'created', 0,
                  %s::jsonb, now(), now(), now()
                )
                """,
                (
                    task_id,
                    tenant_id,
                    "Test Task",
                    Jsonb({"type": "text", "content": "test"}),
                    Jsonb({"nodes": []}),
                ),
            )
            conn.execute(
                """
                INSERT INTO task_nodes (
                  id, task_id, node_key, agent_key, status, input_json,
                  attempt, max_retry, idempotency_key, created_at, updated_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, 'pending', %s::jsonb,
                  0, 3, %s, now(), now()
                )
                """,
                (
                    node_id,
                    task_id,
                    node_key,
                    "claude-code",
                    Jsonb({"goal": "test"}),
                    idempotency_key,
                ),
            )

    try:
        result = tracking.track_request(
            idempotency_key,
            task_id,
            node_id,
            agent_id,
            {"query": "test query"},
        )
        assert result is not None
        assert result.get("idempotency_key") == idempotency_key
        assert result.get("status") == "pending"

        assert tracking.mark_request_completed(
            idempotency_key, {"result": "test result"}
        )
        assert tracking.is_request_completed(idempotency_key) is True
        assert tracking.get_cached_response(idempotency_key) == {
            "result": "test result"
        }

        result2 = tracking.track_request(
            idempotency_key,
            task_id,
            node_id,
            agent_id,
            {"query": "test query"},
        )
        assert result2 is not None
        assert result2.get("id") == result.get("id")
    finally:
        with psycopg.connect(database_url) as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM a2a_requests WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                conn.execute(
                    "DELETE FROM task_nodes WHERE task_id = %s::uuid",
                    (task_id,),
                )
                conn.execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))
                conn.execute("DELETE FROM agents WHERE id = %s::uuid", (agent_id,))
