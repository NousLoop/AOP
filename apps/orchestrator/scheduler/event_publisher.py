"""EventPublisher: Task lifecycle events, aggregation, and evaluation."""

from __future__ import annotations

from typing import Any

from aggregator import Aggregator
from evaluation import EvaluationService
from streams import StreamClient


class EventPublisher:
    """Publishes task lifecycle events and triggers side effects."""

    def __init__(
        self,
        streams: StreamClient | None = None,
        aggregator: Aggregator | None = None,
        evaluations: EvaluationService | None = None,
    ) -> None:
        self.streams = streams or StreamClient()
        self.aggregator = aggregator or Aggregator()
        self.evaluations = evaluations or EvaluationService()

    def publish_node_enqueued(
        self, task_id: str, node_key: str, agent_key: str
    ) -> str:
        """Publish node enqueued event.
        
        Returns:
            Redis Stream message ID
        """
        return self.streams.publish_task_event(
            "task.node.enqueued",
            {
                "task_id": task_id,
                "node_key": node_key,
                "agent_key": agent_key,
            },
        )

    def publish_task_completed(self, task_id: str) -> dict[str, Any]:
        """Publish task completed event with aggregation and evaluation.
        
        Returns:
            Dictionary with aggregation result and evaluation
        """
        try:
            result_json = self.aggregator.build_result(task_id)
            self.streams.publish_task_event(
                "task.completed",
                {
                    "task_id": task_id,
                    "summary_len": len(result_json.get("summary") or ""),
                },
            )
        except Exception:  # noqa: BLE001
            result_json = {}

        try:
            evaluation = self.evaluations.evaluate(task_id)
        except Exception:  # noqa: BLE001
            evaluation = {}

        return {"aggregation": result_json, "evaluation": evaluation}

    def publish_task_failed(self, task_id: str) -> dict[str, Any]:
        """Publish task failed event with evaluation.
        
        Returns:
            Dictionary with evaluation result
        """
        try:
            evaluation = self.evaluations.evaluate(task_id)
        except Exception:  # noqa: BLE001
            evaluation = {}

        return {"evaluation": evaluation}

    def publish_node_completed(
        self, task_id: str, node_key: str, agent_id: str, latency_ms: int
    ) -> str:
        """Publish node completed event.
        
        Returns:
            Redis Stream message ID
        """
        return self.streams.publish_execution_event(
            "agent.task.completed",
            {
                "task_id": task_id,
                "node_key": node_key,
                "agent_id": agent_id,
                "latency_ms": latency_ms,
            },
        )

    def publish_node_started(
        self, task_id: str, node_key: str, agent_id: str, attempt: int
    ) -> str:
        """Publish node started event.
        
        Returns:
            Redis Stream message ID
        """
        return self.streams.publish_execution_event(
            "agent.task.started",
            {
                "task_id": task_id,
                "node_key": node_key,
                "agent_id": agent_id,
                "attempt": attempt,
            },
        )

    def publish_node_failed(
        self,
        task_id: str,
        node_key: str,
        error: str,
        attempt: int,
        decision: str,
    ) -> str:
        """Publish node failed event.
        
        Returns:
            Redis Stream message ID
        """
        return self.streams.publish_execution_event(
            "agent.task.failed",
            {
                "task_id": task_id,
                "node_key": node_key,
                "error": error,
                "attempt": attempt,
                "decision": decision,
            },
        )
