"""Outbox pattern for distributed transaction coordination (P36.2).

This module implements the outbox pattern to ensure atomic coordination
between PostgreSQL state changes and Redis queue operations.
"""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from db import connect
from psycopg.types.json import Jsonb

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

class OutboxEvent:
    """Represents an outbox event for reliable message delivery."""

    def __init__(
        self,
        event_type: str,
        payload: dict[str, Any],
        target_stream: str,
    ):
        self.event_type = event_type
        self.payload = payload
        self.target_stream = target_stream
        self.status = "pending"
        self.attempts = 0
        self.last_error = None
        self.created_at = _utc_now()
        self.processed_at = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "payload": self.payload,
            "target_stream": self.target_stream,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
        }

class OutboxProcessor:
    """Processes outbox events and publishes to Redis streams."""

    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id
        self._streams_client = None  # Lazy-loaded

    def _get_streams_client(self):
        """Lazy import of StreamClient to avoid circular dependency."""
        if self._streams_client is None:
            try:
                from streams import StreamClient
                self._streams_client = StreamClient()
            except ImportError:
                self._streams_client = None
        return self._streams_client

    def write_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        target_stream: str,
    ) -> str:
        """Write an outbox event to PostgreSQL."""
        with connect(self.database_url) as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO outbox_events (
                      event_type, payload, target_stream, status, created_at
                    ) VALUES (
                      %s, %s::jsonb, %s, 'pending', %s
                    )
                    RETURNING id::text
                    """,
                    (event_type, Jsonb(payload), target_stream, _utc_now()),
                ).fetchone()
                return row["id"] if row else ""

    def _publish_event_to_redis(
        self,
        event_type: str,
        payload: dict[str, Any],
        target_stream: str,
    ) -> None:
        """Publish a single event's payload to its target Redis stream."""
        streams = self._get_streams_client()
        if not streams:
            raise RuntimeError("StreamClient unavailable")

        if event_type == "job_enqueue":
            streams.enqueue_execution(
                task_id=payload["task_id"],
                node_id=payload["node_id"],
                node_key=payload["node_key"],
                agent_key=payload["agent_key"],
                attempt=payload.get("attempt", 1),
                priority=payload.get("priority", 100),
                exclude_agent_ids=payload.get("exclude_agent_ids"),
                delay_seconds=payload.get("delay_seconds", 0),
            )
        elif event_type == "task_event":
            streams.publish_task_event(
                payload.get("event_type", "unknown"),
                payload.get("data", {}),
            )
        elif event_type == "execution_event":
            streams.publish_execution_event(
                payload.get("event_type", "unknown"),
                payload.get("data", {}),
            )
        else:
            raise ValueError(f"unknown event type: {event_type}")

    def publish_to_redis(self, event_id: str, event_data: dict[str, Any]) -> bool:
        """Publish an outbox event to Redis and mark it processed (standalone)."""
        try:
            self._publish_event_to_redis(
                event_data["event_type"],
                event_data["payload"],
                event_data["target_stream"],
            )
            with connect(self.database_url) as conn:
                with conn.transaction():
                    result = conn.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'processed',
                            processed_at = %s
                        WHERE id = %s::uuid AND status = 'pending'
                        RETURNING id::text
                        """,
                        (_utc_now(), event_id),
                    ).fetchone()
                    return result is not None
        except Exception as e:
            print(f"[outbox] Error publishing to Redis: {e}")
            return False

    def process_pending_events(self, limit: int = 100) -> int:
        """Process pending outbox events.

        Redis publish and the ``processed`` UPDATE share the same connection so
        the ``FOR UPDATE SKIP LOCKED`` row locks are released without blocking
        a second connection (avoids a self-deadlock).
        """
        with connect(self.database_url) as conn:
            # Get pending events
            rows = conn.execute(
                """
                SELECT id::text, event_type, payload, target_stream
                FROM outbox_events
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            ).fetchall()

            processed_count = 0
            for row in rows:
                event_id = row["id"]
                try:
                    self._publish_event_to_redis(
                        row["event_type"], row["payload"], row["target_stream"]
                    )
                    conn.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'processed', processed_at = %s
                        WHERE id = %s::uuid
                        """,
                        (_utc_now(), event_id),
                    )
                    processed_count += 1
                except Exception as e:
                    print(f"[outbox] Error processing event {event_id}: {e}")
                    conn.execute(
                        """
                        UPDATE outbox_events
                        SET status = 'failed',
                            last_error = %s,
                            attempts = attempts + 1
                        WHERE id = %s::uuid
                        """,
                        (str(e), event_id),
                    )
            return processed_count

    def get_failed_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get failed outbox events for retry."""
        with connect(self.database_url) as conn:
            rows = conn.execute(
                """
                SELECT id::text, event_type, payload, target_stream, attempts, last_error
                FROM outbox_events
                WHERE status = 'failed'
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def retry_failed_event(self, event_id: str) -> bool:
        """Retry a failed outbox event."""
        with connect(self.database_url) as conn:
            with conn.transaction():
                result = conn.execute(
                    """
                    UPDATE outbox_events
                    SET status = 'pending',
                        last_error = NULL
                    WHERE id = %s::uuid AND status = 'failed'
                    RETURNING id::text
                    """,
                    (event_id,),
                ).fetchone()
                return result is not None

    def cleanup_old_events(self, days: int = 7) -> int:
        """Clean up old processed events."""
        with connect(self.database_url) as conn:
            with conn.transaction():
                result = conn.execute(
                    """
                    DELETE FROM outbox_events
                    WHERE status = 'processed'
                      AND processed_at < (now() - (%s || ' days')::interval)
                    RETURNING id
                    """,
                    (days,),
                ).fetchall()
                return len(result)

    def get_stats(self) -> dict[str, Any]:
        """Get outbox statistics."""
        with connect(self.database_url) as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) as count
                FROM outbox_events
                GROUP BY status
                """,
            ).fetchall()
            return {row["status"]: row["count"] for row in rows}

# Singleton instance for application use
_outbox_processor: OutboxProcessor | None = None

def get_outbox_processor() -> OutboxProcessor:
    """Get or create the singleton outbox processor."""
    global _outbox_processor
    if _outbox_processor is None:
        _outbox_processor = OutboxProcessor()
    return _outbox_processor
