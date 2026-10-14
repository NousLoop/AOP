"""JobQueue: Redis Stream enqueue operations for task execution."""

from __future__ import annotations

from typing import Any

from streams import StreamClient

# Outbox pattern for P36.2 distributed transactions (lazy import)
try:
    from outbox import get_outbox_processor
    OUTBOX_AVAILABLE = True
except ImportError:
    OUTBOX_AVAILABLE = False
    def get_outbox_processor():
        return None


class JobQueue:
    """Manages job enqueue operations to Redis Streams."""

    def __init__(self, streams: StreamClient | None = None) -> None:
        self.streams = streams or StreamClient()
        self._outbox_processor = None  # Lazy-loaded

    def _get_outbox_processor(self):
        """Lazy import of outbox processor."""
        if self._outbox_processor is None:
            self._outbox_processor = get_outbox_processor()
        return self._outbox_processor

    def enqueue(
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
        use_outbox: bool = True,
    ) -> str:
        """Enqueue a single job to Redis Stream.
        
        Args:
            use_outbox: If True, use outbox pattern for atomic coordination
        
        Returns:
            Redis Stream message ID (or outbox event ID if using outbox)
        """
        # P36.2: Use outbox pattern for atomic coordination
        if use_outbox and self._get_outbox_processor():
            processor = self._get_outbox_processor()
            event_id = processor.write_event(
                event_type="job_enqueue",
                payload={
                    "task_id": task_id,
                    "node_id": node_id,
                    "node_key": node_key,
                    "agent_key": agent_key,
                    "attempt": attempt,
                    "priority": priority,
                    "exclude_agent_ids": exclude_agent_ids or [],
                    "delay_seconds": delay_seconds,
                },
                target_stream="a2a.execution.queue",
            )
            return event_id or ""
        
        # Fallback to direct Redis enqueue
        return self.streams.enqueue_execution(
            task_id=task_id,
            node_id=node_id,
            node_key=node_key,
            agent_key=agent_key,
            attempt=attempt,
            priority=priority,
            exclude_agent_ids=exclude_agent_ids,
            delay_seconds=delay_seconds,
        )

    def enqueue_batch(self, jobs: list[dict[str, Any]]) -> list[str]:
        """Enqueue multiple jobs to Redis Stream.
        
        Args:
            jobs: List of job dictionaries with keys:
                  task_id, node_id, node_key, agent_key, attempt, 
                  exclude_agent_ids, delay_seconds
        
        Returns:
            List of Redis Stream message IDs
        """
        message_ids = []
        for job in jobs:
            message_id = self.enqueue(
                task_id=job["task_id"],
                node_id=job["node_id"],
                node_key=job["node_key"],
                agent_key=job["agent_key"],
                attempt=job.get("attempt", 1),
                exclude_agent_ids=job.get("exclude_agent_ids"),
                delay_seconds=job.get("delay_seconds", 0),
            )
            message_ids.append(message_id)
        return message_ids
