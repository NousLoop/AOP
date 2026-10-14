"""Phase 3/4 — ExecutionService: binds record SoT + events + retry/recover/cancel."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .config import ExecutionConfig
from .events import (
    DELEGATION_FAILED,
    DELEGATION_RELEASED,
    DELEGATION_REQUESTED,
    ExecutionEvent,
    ExecutionEventBus,
    TASK_CANCELLED,
)
from .record import ExecutionRecord, ExecutionRecordStore
from .retry import RetryPolicy, TimeoutPolicy
from .state_machine import (
    ExecutionState,
    ExecutionStateMachine,
    IllegalTransition,
    RecoveryClass,
)

logger = logging.getLogger(__name__)


class ExecutionService:
    """OS execution runtime façade. Record is SoT; events are propagation."""

    def __init__(
        self,
        store: ExecutionRecordStore | None = None,
        events: ExecutionEventBus | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_policy: TimeoutPolicy | None = None,
        config: ExecutionConfig | None = None,
    ):
        self.store = store or ExecutionRecordStore()
        self.events = events or ExecutionEventBus()
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout_policy = timeout_policy or TimeoutPolicy()
        # Direct construction (unit tests) stays hermetic; production uses factory.
        self.config = config or ExecutionConfig(store="memory", outbox_enabled=False)
        self._outbox_enabled = bool(self.config.outbox_enabled)

    def create(
        self,
        *,
        task_id: str,
        root_task_id: str | None = None,
        correlation_id: str | None = None,
        parent_task_id: str | None = None,
        agent_id: str | None = None,
        operation: str = "execute",
        idempotency_key: str | None = None,
        max_retries: int | None = None,
        branch_lineage: list[str] | None = None,
        visited_agents: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> ExecutionRecord:
        if idempotency_key:
            existing = self.store.get_by_idempotency(idempotency_key)
            if existing:
                return existing
        # Phase 5: inherit TenantContext when not explicitly provided
        try:
            from scheduling.context import get_tenant_context

            tctx = get_tenant_context()
        except Exception:  # noqa: BLE001
            tctx = None
        rec = ExecutionRecord(
            task_id=task_id,
            root_task_id=root_task_id or task_id,
            correlation_id=correlation_id or task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            operation=operation,
            state=ExecutionState.PENDING.value,
            max_retries=max_retries if max_retries is not None else self.retry_policy.max_retries,
            idempotency_key=idempotency_key,
            branch_lineage=list(branch_lineage or []),
            visited_agents=dict(visited_agents or {}),
            metadata=dict(metadata or {}),
            tenant_id=tenant_id or (tctx.tenant_id if tctx else None),
            user_id=user_id or (tctx.user_id if tctx else None),
            project_id=project_id or (tctx.project_id if tctx else None),
        )
        saved = self.store.upsert(rec)
        self._emit(ExecutionStateMachine.event_for(ExecutionState.PENDING), saved)
        return saved

    def transition(
        self,
        task_id: str,
        target: ExecutionState | str,
        *,
        operation: str = "execute",
        error: str | None = None,
        allow_recover_retry: bool = False,
        bump_attempt: bool = False,
        metadata_patch: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        rec, changed, event_type = self.store.transition(
            task_id,
            target,
            operation=operation,
            error=error,
            allow_recover_retry=allow_recover_retry,
            bump_attempt=bump_attempt,
            metadata_patch=metadata_patch,
        )
        if changed:
            self._emit(event_type, rec, extra={"error": error} if error else None)
        return rec

    def start(self, task_id: str, *, operation: str = "execute") -> ExecutionRecord:
        return self.transition(task_id, ExecutionState.RUNNING, operation=operation)

    def succeed(self, task_id: str, *, operation: str = "execute") -> ExecutionRecord:
        rec = self.transition(task_id, ExecutionState.SUCCEEDED, operation=operation)
        # Phase 5.3: auto cost record on success (hermetic in-memory by default)
        try:
            from scheduling.cost import CostService

            if not hasattr(self, "cost_service") or self.cost_service is None:
                self.cost_service = CostService()
            self.cost_service.record_from_execution(rec)
        except Exception:  # noqa: BLE001
            pass
        return rec

    def fail(
        self, task_id: str, error: str, *, operation: str = "execute"
    ) -> ExecutionRecord:
        return self.transition(
            task_id, ExecutionState.FAILED, operation=operation, error=error
        )

    def timeout(self, task_id: str, *, operation: str = "execute") -> ExecutionRecord:
        return self.transition(task_id, ExecutionState.TIMEOUT, operation=operation)

    def cancel(self, task_id: str, *, operation: str = "execute") -> ExecutionRecord:
        return self.transition(task_id, ExecutionState.CANCELLED, operation=operation)

    def apply_callback(
        self,
        task_id: str,
        *,
        success: bool,
        operation: str = "execute",
        error: str | None = None,
    ) -> ExecutionRecord:
        """Apply a completion callback with terminal protection.

        Late callbacks after TIMEOUT/CANCELLED/SUCCEEDED are ignored (idempotent).
        """
        rec = self.store.get(task_id, operation=operation)
        if rec is None:
            raise KeyError(task_id)
        if ExecutionStateMachine.is_terminal(rec.state):
            # Duplicate / late callback: do not regress
            logger.info(
                "ignored late/duplicate callback task=%s state=%s",
                task_id,
                rec.state,
            )
            return rec
        target = ExecutionState.SUCCEEDED if success else ExecutionState.FAILED
        return self.transition(task_id, target, operation=operation, error=error)

    def mark_retry(
        self, task_id: str, *, operation: str = "execute", error_code: str | None = None
    ) -> ExecutionRecord:
        rec = self.store.get(task_id, operation=operation)
        if rec is None:
            raise KeyError(task_id)
        policy = RetryPolicy(
            max_retries=rec.max_retries,
            base_delay_s=self.retry_policy.base_delay_s,
            max_delay_s=self.retry_policy.max_delay_s,
            retryable_errors=self.retry_policy.retryable_errors,
        )
        if not policy.should_retry(rec.attempt, error_code):
            return self.fail(
                task_id,
                error=f"retry exhausted after {rec.attempt} attempts",
                operation=operation,
            )
        delay = policy.backoff(rec.attempt)
        # Phase 5.8: attach failure classification + reselection hint (does not
        # mutate agent_id here — caller rediscovers with exclude list).
        meta_patch: dict[str, Any] = {
            "next_delay_s": delay,
            "last_error_code": error_code,
        }
        try:
            from scheduling.reselection import classify_failure, plan_recovery

            klass = classify_failure(error_code=error_code)
            plan = plan_recovery(klass, failed_agent_id=rec.agent_id)
            meta_patch["failure_class"] = plan["class"]
            meta_patch["recovery_action"] = plan["action"]
            meta_patch["exclude_agent_ids"] = plan["exclude_agent_ids"]
            meta_patch["reselect"] = plan.get("reselect", False)
        except Exception:  # noqa: BLE001
            pass
        return self.transition(
            task_id,
            ExecutionState.RETRYING,
            operation=operation,
            allow_recover_retry=True,
            bump_attempt=True,
            metadata_patch=meta_patch,
        )

    def recover(
        self,
        task_id: str,
        *,
        operation: str = "execute",
        stale_after_s: float = 120.0,
        owner_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> dict[str, Any]:
        classification = self.store.classify(
            task_id, operation=operation, stale_after_s=stale_after_s
        )
        klass = classification["class"]
        rec = classification.get("record")
        if klass == RecoveryClass.UNKNOWN.value:
            return {"action": "none", "class": klass, "reason": "no execution record"}
        if klass == RecoveryClass.TERMINAL.value:
            return {"action": "none", "class": klass, "record": rec}
        if klass == RecoveryClass.RUNNING.value:
            return {"action": "wait", "class": klass, "record": rec}

        lease = (
            lease_seconds
            if lease_seconds is not None
            else float(getattr(self.config, "lease_duration_s", 30.0))
        )
        owner = owner_id or getattr(self.config, "owner_id", None) or "recovery"
        token = self.store.try_acquire_ownership(
            task_id,
            operation=operation,
            owner_id=owner,
            lease_seconds=lease,
        )
        if not token:
            return {
                "action": "contention",
                "class": klass,
                "reason": "another worker owns recovery",
                "record": rec,
            }
        try:
            live = self.store.get(task_id, operation=operation)
            if live and live.state == ExecutionState.PENDING.value:
                updated = self.start(task_id, operation=operation)
                return {
                    "action": "start",
                    "class": klass,
                    "ownership_token": token,
                    "owner_id": owner,
                    "lease_seconds": lease,
                    "record": updated.to_dict(),
                }
            updated = self.mark_retry(task_id, operation=operation, error_code="RECOVER")
            # Visual Runtime observation (does not alter recovery semantics)
            try:
                self.events.emit(
                    ExecutionEvent(
                        event_type="agent.recovered",
                        root_task_id=updated.root_task_id,
                        correlation_id=updated.correlation_id,
                        task_id=updated.task_id,
                        agent_id=updated.agent_id,
                        payload={
                            "reason": "recover",
                            "state": updated.state,
                            "attempt": updated.attempt,
                        },
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            return {
                "action": "retry",
                "class": klass,
                "ownership_token": token,
                "owner_id": owner,
                "lease_seconds": lease,
                "record": updated.to_dict(),
                "backoff_s": updated.metadata.get("next_delay_s"),
            }
        except IllegalTransition as exc:
            self.store.release_ownership(task_id, token, operation=operation)
            return {"action": "none", "class": klass, "error": str(exc), "record": rec}

    def renew_ownership(
        self,
        task_id: str,
        token: str,
        *,
        operation: str = "execute",
        lease_seconds: float | None = None,
    ) -> bool:
        lease = (
            lease_seconds
            if lease_seconds is not None
            else float(getattr(self.config, "lease_duration_s", 30.0))
        )
        if hasattr(self.store, "renew_lease"):
            return bool(
                self.store.renew_lease(
                    task_id, token, operation=operation, lease_seconds=lease
                )
            )
        return False

    def cancel_tree(
        self,
        root_task_id: str,
        *,
        child_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Cancel root + known children; skip already-terminal records."""
        targets = [root_task_id] + list(child_task_ids or [])
        # Also cancel any records sharing the root
        for rec in self.store.list_for_root(root_task_id):
            if rec.task_id not in targets:
                targets.append(rec.task_id)
        cancelled, skipped = [], []
        for tid in targets:
            rec = self.store.get(tid)
            if rec is None:
                skipped.append({"task_id": tid, "reason": "missing"})
                continue
            if ExecutionStateMachine.is_terminal(rec.state):
                skipped.append({"task_id": tid, "reason": f"terminal:{rec.state}"})
                continue
            try:
                updated = self.cancel(tid)
                cancelled.append(updated.to_dict())
                self.events.emit(
                    ExecutionEvent(
                        event_type=TASK_CANCELLED,
                        root_task_id=updated.root_task_id,
                        correlation_id=updated.correlation_id,
                        task_id=updated.task_id,
                        agent_id=updated.agent_id,
                        parent_task_id=updated.parent_task_id,
                        payload={"propagated_from": root_task_id},
                    )
                )
            except IllegalTransition as exc:
                skipped.append({"task_id": tid, "reason": str(exc)})
        return {"cancelled": cancelled, "skipped": skipped, "root_task_id": root_task_id}

    def record_delegation(
        self,
        *,
        task_id: str,
        root_task_id: str,
        correlation_id: str,
        parent_task_id: str | None,
        agent_id: str,
        idempotency_key: str | None = None,
        branch_lineage: list[str] | None = None,
        visited_agents: dict[str, int] | None = None,
    ) -> ExecutionRecord:
        rec = self.create(
            task_id=task_id,
            root_task_id=root_task_id,
            correlation_id=correlation_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            operation="delegate",
            idempotency_key=idempotency_key,
            branch_lineage=branch_lineage,
            visited_agents=visited_agents,
        )
        self.events.emit(
            ExecutionEvent(
                event_type=DELEGATION_REQUESTED,
                root_task_id=root_task_id,
                correlation_id=correlation_id,
                task_id=task_id,
                agent_id=agent_id,
                parent_task_id=parent_task_id,
                payload={"idempotency_key": idempotency_key},
            )
        )
        return rec

    def get(self, task_id: str, *, operation: str = "execute") -> Optional[ExecutionRecord]:
        return self.store.get(task_id, operation=operation)

    def states_for_tasks(self, task_ids: list[str]) -> dict[str, str]:
        """Batch OS execute-state for DAG task list (one plane, not a second status)."""
        getter = getattr(self.store, "get_many", None)
        recs: dict[str, Any]
        if callable(getter):
            recs = getter(list(task_ids), operation="execute")
        else:
            recs = {}
            for tid in task_ids:
                rec = self.store.get(str(tid), operation="execute")
                if rec:
                    recs[str(tid)] = rec
        return {tid: rec.state for tid, rec in recs.items() if rec and rec.state}

    def get_execution_view(self, task_id: str) -> dict[str, Any]:
        execute = self.store.get(task_id, operation="execute")
        delegate = self.store.get(task_id, operation="delegate")
        events = self.events.list_for_task(task_id)
        classification = self.store.classify(task_id)
        return {
            "task_id": task_id,
            "execute": execute.to_dict() if execute else None,
            "delegate": delegate.to_dict() if delegate else None,
            "events": events,
            "recovery": classification,
        }

    def _emit(
        self,
        event_type: str,
        rec: ExecutionRecord,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "state": rec.state,
            "attempt": rec.attempt,
            "operation": rec.operation,
        }
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})
        ev = self.events.emit(
            ExecutionEvent(
                event_type=event_type,
                root_task_id=rec.root_task_id,
                correlation_id=rec.correlation_id,
                task_id=rec.task_id,
                agent_id=rec.agent_id,
                parent_task_id=rec.parent_task_id,
                payload=payload,
            )
        )
        # Memory store: best-effort outbox (Postgres path may already have written
        # outbox in the same transition txn via store hook).
        if self._outbox_enabled and not getattr(self.store, "_txn_outbox", False):
            try:
                from .outbox_bridge import emit_outbox_standalone

                emit_outbox_standalone(
                    event_id=ev.event_id,
                    event_type=ev.event_type,
                    root_task_id=ev.root_task_id,
                    correlation_id=ev.correlation_id,
                    task_id=ev.task_id,
                    agent_id=ev.agent_id,
                    sequence=(ev.payload or {}).get("sequence"),
                    payload=dict(ev.payload or {}),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("outbox emit skipped: %s", exc)


__all__ = ["ExecutionService", "IllegalTransition", "DELEGATION_FAILED", "DELEGATION_RELEASED"]
