"""Outbox processor background service (P36.2).

This service runs as a background job to process outbox events
and publish them to Redis streams reliably.
"""

from __future__ import annotations

import time
import signal
import sys
from typing import Any

from outbox import get_outbox_processor
from streams import StreamClient


class OutboxProcessorService:
    """Background service for processing outbox events."""

    def __init__(
        self,
        poll_interval: float = 1.0,
        batch_size: int = 100,
    ):
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.processor = get_outbox_processor()
        self.streams = StreamClient()
        self.running = False

    def process_event(self, event: dict[str, Any]) -> bool:
        """Process a single outbox event by publishing to Redis."""
        try:
            event_type = event["event_type"]
            payload = event["payload"]
            target_stream = event["target_stream"]

            if event_type == "job_enqueue":
                # Publish job to Redis stream
                self.streams.enqueue_execution(
                    task_id=payload["task_id"],
                    node_id=payload["node_id"],
                    node_key=payload["node_key"],
                    agent_key=payload["agent_key"],
                    attempt=payload.get("attempt", 1),
                    priority=payload.get("priority", 100),
                    exclude_agent_ids=payload.get("exclude_agent_ids"),
                    delay_seconds=payload.get("delay_seconds", 0),
                )
                return True
            elif event_type == "task_event":
                # Publish task event
                self.streams.publish_task_event(
                    payload.get("event_type", "unknown"),
                    payload.get("data", {}),
                )
                return True
            elif event_type == "execution_event":
                # Publish execution event
                self.streams.publish_execution_event(
                    payload.get("event_type", "unknown"),
                    payload.get("data", {}),
                )
                return True
            else:
                print(f"[outbox] Unknown event type: {event_type}")
                return False
        except Exception as e:
            print(f"[outbox] Error processing event: {e}")
            return False

    def process_batch(self) -> int:
        """Process a batch of pending outbox events."""
        processed = 0
        try:
            # Process pending events using the outbox processor
            processed = self.processor.process_pending_events(limit=self.batch_size)
            
            if processed > 0:
                print(f"[outbox] Processed {processed} pending events")
        except Exception as e:
            print(f"[outbox] Error in process_batch: {e}")
        
        return processed

    def run_forever(self) -> None:
        """Run the outbox processor service forever."""
        self.running = True
        print("[outbox] Starting outbox processor service")
        
        # Setup signal handlers
        def signal_handler(signum, frame):
            print(f"[outbox] Received signal {signum}, shutting down")
            self.running = False
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        while self.running:
            try:
                try:
                    from pulse import beat_outbox

                    beat_outbox()
                except Exception:  # noqa: BLE001
                    pass
                processed = self.process_batch()
                if processed > 0:
                    print(f"[outbox] Processed {processed} events")
                
                # Cleanup old events periodically
                if time.time() % 3600 < self.poll_interval:  # Once per hour
                    cleaned = self.processor.cleanup_old_events(days=7)
                    if cleaned > 0:
                        print(f"[outbox] Cleaned up {cleaned} old events")
                
                time.sleep(self.poll_interval)
            except Exception as e:
                print(f"[outbox] Error in main loop: {e}")
                time.sleep(self.poll_interval)

        print("[outbox] Outbox processor service stopped")


def main() -> int:
    """Main entry point for outbox processor service."""
    import os
    
    poll_interval = float(os.getenv("OUTBOX_POLL_INTERVAL", "1.0"))
    batch_size = int(os.getenv("OUTBOX_BATCH_SIZE", "100"))
    
    service = OutboxProcessorService(
        poll_interval=poll_interval,
        batch_size=batch_size,
    )
    
    try:
        service.run_forever()
        return 0
    except KeyboardInterrupt:
        print("[outbox] Interrupted by user")
        return 0
    except Exception as e:
        print(f"[outbox] Fatal error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
