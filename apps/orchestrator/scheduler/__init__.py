"""Scheduler: persist DAG nodes/deps and mark READY roots."""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db import connect
from planner.dag import PlanNode, TaskPlan, ready_node_ids, validate_plan
from approval import apply_decision, resolve_for_node

# Request tracking will be imported lazily in methods to avoid circular dependency
# Outbox pattern for P36.2 distributed transactions (lazy import)

__all__ = [
    "DEFAULT_TENANT_ID",
    "Scheduler",
    "ScheduleResult",
    "is_cancel_error",
    "is_permanent_agent_error",
]

_NODE_ACTIVE = frozenset(
    {"pending", "ready", "running", "retrying", "waiting_for_user", "waiting_for_agent"}
)
_NODE_OK = frozenset({"success"})
_NODE_BAD = frozenset({"failed", "cancelled"})

# Missing CLI / SDK / API key — retrying will not help.
_PERMANENT_ERROR_MARKERS = (
    "余额不足",
    "insufficient balance",
    "insufficient_quota",
    "payment required",
    "unavailable",
    "not found",
    "pi cli not found",
    "pi_cli_path",
    "deepseek harness unavailable",
    "deepseek_api_key",
    "missing_credential",
    "no api key",
    "needs deepseek_api_key",
    "install deepseek",
    "install @earendil",
    "set deepseek_api_key",
    "set pi_cli_path",
    "no such file",
    "command not found",
    "dsh_home",
)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def is_permanent_agent_error(error: str | None) -> bool:
    """True when the agent/runtime error will not succeed on retry."""
    low = (error or "").lower()
    return any(m in low for m in _PERMANENT_ERROR_MARKERS)

def is_cancel_error(error: str | None) -> bool:
    """True when the failure is a user/OS cancel — must not be retried."""
    low = (error or "").lower()
    return "canceled" in low or "cancelled" in low

@dataclass
class ScheduleResult:
    task_id: str
    status: str
    ready_nodes: list[str]
    nodes: list[dict[str, Any]]
    ready_jobs: list[dict[str, Any]] = field(default_factory=list)

class Scheduler:
    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id
        self._request_tracking_service = None  # Lazy-loaded
        self._outbox_processor = None  # Lazy-loaded for P36.2

    def _connect(self):
        """Open a DB connection via the shared resilient helper (db.connect)."""
        return connect(self.database_url)

    def create_task(
        self,
        *,
        goal: str,
        plan: TaskPlan,
        user_id: str | None = None,
        input_extra: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        plan_source: str = "planner",
        tenant_id: str | None = None,
    ) -> ScheduleResult:
        validate_plan(plan)
        tid = tenant_id or self.tenant_id

        input_json = {"type": "text", "content": goal}
        if input_extra:
            input_json.update(input_extra)

        with self._connect() as conn:
            with conn.transaction():
                task_id = str(uuid.uuid4())
                now = _utc_now()
                conn.execute(
                    """
                    INSERT INTO tasks (
                      id, tenant_id, user_id, title, input_json, status, progress,
                      plan_json, workflow_id, created_at, updated_at, started_at
                    ) VALUES (
                      %s::uuid, %s::uuid, %s, %s, %s::jsonb, 'planning', 0,
                      %s::jsonb, %s::uuid, %s, %s, %s
                    )
                    """,
                    (
                        task_id,
                        tid,
                        user_id,
                        plan.title,
                        Jsonb(input_json),
                        Jsonb(plan.to_dict()),
                        workflow_id,
                        now,
                        now,
                        now,
                    ),
                )
                self._append_event(
                    conn,
                    task_id,
                    None,
                    "task.created",
                    "Task Created",
                    {"goal": goal, "workflow_id": workflow_id, "plan_source": plan_source},
                )
                planned_msg = (
                    f"Workflow applied with {len(plan.nodes)} nodes"
                    if plan_source == "workflow"
                    else f"Planner decomposed into {len(plan.nodes)} nodes"
                )
                self._append_event(
                    conn,
                    task_id,
                    None,
                    "task.planned",
                    planned_msg,
                    plan.to_dict(),
                )

                node_rows: dict[str, str] = {}  # node_key -> uuid
                agent_by_key = {n.id: n.agent_key for n in plan.nodes}
                for node in plan.nodes:
                    node_uuid = str(uuid.uuid4())
                    node_rows[node.id] = node_uuid
                    # P36.1: Generate stable idempotency key for this node
                    node_idempotency_key = f"req_{task_id[:8]}_{node.id}"
                    
                    conn.execute(
                        """
                        INSERT INTO task_nodes (
                          id, task_id, node_key, agent_key, status, input_json,
                          attempt, max_retry, idempotency_key, created_at, updated_at
                        ) VALUES (
                          %s::uuid, %s::uuid, %s, %s, 'pending', %s::jsonb,
                          0, 3, %s, %s, %s
                        )
                        """,
                        (
                            node_uuid,
                            task_id,
                            node.id,
                            node.agent_key,
                            Jsonb(
                                {
                                    "goal": goal,
                                    "node_id": node.id,
                                    "instruction": (
                                        (node.instruction or "").strip() or goal
                                    ),
                                    "agent_key": node.agent_key,
                                }
                            ),
                            node_idempotency_key,
                            now,
                            now,
                        ),
                    )

                for node in plan.nodes:
                    for dep in node.depends_on:
                        conn.execute(
                            """
                            INSERT INTO task_dependencies (task_id, node_id, depends_on_node_id)
                            VALUES (%s::uuid, %s::uuid, %s::uuid)
                            """,
                            (task_id, node_rows[node.id], node_rows[dep]),
                        )

                ready_keys = ready_node_ids(
                    plan.nodes,
                    completed=set(),
                    pending={n.id for n in plan.nodes},
                )
                for key in ready_keys:
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'ready', updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (now, node_rows[key]),
                    )
                    self._append_event(
                        conn,
                        task_id,
                        node_rows[key],
                        "task.node.ready",
                        f"Node {key} ready",
                        {"node_key": key, "agent_key": agent_by_key[key]},
                    )

                status = "running" if ready_keys else "planning"
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = %s, progress = %s, updated_at = %s
                    WHERE id = %s::uuid
                    """,
                    (status, 5 if ready_keys else 0, now, task_id),
                )
                self._append_event(
                    conn,
                    task_id,
                    None,
                    "task.running" if ready_keys else "task.planned",
                    f"Ready nodes: {', '.join(ready_keys) or '(none)'}",
                    {"ready_nodes": ready_keys},
                )

                nodes = self._list_nodes(conn, task_id)
                ready_jobs = [
                    {
                        "task_id": task_id,
                        "node_id": node_rows[key],
                        "node_key": key,
                        "agent_key": agent_by_key[key],
                        "attempt": 1,
                    }
                    for key in ready_keys
                ]

        return ScheduleResult(
            task_id=task_id,
            status=status,
            ready_nodes=ready_keys,
            nodes=nodes,
            ready_jobs=ready_jobs,
        )

    def get_node(self, task_id: str, node_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id::text AS node_id, node_key, agent_key, status, attempt, max_retry,
                       assigned_agent_id::text AS agent_id, input_json, output_json,
                       error_message
                FROM task_nodes
                WHERE task_id = %s::uuid AND node_key = %s
                """,
                (task_id, node_key),
            ).fetchone()
            return dict(row) if row else None

    def list_a2a_cancel_targets(self, task_id: str) -> list[dict[str, Any]]:
        """Nodes that may still hold in-flight harness work (for A2A tasks/cancel)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT node_key, status,
                       assigned_agent_id::text AS agent_id,
                       output_json
                FROM task_nodes
                WHERE task_id = %s::uuid
                  AND status IN ('running', 'ready', 'retrying', 'waiting_for_user')
                  AND assigned_agent_id IS NOT NULL
                ORDER BY updated_at ASC NULLS LAST, node_key ASC
                """,
                (task_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            output = d.get("output_json") or {}
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except Exception:  # noqa: BLE001
                    output = {}
            a2a_id = None
            if isinstance(output, dict):
                a2a_id = output.get("a2a_task_id") or output.get("a2aTaskId")
            out.append(
                {
                    "node_key": d.get("node_key"),
                    "status": d.get("status"),
                    "agent_id": d.get("agent_id"),
                    "a2a_task_id": a2a_id,
                }
            )
        return out

    def get_task_goal(self, task_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM tasks WHERE id = %s::uuid",
                (task_id,),
            ).fetchone()
            if not row:
                return ""
            data = row["input_json"] or {}
            if isinstance(data, str):
                data = json.loads(data)
            return str(data.get("content") or "")

    def reclaim_stale_nodes(self, *, stale_seconds: float = 900) -> list[dict[str, Any]]:
        """Mark long-running nodes as retrying and return re-enqueue jobs (长任务回收)."""
        stale_seconds = max(60.0, float(stale_seconds))
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                rows = conn.execute(
                    """
                    SELECT tn.id::text AS node_id, tn.task_id::text AS task_id, tn.node_key, tn.agent_key,
                           tn.attempt, tn.max_retry, tn.assigned_agent_id::text AS agent_id
                    FROM task_nodes tn
                    JOIN tasks t ON t.id = tn.task_id
                    WHERE tn.status = 'running'
                      AND t.status NOT IN ('cancelled', 'completed', 'failed')
                      AND COALESCE(tn.started_at, tn.updated_at) < (%s - (%s || ' seconds')::interval)
                    ORDER BY tn.updated_at ASC
                    LIMIT 20
                    """,
                    (now, str(int(stale_seconds))),
                ).fetchall()
                jobs: list[dict[str, Any]] = []
                for row in rows:
                    attempt = int(row["attempt"] or 1)
                    max_retry = int(row["max_retry"] or 3)
                    next_attempt = attempt + 1
                    if next_attempt > max_retry:
                        conn.execute(
                            """
                            UPDATE task_nodes
                            SET status = 'failed',
                                error_message = %s,
                                finished_at = %s,
                                updated_at = %s
                            WHERE id = %s::uuid
                            """,
                            (
                                f"stale running > {int(stale_seconds)}s",
                                now,
                                now,
                                row["node_id"],
                            ),
                        )
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status = 'failed',
                                error_message = %s,
                                updated_at = %s,
                                finished_at = %s
                            WHERE id = %s::uuid
                            """,
                            (
                                f"node {row['node_key']} stale timeout",
                                now,
                                now,
                                row["task_id"],
                            ),
                        )
                        self._append_event(
                            conn,
                            row["task_id"],
                            row["node_id"],
                            "task.node.stale_failed",
                            f"Node {row['node_key']} failed after stale timeout",
                            {"stale_seconds": stale_seconds},
                        )
                        continue

                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'retrying',
                            attempt = %s,
                            error_message = %s,
                            updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (
                            attempt,
                            f"reclaimed stale running > {int(stale_seconds)}s",
                            now,
                            row["node_id"],
                        ),
                    )
                    self._append_event(
                        conn,
                        row["task_id"],
                        row["node_id"],
                        "task.node.stale_reclaimed",
                        f"Node {row['node_key']} reclaimed after {int(stale_seconds)}s",
                        {"next_attempt": next_attempt, "stale_seconds": stale_seconds},
                    )
                    jobs.append(
                        {
                            "task_id": row["task_id"],
                            "node_id": row["node_id"],
                            "node_key": row["node_key"],
                            "agent_key": row["agent_key"],
                            "attempt": next_attempt,
                            "exclude_agent_ids": [row["agent_id"]] if row.get("agent_id") else [],
                        }
                    )
                return jobs

    def requeue_incomplete(self, task_id: str) -> list[dict[str, Any]]:
        """Operator recover: re-enqueue ready/retrying work; unlock pending; reclaim running.

        Does not resurrect cancelled/completed tasks. Failed tasks are set back to
        running and their failed nodes become retrying.
        """
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                row = conn.execute(
                    """
                    SELECT id::text AS task_id, status
                    FROM tasks WHERE id = %s::uuid FOR UPDATE
                    """,
                    (task_id,),
                ).fetchone()
                if not row:
                    return []
                status = row["status"]
                if status in ("cancelled", "completed"):
                    return []
                if status == "failed":
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'running',
                            error_message = NULL,
                            finished_at = NULL,
                            updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (now, task_id),
                    )
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'retrying',
                            error_message = NULL,
                            finished_at = NULL,
                            updated_at = %s
                        WHERE task_id = %s::uuid AND status = 'failed'
                        """,
                        (now, task_id),
                    )
                    self._append_event(
                        conn,
                        task_id,
                        None,
                        "task.requeued",
                        "failed task requeued by operator recover",
                        {},
                    )

                jobs: list[dict[str, Any]] = []
                seen: set[str] = set()
                try:
                    plan = self._load_plan(conn, task_id)
                except Exception:  # noqa: BLE001
                    plan = None
                if plan is not None:
                    for job in self._unlock_dependents(conn, task_id, plan, now):
                        jobs.append(job)
                        seen.add(job["node_key"])

                running = conn.execute(
                    """
                    SELECT id::text AS node_id, node_key, agent_key, attempt,
                           assigned_agent_id::text AS agent_id
                    FROM task_nodes
                    WHERE task_id = %s::uuid AND status = 'running'
                    FOR UPDATE
                    """,
                    (task_id,),
                ).fetchall()
                for n in running:
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'retrying',
                            error_message = COALESCE(error_message, 'operator recover'),
                            updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (now, n["node_id"]),
                    )
                    self._append_event(
                        conn,
                        task_id,
                        n["node_id"],
                        "task.node.requeued",
                        f"Node {n['node_key']} requeued by operator recover",
                        {"from": "running"},
                    )
                    jobs.append(
                        {
                            "task_id": task_id,
                            "node_id": n["node_id"],
                            "node_key": n["node_key"],
                            "agent_key": n["agent_key"],
                            "attempt": int(n["attempt"] or 1),
                            "exclude_agent_ids": [n["agent_id"]] if n.get("agent_id") else [],
                        }
                    )
                    seen.add(n["node_key"])

                waiting = conn.execute(
                    """
                    SELECT id::text AS node_id, node_key, agent_key, attempt
                    FROM task_nodes
                    WHERE task_id = %s::uuid AND status IN ('ready', 'retrying')
                    """,
                    (task_id,),
                ).fetchall()
                for n in waiting:
                    if n["node_key"] in seen:
                        continue
                    jobs.append(
                        {
                            "task_id": task_id,
                            "node_id": n["node_id"],
                            "node_key": n["node_key"],
                            "agent_key": n["agent_key"],
                            "attempt": int(n["attempt"] or 1),
                        }
                    )
                    seen.add(n["node_key"])
                return jobs

    def claim_running(self, task_id: str, node_key: str, agent_id: str, attempt: int) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                cur = conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'running',
                        assigned_agent_id = %s::uuid,
                        attempt = %s,
                        started_at = %s,
                        updated_at = %s,
                        error_message = NULL
                    WHERE task_id = %s::uuid
                      AND node_key = %s
                      AND status IN ('ready', 'retrying')
                      AND EXISTS (
                        SELECT 1 FROM tasks t
                        WHERE t.id = task_nodes.task_id
                          AND t.status NOT IN ('cancelled', 'completed', 'failed')
                      )
                    RETURNING id::text
                    """,
                    (agent_id, attempt, now, now, task_id, node_key),
                )
                row = cur.fetchone()
                if not row:
                    return False
                self._append_event(
                    conn,
                    task_id,
                    row["id"],
                    "agent.task.started",
                    f"Node {node_key} running on agent {agent_id}",
                    {"node_key": node_key, "agent_id": agent_id, "attempt": attempt},
                )
                conn.execute(
                    """
                    UPDATE tasks SET status = 'running',
                        progress = GREATEST(COALESCE(progress, 0), %s),
                        updated_at = %s
                    WHERE id = %s::uuid AND status IN ('ready', 'planning', 'running')
                    """,
                    (15 if attempt >= 1 else 5, now, task_id),
                )
                return True

    def mark_success(
        self,
        task_id: str,
        node_key: str,
        *,
        output: dict[str, Any],
        a2a_task_id: str | None = None,
        latency_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Mark node success (or HITL wait), unlock dependents. Returns newly ready jobs."""
        with self._connect() as conn:
            with conn.transaction():
                plan = self._load_plan(conn, task_id)
                now = _utc_now()
                plan_node = next((n for n in plan.nodes if n.id == node_key), None)
                policy = resolve_for_node(plan_node=plan_node, output=output)
                needs_hitl = policy.needs_gate

                # P36.2: Write task event to outbox
                self.write_outbox_event(
                    "task_event",
                    {
                        "task_id": task_id,
                        "node_key": node_key,
                        "event_type": "node_success",
                        "output": output,
                        "a2a_task_id": a2a_task_id,
                        "latency_ms": latency_ms,
                    },
                    "task_events"
                )
                target_status = policy.wait_status() if needs_hitl else "success"
                stored_output = dict(output or {})
                if needs_hitl:
                    stored_output["hitl"] = {
                        **(stored_output.get("hitl") if isinstance(stored_output.get("hitl"), dict) else {}),
                        **policy.to_hitl(),
                    }

                node = conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = %s,
                        output_json = %s::jsonb,
                        finished_at = %s,
                        updated_at = %s
                    WHERE task_id = %s::uuid AND node_key = %s
                      AND status IN ('ready', 'running', 'retrying')
                    RETURNING id::text AS node_id, agent_key, assigned_agent_id::text AS agent_id,
                              attempt, input_json
                    """,
                    (target_status, Jsonb(stored_output), now, now, task_id, node_key),
                ).fetchone()
                if not node:
                    return []

                self._save_checkpoint(
                    conn,
                    task_id=task_id,
                    node_id=node["node_id"],
                    node_key=node_key,
                    attempt=int(node.get("attempt") or 1),
                    status=target_status,
                    snapshot=self._build_checkpoint_snapshot(
                        status=target_status,
                        attempt=int(node.get("attempt") or 1),
                        agent_key=node.get("agent_key"),
                        output=output,
                        input_json=node.get("input_json"),
                    ),
                )

                self._append_event(
                    conn,
                    task_id,
                    node["node_id"],
                    "task.node.completed" if not needs_hitl else "task.node.awaiting_approval",
                    (
                        f"Node {node_key} completed"
                        if not needs_hitl
                        else f"Node {node_key} awaiting {policy.mode} approval"
                    ),
                    {
                        "node_key": node_key,
                        "a2a_task_id": a2a_task_id,
                        "latency_ms": latency_ms,
                        "approval_mode": policy.mode,
                        "approver_agent": policy.approver_agent,
                    },
                )

                # agent_runs audit (HITL still counts as successful agent execution)
                if node.get("agent_id"):
                    conn.execute(
                        """
                        INSERT INTO agent_runs (
                          tenant_id, task_id, node_id, agent_id, a2a_task_id, status,
                          response_json, latency_ms, created_at, finished_at
                        ) VALUES (
                          %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, 'success',
                          %s::jsonb, %s, %s, %s
                        )
                        """,
                        (
                            self.tenant_id,
                            task_id,
                            node["node_id"],
                            node["agent_id"],
                            a2a_task_id,
                            Jsonb(output),
                            latency_ms,
                            now,
                            now,
                        ),
                    )

                if needs_hitl:
                    rows = self._list_nodes(conn, task_id)
                    completed = {r["id"] for r in rows if r["status"] == "success"}
                    total = len(rows) or 1
                    progress = int(len(completed) * 100 / total)
                    task_status = policy.wait_status()
                    conn.execute(
                        """
                        UPDATE tasks SET status = %s, progress = %s, updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (task_status, progress, now, task_id),
                    )
                    if policy.needs_agent and policy.approver_agent:
                        self._signal_agent_approval(
                            conn,
                            task_id=task_id,
                            node_id=node["node_id"],
                            node_key=node_key,
                            policy=policy,
                            output=stored_output,
                        )
                    self._append_event(
                        conn,
                        task_id,
                        node["node_id"],
                        "task.waiting_for_user"
                        if task_status == "waiting_for_user"
                        else "task.waiting_for_agent",
                        f"Task paused for {policy.mode} approval at {node_key}",
                        {
                            "node_key": node_key,
                            "agent_key": node.get("agent_key"),
                            "approval_mode": policy.mode,
                            "approver_agent": policy.approver_agent,
                        },
                    )
                    return []

                return self._unlock_dependents(conn, task_id, plan, now)

    def mark_failure(
        self,
        task_id: str,
        node_key: str,
        *,
        error: str,
        attempt: int,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Return decision: retry | failed | cancelled, plus next_attempt / max_retry."""
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                row = conn.execute(
                    """
                    SELECT tn.id::text AS node_id, tn.max_retry, tn.agent_key, tn.attempt,
                           tn.input_json, tn.output_json, tn.status AS node_status,
                           t.status AS task_status
                    FROM task_nodes tn
                    JOIN tasks t ON t.id = tn.task_id
                    WHERE tn.task_id = %s::uuid AND tn.node_key = %s
                    """,
                    (task_id, node_key),
                ).fetchone()
                if not row:
                    return {"decision": "failed", "attempt": attempt, "max_retry": 0}

                max_retry = int(row["max_retry"] or 3)
                node_status = str(row.get("node_status") or "")
                task_status = str(row.get("task_status") or "")
                cancel_end = (
                    task_status == "cancelled"
                    or node_status == "cancelled"
                    or is_cancel_error(error)
                )
                if cancel_end:
                    # User/OS cancel (or late A2A canceled callback): never retry.
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'cancelled',
                            attempt = %s,
                            error_message = COALESCE(%s, error_message, 'cancelled'),
                            finished_at = COALESCE(finished_at, %s),
                            updated_at = %s
                        WHERE id = %s::uuid
                          AND status NOT IN ('success', 'cancelled')
                        """,
                        (attempt, error or "cancelled", now, now, row["node_id"]),
                    )
                    if task_status != "cancelled":
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status = 'cancelled',
                                updated_at = %s,
                                finished_at = COALESCE(finished_at, %s),
                                error_message = COALESCE(error_message, %s)
                            WHERE id = %s::uuid
                              AND status NOT IN ('completed', 'cancelled')
                            """,
                            (now, now, error or "cancelled by user", task_id),
                        )
                    self._append_event(
                        conn,
                        task_id,
                        row["node_id"],
                        "task.node.cancelled",
                        f"Node {node_key} cancelled (no retry): {error}",
                        {"attempt": attempt, "agent_id": agent_id, "error": error},
                    )
                    return {
                        "decision": "cancelled",
                        "attempt": attempt,
                        "max_retry": max_retry,
                        "node_id": row["node_id"],
                        "task_status": "cancelled",
                    }

                # P36.2: Write task event to outbox
                self.write_outbox_event(
                    "task_event",
                    {
                        "task_id": task_id,
                        "node_key": node_key,
                        "event_type": "node_failure",
                        "error": error,
                        "attempt": attempt,
                        "agent_id": agent_id,
                    },
                    "task_events"
                )

                # Permanent env/CLI gaps: skip remaining retries.
                if is_permanent_agent_error(error):
                    attempt = max(int(attempt or 1), max_retry)

                fail_status = "retrying" if attempt < max_retry else "failed"
                out = row.get("output_json")
                if isinstance(out, str):
                    try:
                        out = json.loads(out)
                    except Exception:
                        out = {}
                if not isinstance(out, dict):
                    out = {}
                self._save_checkpoint(
                    conn,
                    task_id=task_id,
                    node_id=row["node_id"],
                    node_key=node_key,
                    attempt=int(attempt or 1),
                    status=fail_status,
                    snapshot=self._build_checkpoint_snapshot(
                        status=fail_status,
                        attempt=int(attempt or 1),
                        agent_key=row.get("agent_key"),
                        output=out or None,
                        error=error,
                        input_json=row.get("input_json"),
                    ),
                )
                
                if attempt < max_retry:
                    conn.execute(
                        """
                        UPDATE task_nodes
                        SET status = 'retrying',
                            attempt = %s,
                            error_message = %s,
                            updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (attempt, error, now, row["node_id"]),
                    )
                    self._append_event(
                        conn,
                        task_id,
                        row["node_id"],
                        "task.node.retrying",
                        f"Node {node_key} retry {attempt}/{max_retry}: {error}",
                        {"attempt": attempt, "max_retry": max_retry, "agent_id": agent_id},
                    )
                    return {
                        "decision": "retry",
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "max_retry": max_retry,
                        "node_id": row["node_id"],
                    }

                conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'failed',
                        attempt = %s,
                        error_message = %s,
                        finished_at = %s,
                        updated_at = %s
                    WHERE id = %s::uuid
                    """,
                    (attempt, error, now, now, row["node_id"]),
                )
                self._append_event(
                    conn,
                    task_id,
                    row["node_id"],
                    "task.node.failed",
                    f"Node {node_key} failed: {error}",
                    {"attempt": attempt, "agent_id": agent_id},
                )
                if agent_id:
                    conn.execute(
                        """
                        INSERT INTO agent_runs (
                          tenant_id, task_id, node_id, agent_id, status,
                          error_message, created_at, finished_at
                        ) VALUES (
                          %s::uuid, %s::uuid, %s::uuid, %s::uuid, 'failed',
                          %s, %s, %s
                        )
                        """,
                        (
                            self.tenant_id,
                            task_id,
                            row["node_id"],
                            agent_id,
                            error,
                            now,
                            now,
                        ),
                    )

                plan = self._load_plan(conn, task_id)
                self._cancel_blocked_dependents(
                    conn, task_id, failed_key=node_key, plan=plan, now=now
                )
                settled = self._settle_task_after_node(
                    conn,
                    task_id,
                    now,
                    trigger_node=node_key,
                    trigger_error=error,
                )
                return {
                    "decision": "failed",
                    "attempt": attempt,
                    "max_retry": max_retry,
                    "node_id": row["node_id"],
                    "task_status": settled,
                }

    def approve_node(
        self,
        task_id: str,
        node_key: str | None = None,
        *,
        human_input: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Approve a waiting node. ``actor`` is system (Inbox) or agent (peer).

        Optional ``human_input`` is merged into ``output_json.hitl`` so
        downstream ``_compose_query`` can resume with that content.
        """
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                if node_key:
                    node = conn.execute(
                        """
                        SELECT id::text AS node_id, node_key, agent_key, status, output_json
                        FROM task_nodes
                        WHERE task_id = %s::uuid AND node_key = %s
                        """,
                        (task_id, node_key),
                    ).fetchone()
                else:
                    node = conn.execute(
                        """
                        SELECT id::text AS node_id, node_key, agent_key, status, output_json
                        FROM task_nodes
                        WHERE task_id = %s::uuid
                          AND status IN ('waiting_for_user', 'waiting_for_agent')
                        ORDER BY updated_at ASC
                        LIMIT 1
                        """,
                        (task_id,),
                    ).fetchone()
                if not node:
                    raise ValueError("no waiting node to approve")
                if node["status"] not in {"waiting_for_user", "waiting_for_agent"}:
                    raise ValueError(f"node status={node['status']} is not waiting")

                plan = self._load_plan(conn, task_id)
                plan_node = next((n for n in plan.nodes if n.id == node["node_key"]), None)
                raw_out = node.get("output_json")
                if isinstance(raw_out, str):
                    try:
                        raw_out = json.loads(raw_out)
                    except Exception:
                        raw_out = {}
                if not isinstance(raw_out, dict):
                    raw_out = {}
                policy = apply_decision(
                    resolve_for_node(plan_node=plan_node, output=raw_out),
                    actor=actor,
                    approved=True,
                )
                text = (human_input or "").strip() or None
                hitl = {
                    **(raw_out.get("hitl") if isinstance(raw_out.get("hitl"), dict) else {}),
                    **policy.to_hitl(),
                    "approved": not policy.needs_gate,
                    "input": text,
                    "approved_at": now.isoformat(),
                    "last_actor": "agent" if str(actor).lower() in {"agent", "peer", "a2a"} else "system",
                }
                merged = {**raw_out, "hitl": hitl}
                next_status = "success" if not policy.needs_gate else policy.wait_status()
                conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = %s,
                        updated_at = %s,
                        finished_at = COALESCE(finished_at, %s),
                        output_json = %s::jsonb
                    WHERE id = %s::uuid
                    """,
                    (next_status, now, now, Jsonb(merged), node["node_id"]),
                )
                self._save_checkpoint(
                    conn,
                    task_id=task_id,
                    node_id=node["node_id"],
                    node_key=node["node_key"],
                    attempt=1,
                    status=next_status,
                    snapshot=self._build_checkpoint_snapshot(
                        status=next_status,
                        attempt=1,
                        agent_key=node.get("agent_key"),
                        output=merged,
                    ),
                )
                self._append_event(
                    conn,
                    task_id,
                    node["node_id"],
                    "task.node.approved",
                    f"Node {node['node_key']} approved by {hitl.get('last_actor')}"
                    + (" with input" if text else ""),
                    {
                        "node_key": node["node_key"],
                        "actor": hitl.get("last_actor"),
                        "approval_mode": policy.mode,
                        "has_human_input": bool(text),
                        "human_input_chars": len(text) if text else 0,
                        "still_waiting": policy.needs_gate,
                    },
                )
                if policy.needs_gate:
                    conn.execute(
                        """
                        UPDATE tasks SET status = %s, updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        (next_status, now, task_id),
                    )
                    return {
                        "task_id": task_id,
                        "node_key": node["node_key"],
                        "approved": True,
                        "has_human_input": bool(text),
                        "ready_jobs": [],
                        "status": next_status,
                        "approval": policy.to_hitl(),
                    }
                ready_jobs = self._unlock_dependents(conn, task_id, plan, now)
                return {
                    "task_id": task_id,
                    "node_key": node["node_key"],
                    "approved": True,
                    "has_human_input": bool(text),
                    "ready_jobs": ready_jobs,
                    "status": self._task_status(conn, task_id),
                    "approval": policy.to_hitl(),
                }

    def reject_node(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        reason: str = "rejected by user",
    ) -> dict[str, Any]:
        """Reject a waiting_for_user node and fail the task."""
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                if node_key:
                    node = conn.execute(
                        """
                        SELECT id::text AS node_id, node_key, status
                        FROM task_nodes
                        WHERE task_id = %s::uuid AND node_key = %s
                        """,
                        (task_id, node_key),
                    ).fetchone()
                else:
                    node = conn.execute(
                        """
                        SELECT id::text AS node_id, node_key, status
                        FROM task_nodes
                        WHERE task_id = %s::uuid
                          AND status IN ('waiting_for_user', 'waiting_for_agent')
                        ORDER BY updated_at ASC
                        LIMIT 1
                        """,
                        (task_id,),
                    ).fetchone()
                if not node:
                    raise ValueError("no waiting node to reject")
                if node["status"] not in {"waiting_for_user", "waiting_for_agent"}:
                    raise ValueError(f"node status={node['status']} is not waiting")

                conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'failed', error_message = %s,
                        finished_at = %s, updated_at = %s
                    WHERE id = %s::uuid
                    """,
                    (reason, now, now, node["node_id"]),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed', error_message = %s,
                        updated_at = %s, finished_at = %s
                    WHERE id = %s::uuid
                    """,
                    (f"node {node['node_key']} rejected: {reason}", now, now, task_id),
                )
                self._append_event(
                    conn,
                    task_id,
                    node["node_id"],
                    "task.node.rejected",
                    f"Node {node['node_key']} rejected",
                    {"node_key": node["node_key"], "reason": reason},
                )
                self._append_event(
                    conn,
                    task_id,
                    None,
                    "task.failed",
                    "Task failed after HITL reject",
                    {"node_key": node["node_key"]},
                )
                return {
                    "task_id": task_id,
                    "node_key": node["node_key"],
                    "rejected": True,
                    "status": "failed",
                }

    def list_checkpoints(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List append-only node checkpoints for a task (newest first)."""
        limit = max(1, min(int(limit or 50), 200))
        with self._connect() as conn:
            try:
                if node_key:
                    rows = conn.execute(
                        """
                        SELECT id, task_id::text AS task_id, node_id::text AS node_id,
                               node_key, attempt, status, snapshot_json, created_at
                        FROM task_node_checkpoints
                        WHERE task_id = %s::uuid AND node_key = %s
                        ORDER BY created_at DESC, id DESC
                        LIMIT %s
                        """,
                        (task_id, node_key, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, task_id::text AS task_id, node_id::text AS node_id,
                               node_key, attempt, status, snapshot_json, created_at
                        FROM task_node_checkpoints
                        WHERE task_id = %s::uuid
                        ORDER BY created_at DESC, id DESC
                        LIMIT %s
                        """,
                        (task_id, limit),
                    ).fetchall()
            except Exception:
                return []
            out = []
            for r in rows:
                d = dict(r)
                if hasattr(d.get("created_at"), "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
                out.append(d)
            return out

    def replay_from_node(
        self,
        task_id: str,
        node_key: str,
        *,
        clear_downstream: bool = True,
    ) -> dict[str, Any]:
        """Re-enqueue a plan node from its last checkpoint (retry_same).

        Sets the node to ``retrying``, optionally resets transitive dependents
        to ``pending``, and returns a ready job for the worker queue.
        """
        with self._connect() as conn:
            with conn.transaction():
                now = _utc_now()
                node = conn.execute(
                    """
                    SELECT id::text AS node_id, node_key, agent_key, status, attempt,
                           max_retry, checkpoint_json, checkpoint_attempt
                    FROM task_nodes
                    WHERE task_id = %s::uuid AND node_key = %s
                    FOR UPDATE
                    """,
                    (task_id, node_key),
                ).fetchone()
                if not node:
                    raise ValueError(f"node {node_key!r} not found")
                st = node["status"]
                if st not in ("failed", "retrying", "success", "cancelled", "waiting_for_user"):
                    raise ValueError(
                        f"node status={st} cannot replay (need failed/retrying/success/…)"
                    )

                next_attempt = int(node.get("attempt") or 0) + 1
                max_retry = int(node.get("max_retry") or 3)
                # Allow at least one forced replay even if retries exhausted
                if next_attempt > max_retry:
                    conn.execute(
                        """
                        UPDATE task_nodes SET max_retry = %s WHERE id = %s::uuid
                        """,
                        (next_attempt, node["node_id"]),
                    )

                cleared: list[str] = []
                if clear_downstream:
                    plan = self._load_plan(conn, task_id)
                    # transitive dependents of node_key
                    deps_of: dict[str, list[str]] = {
                        n.id: list(n.depends_on or []) for n in plan.nodes
                    }
                    frontier = {node_key}
                    affected: set[str] = set()
                    changed = True
                    while changed:
                        changed = False
                        for nid, deps in deps_of.items():
                            if nid in affected or nid == node_key:
                                continue
                            if any(d in frontier for d in deps):
                                affected.add(nid)
                                frontier.add(nid)
                                changed = True
                    for dep_key in sorted(affected):
                        upd = conn.execute(
                            """
                            UPDATE task_nodes
                            SET status = 'pending',
                                error_message = NULL,
                                finished_at = NULL,
                                attempt = 0,
                                updated_at = %s
                            WHERE task_id = %s::uuid AND node_key = %s
                              AND status IN ('pending','ready','success','failed',
                                             'retrying','waiting_for_user','running',
                                             'cancelled')
                            RETURNING node_key
                            """,
                            (now, task_id, dep_key),
                        ).fetchone()
                        if upd:
                            cleared.append(dep_key)

                conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'retrying',
                        attempt = %s,
                        error_message = NULL,
                        finished_at = NULL,
                        updated_at = %s
                    WHERE id = %s::uuid
                    """,
                    (next_attempt, now, node["node_id"]),
                )
                # Task must be runnable again
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'running',
                        error_message = NULL,
                        finished_at = NULL,
                        updated_at = %s
                    WHERE id = %s::uuid
                      AND status IN ('failed', 'cancelled', 'completed',
                                     'waiting_for_user', 'running', 'ready')
                    """,
                    (now, task_id),
                )
                self._append_event(
                    conn,
                    task_id,
                    node["node_id"],
                    "task.node.replayed",
                    f"Node {node_key} replayed from checkpoint attempt="
                    f"{node.get('checkpoint_attempt') or node.get('attempt')}",
                    {
                        "node_key": node_key,
                        "next_attempt": next_attempt,
                        "cleared_downstream": cleared,
                        "checkpoint_attempt": node.get("checkpoint_attempt"),
                    },
                )
                job = {
                    "task_id": task_id,
                    "node_id": node["node_id"],
                    "node_key": node_key,
                    "agent_key": node["agent_key"],
                    "attempt": next_attempt,
                }
                return {
                    "task_id": task_id,
                    "node_key": node_key,
                    "replayed": True,
                    "next_attempt": next_attempt,
                    "cleared_downstream": cleared,
                    "checkpoint": node.get("checkpoint_json"),
                    "ready_jobs": [job],
                    "status": "running",
                }

    def _signal_agent_approval(
        self,
        conn: Any,
        *,
        task_id: str,
        node_id: str,
        node_key: str,
        policy: Any,
        output: dict[str, Any],
    ) -> None:
        """Tell the named peer agent a hop is waiting on its approval."""
        key = (policy.approver_agent or "").strip()
        if not key:
            return
        row = None
        try:
            row = conn.execute(
                """
                SELECT id::text AS agent_id, agent_key, endpoint, name
                FROM agents
                WHERE tenant_id = %s::uuid AND agent_key = %s
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                (self.tenant_id, key),
            ).fetchone()
        except Exception:  # noqa: BLE001
            try:
                row = conn.execute(
                    """
                    SELECT id::text AS agent_id, agent_key, name
                    FROM agents
                    WHERE agent_key = %s
                    LIMIT 1
                    """,
                    (key,),
                ).fetchone()
            except Exception:  # noqa: BLE001
                row = None
        endpoint = (row or {}).get("endpoint") if row else None
        payload = {
            "type": "approval_request",
            "task_id": task_id,
            "node_key": node_key,
            "approval_mode": policy.mode,
            "approver_agent": key,
            "reason": policy.reason,
            "hitl": policy.to_hitl(),
        }
        self._append_event(
            conn,
            task_id,
            node_id,
            "agent.approval.requested",
            f"Approval signal → {key}",
            {
                **payload,
                "approver_agent_id": (row or {}).get("agent_id"),
                "endpoint": endpoint,
            },
        )
        try:
            self.write_outbox_event(
                "agent_approval",
                payload,
                "agent_signals",
            )
        except Exception:  # noqa: BLE001
            pass
        if not endpoint:
            return
        try:
            import os

            import httpx

            url = str(endpoint).rstrip("/") + "/v1/approvals"
            headers = {"Content-Type": "application/json"}
            token = (os.getenv("AOP_COLLAB_TOKEN") or "").strip()
            if token:
                headers["X-AOP-Collab-Token"] = token
            httpx.post(url, json=payload, headers=headers, timeout=3.0)
        except Exception:  # noqa: BLE001
            pass

    def _unlock_dependents(
        self,
        conn: Any,
        task_id: str,
        plan: TaskPlan,
        now: datetime,
    ) -> list[dict[str, Any]]:
        rows = self._list_nodes(conn, task_id)
        completed = {r["id"] for r in rows if r["status"] == "success"}
        pending = {r["id"] for r in rows if r["status"] == "pending"}
        newly = ready_node_ids(plan.nodes, completed=completed, pending=pending)
        agent_by_key = {n.id: n.agent_key for n in plan.nodes}
        ready_jobs: list[dict[str, Any]] = []
        for key in newly:
            updated = conn.execute(
                """
                UPDATE task_nodes
                SET status = 'ready', updated_at = %s
                WHERE task_id = %s::uuid AND node_key = %s AND status = 'pending'
                RETURNING id::text AS node_id
                """,
                (now, task_id, key),
            ).fetchone()
            if not updated:
                continue
            self._append_event(
                conn,
                task_id,
                updated["node_id"],
                "task.node.ready",
                f"Node {key} ready",
                {"node_key": key, "agent_key": agent_by_key[key]},
            )
            ready_jobs.append(
                {
                    "task_id": task_id,
                    "node_id": updated["node_id"],
                    "node_key": key,
                    "agent_key": agent_by_key[key],
                    "attempt": 1,
                }
            )

        total = len(rows) or 1
        done = len(completed)
        progress = int(done * 100 / total)
        # Keep unlocking while peers run; final status comes from settlement
        # (partial success / all-failed), not "all nodes success".
        self._settle_task_after_node(conn, task_id, now)
        # Ensure progress reflects completed count while still running
        conn.execute(
            """
            UPDATE tasks SET progress = GREATEST(COALESCE(progress, 0), %s), updated_at = %s
            WHERE id = %s::uuid AND status = 'running'
            """,
            (progress, now, task_id),
        )
        return ready_jobs

    def _cancel_blocked_dependents(
        self,
        conn: Any,
        task_id: str,
        *,
        failed_key: str,
        plan: TaskPlan,
        now: datetime,
    ) -> list[str]:
        """Cancel pending nodes that can never unlock because a dependency failed."""
        blocked: set[str] = {failed_key}
        changed = True
        while changed:
            changed = False
            for n in plan.nodes:
                if n.id in blocked:
                    continue
                if any(d in blocked for d in (n.depends_on or [])):
                    blocked.add(n.id)
                    changed = True
        cancelled: list[str] = []
        for key in blocked:
            if key == failed_key:
                continue
            updated = conn.execute(
                """
                UPDATE task_nodes
                SET status = 'cancelled',
                    error_message = COALESCE(error_message, %s),
                    updated_at = %s,
                    finished_at = COALESCE(finished_at, %s)
                WHERE task_id = %s::uuid AND node_key = %s
                  AND status IN ('pending', 'ready', 'retrying')
                RETURNING id::text AS node_id
                """,
                (
                    f"blocked: dependency {failed_key} failed",
                    now,
                    now,
                    task_id,
                    key,
                ),
            ).fetchone()
            if updated:
                cancelled.append(key)
                self._append_event(
                    conn,
                    task_id,
                    updated["node_id"],
                    "task.node.cancelled",
                    f"Node {key} cancelled (blocked by {failed_key})",
                    {"node_key": key, "blocked_by": failed_key},
                )
        return cancelled

    def _settle_task_after_node(
        self,
        conn: Any,
        task_id: str,
        now: datetime,
        *,
        trigger_node: str | None = None,
        trigger_error: str | None = None,
    ) -> str:
        """Recompute task status from node states.

        Parallel peers: one failed node does **not** fail the whole task while
        siblings are still active. When all nodes are terminal:
        - ≥1 success → completed (partial if any failed)
        - all failed/cancelled → failed
        """
        current = conn.execute(
            "SELECT status FROM tasks WHERE id = %s::uuid FOR UPDATE",
            (task_id,),
        ).fetchone()
        cur_status = str(current["status"]) if current else "unknown"
        if cur_status == "cancelled":
            return "cancelled"

        rows = self._list_nodes(conn, task_id)
        if not rows:
            return cur_status

        active = [r for r in rows if r["status"] in _NODE_ACTIVE]
        ok = [r for r in rows if r["status"] in _NODE_OK]
        bad = [r for r in rows if r["status"] in _NODE_BAD]
        waiting = [r for r in rows if r["status"] in {"waiting_for_user", "waiting_for_agent"}]
        total = len(rows) or 1
        progress = int(len(ok) * 100 / total)

        if waiting and not any(
            r["status"] in {"pending", "ready", "running", "retrying"} for r in rows
        ):
            conn.execute(
                """
                UPDATE tasks SET status = 'waiting_for_user', progress = %s, updated_at = %s,
                  finished_at = NULL
                WHERE id = %s::uuid AND status <> 'cancelled'
                """,
                (progress, now, task_id),
            )
            return "waiting_for_user"

        if active and not waiting:
            # Peers still working — keep task running even if some nodes failed.
            conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    progress = %s,
                    updated_at = %s,
                    finished_at = NULL,
                    error_message = CASE
                      WHEN %s::text IS NOT NULL THEN COALESCE(error_message, %s)
                      ELSE error_message
                    END
                WHERE id = %s::uuid AND status <> 'cancelled'
                """,
                (
                    max(progress, 5),
                    now,
                    trigger_error,
                    (
                        f"node {trigger_node} failed: {trigger_error}"
                        if trigger_node and trigger_error
                        else None
                    ),
                    task_id,
                ),
            )
            return "running"

        if active:
            # mix of waiting + other active already handled; keep running
            conn.execute(
                """
                UPDATE tasks SET status = 'running', progress = %s, updated_at = %s,
                  finished_at = NULL
                WHERE id = %s::uuid AND status <> 'cancelled'
                """,
                (progress, now, task_id),
            )
            return "running"

        # All nodes terminal
        if ok and not bad:
            status = "completed"
            err = None
            event = "task.completed"
            msg = "Task Completed"
        elif ok and bad:
            status = "completed"
            failed_keys = [r["id"] for r in bad]
            err = f"partial success; failed nodes: {', '.join(failed_keys)}"
            event = "task.completed"
            msg = f"Task completed with {len(bad)} failed node(s)"
        else:
            status = "failed"
            err = (
                f"node {trigger_node} failed: {trigger_error}"
                if trigger_node
                else (trigger_error or "all nodes failed")
            )
            event = "task.failed"
            msg = (
                f"Task failed at node {trigger_node}"
                if trigger_node
                else "Task failed"
            )

        conn.execute(
            """
            UPDATE tasks
            SET status = %s,
                progress = %s,
                updated_at = %s,
                finished_at = %s,
                error_message = COALESCE(%s, error_message)
            WHERE id = %s::uuid AND status <> 'cancelled'
            """,
            (status, 100 if status == "completed" else progress, now, now, err, task_id),
        )
        self._append_event(
            conn,
            task_id,
            None,
            event,
            msg,
            {
                "progress": 100 if status == "completed" else progress,
                "ok": len(ok),
                "failed": len(bad),
                "node_key": trigger_node,
            },
        )
        return status

    def _task_status(self, conn: Any, task_id: str) -> str:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = %s::uuid",
            (task_id,),
        ).fetchone()
        return str(row["status"]) if row else "unknown"

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        tid = tenant_id or self.tenant_id
        allowed = {
            "pending",
            "ready",
            "running",
            "planning",
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "waiting_for_user",
            "waiting_for_agent",
        }
        statuses = [s.strip() for s in (status or "").split(",") if s.strip() in allowed]
        with self._connect() as conn:
            if len(statuses) == 1:
                rows = conn.execute(
                    """
                    SELECT id::text AS task_id, title, status, progress,
                           created_at, updated_at, finished_at
                    FROM tasks
                    WHERE tenant_id = %s::uuid AND status = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (tid, statuses[0], limit),
                ).fetchall()
            elif statuses:
                rows = conn.execute(
                    """
                    SELECT id::text AS task_id, title, status, progress,
                           created_at, updated_at, finished_at
                    FROM tasks
                    WHERE tenant_id = %s::uuid AND status = ANY(%s)
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (tid, statuses, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id::text AS task_id, title, status, progress,
                           created_at, updated_at, finished_at
                    FROM tasks
                    WHERE tenant_id = %s::uuid
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (tid, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if tenant_id:
                row = conn.execute(
                    """
                    SELECT id::text AS task_id, title, status, progress, input_json, plan_json,
                           result_json, error_message, created_at, updated_at, started_at, finished_at,
                           tenant_id::text AS tenant_id
                    FROM tasks WHERE id = %s::uuid AND tenant_id = %s::uuid
                    """,
                    (task_id, tenant_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id::text AS task_id, title, status, progress, input_json, plan_json,
                           result_json, error_message, created_at, updated_at, started_at, finished_at,
                           tenant_id::text AS tenant_id
                    FROM tasks WHERE id = %s::uuid
                    """,
                    (task_id,),
                ).fetchone()
            if not row:
                return None
            nodes = self._list_nodes(conn, task_id)
            return {**dict(row), "nodes": nodes}

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT id::text AS task_id, status FROM tasks WHERE id = %s::uuid FOR UPDATE",
                    (task_id,),
                ).fetchone()
                if not row:
                    return None
                now = _utc_now()
                # Sweep any still-active nodes even if the task was already marked
                # failed/completed (parallel peer left running after a sibling failed).
                swept = conn.execute(
                    """
                    UPDATE task_nodes
                    SET status = 'cancelled', updated_at = %s, finished_at = COALESCE(finished_at, %s),
                        error_message = COALESCE(error_message, 'cancelled by user')
                    WHERE task_id = %s::uuid
                      AND status IN ('pending', 'ready', 'running', 'retrying', 'waiting_for_user')
                    RETURNING id::text AS node_id, node_key
                    """,
                    (now, now, task_id),
                ).fetchall()
                already_terminal = row["status"] in ("completed", "failed", "cancelled")
                if already_terminal and not swept:
                    nodes = self._list_nodes(conn, task_id)
                    return {**dict(row), "nodes": nodes, "cancelled": False}

                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled', updated_at = %s, finished_at = %s,
                        error_message = COALESCE(error_message, 'cancelled by user')
                    WHERE id = %s::uuid
                    """,
                    (now, now, task_id),
                )
                for n in swept:
                    self._append_event(
                        conn,
                        task_id,
                        n["node_id"],
                        "task.node.cancelled",
                        f"Node {n['node_key']} cancelled",
                        {"node_key": n["node_key"]},
                    )
                self._append_event(
                    conn,
                    task_id,
                    None,
                    "task.cancelled",
                    "task cancelled",
                    {"swept_nodes": len(swept)},
                )
                nodes = self._list_nodes(conn, task_id)
                return {
                    "task_id": task_id,
                    "status": "cancelled",
                    "cancelled": True,
                    "nodes": nodes,
                }

    def overview_stats(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        tid = tenant_id or self.tenant_id
        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT
                  COUNT(*)::int AS total_tasks,
                  COUNT(*) FILTER (WHERE status = 'running')::int AS running,
                  COUNT(*) FILTER (WHERE status = 'completed')::int AS completed,
                  COUNT(*) FILTER (WHERE status = 'failed')::int AS failed
                FROM tasks
                WHERE tenant_id = %s::uuid
                """,
                (tid,),
            ).fetchone()
            queue = conn.execute(
                """
                SELECT COUNT(*)::int AS waiting
                FROM task_nodes n
                JOIN tasks t ON t.id = n.task_id
                WHERE n.status = 'ready' AND t.tenant_id = %s::uuid
                """,
                (tid,),
            ).fetchone()
            agents = conn.execute(
                """
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE status IN ('online', 'running'))::int AS online,
                  COUNT(*) FILTER (WHERE status = 'degraded')::int AS degraded,
                  COUNT(*) FILTER (WHERE status IN ('offline', 'disabled', 'unhealthy'))::int AS offline
                FROM agents
                WHERE tenant_id = %s::uuid OR tenant_id IS NULL
                """,
                (tid,),
            ).fetchone()
            trend_rows = conn.execute(
                """
                SELECT to_char(created_at::date, 'YYYY-MM-DD') AS day,
                       COUNT(*)::int AS count
                FROM tasks
                WHERE tenant_id = %s::uuid
                  AND created_at >= (CURRENT_DATE - INTERVAL '6 days')
                GROUP BY created_at::date
                ORDER BY created_at::date ASC
                """,
                (tid,),
            ).fetchall()

        total = int(totals["total_tasks"] or 0)
        completed = int(totals["completed"] or 0)
        failed = int(totals["failed"] or 0)
        finished = completed + failed
        success_rate = round((completed / finished) * 100, 1) if finished else 0.0

        # Fill missing days in last 7 days
        from datetime import date, timedelta

        day_map = {r["day"]: int(r["count"]) for r in trend_rows}
        today = date.today()
        trend = []
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            trend.append({"day": d, "count": day_map.get(d, 0)})

        return {
            "total_tasks": total,
            "running": int(totals["running"] or 0),
            "completed": completed,
            "failed": failed,
            "success_rate": success_rate,
            "agents_online": int(agents["online"] or 0),
            "agents_degraded": int(agents["degraded"] or 0),
            "agents_offline": int(agents["offline"] or 0),
            "agents_total": int(agents["total"] or 0),
            "queue_waiting": int(queue["waiting"] or 0),
            "trend": trend,
        }

    def list_events(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type, message, payload, created_at AS ts
                FROM task_events
                WHERE task_id = %s::uuid
                ORDER BY id ASC
                LIMIT %s
                """,
                (task_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def _load_plan(self, conn, task_id: str) -> TaskPlan:
        row = conn.execute(
            "SELECT plan_json FROM tasks WHERE id = %s::uuid",
            (task_id,),
        ).fetchone()
        if not row or not row["plan_json"]:
            raise ValueError(f"task plan not found: {task_id}")
        raw = row["plan_json"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        return TaskPlan.from_dict(raw)

    def _list_nodes(self, conn, task_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT tn.node_key AS id,
                   tn.status,
                   tn.assigned_agent_id::text AS agent_id,
                   COALESCE(
                     tn.output_json->'agent'->>'key',
                     a.agent_key,
                     tn.agent_key
                   ) AS agent_key,
                   COALESCE(a.name, tn.output_json->'agent'->>'key', tn.agent_key) AS agent_name,
                   tn.output_json->'agent'->>'endpoint' AS agent_endpoint,
                   tn.output_json->>'a2a_task_id' AS a2a_task_id,
                   tn.attempt,
                   tn.error_message,
                   LEFT(COALESCE(tn.output_json->>'text', ''), 800) AS output_excerpt,
                   tn.output_json->'handoff' AS handoff,
                   tn.output_json->'hitl' AS hitl,
                   tn.checkpoint_at,
                   tn.checkpoint_attempt,
                   tn.checkpoint_json->'artifact_ids' AS checkpoint_artifact_ids,
                   tn.checkpoint_json->'error' AS checkpoint_error,
                   tn.checkpoint_json->'status' AS checkpoint_status
            FROM task_nodes tn
            LEFT JOIN agents a ON a.id = tn.assigned_agent_id
            WHERE tn.task_id = %s::uuid
            ORDER BY tn.created_at ASC, tn.node_key ASC
            """,
            (task_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if hasattr(d.get("checkpoint_at"), "isoformat"):
                d["checkpoint_at"] = d["checkpoint_at"].isoformat()
            out.append(d)
        return out

    def _append_event(
        self,
        conn,
        task_id: str,
        node_id: str | None,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events (task_id, node_id, event_type, message, payload, created_at)
            VALUES (%s::uuid, %s::uuid, %s, %s, %s::jsonb, %s)
            """,
            (
                task_id,
                node_id,
                event_type,
                message,
                Jsonb(payload or {}),
                _utc_now(),
            ),
        )

    def _save_checkpoint(
        self,
        conn: Any,
        *,
        task_id: str,
        node_id: str,
        node_key: str,
        attempt: int,
        status: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Persist latest checkpoint on the node + append history row + event."""
        now = _utc_now()
        snap = dict(snapshot or {})
        snap.setdefault("status", status)
        snap.setdefault("attempt", int(attempt or 1))
        snap.setdefault("node_key", node_key)
        conn.execute(
            """
            UPDATE task_nodes
            SET checkpoint_json = %s::jsonb,
                checkpoint_at = %s,
                checkpoint_attempt = %s,
                updated_at = %s
            WHERE id = %s::uuid
            """,
            (Jsonb(snap), now, int(attempt or 1), now, node_id),
        )
        try:
            conn.execute(
                """
                INSERT INTO task_node_checkpoints (
                  task_id, node_id, node_key, attempt, status, snapshot_json, created_at
                ) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    task_id,
                    node_id,
                    node_key,
                    int(attempt or 1),
                    status,
                    Jsonb(snap),
                    now,
                ),
            )
        except Exception:
            # Migration may not be applied yet in some envs; node columns still updated.
            pass
        self._append_event(
            conn,
            task_id,
            node_id,
            "task.node.checkpoint",
            f"Checkpoint {node_key} attempt={attempt} status={status}",
            {
                "node_key": node_key,
                "attempt": int(attempt or 1),
                "status": status,
                "artifact_ids": snap.get("artifact_ids") or [],
                "error": snap.get("error"),
            },
        )

    def _build_checkpoint_snapshot(
        self,
        *,
        status: str,
        attempt: int,
        agent_key: str | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        input_json: Any = None,
    ) -> dict[str, Any]:
        out = output if isinstance(output, dict) else {}
        arts: list[str] = []
        handoff = out.get("handoff") if isinstance(out.get("handoff"), dict) else None
        if handoff and isinstance(handoff.get("artifact_ids"), list):
            arts = [str(a) for a in handoff["artifact_ids"] if a]
        elif isinstance(out.get("artifacts"), list):
            for a in out["artifacts"]:
                if isinstance(a, dict) and a.get("uri") and a.get("name") != "meta.json":
                    arts.append(str(a["uri"]))
        input_summary = None
        if isinstance(input_json, dict):
            input_summary = str(input_json.get("goal") or input_json.get("content") or "")[:240] or None
        elif isinstance(input_json, str):
            input_summary = input_json[:240]
        text = out.get("text")
        text_preview = (str(text)[:240] + "…") if isinstance(text, str) and len(text) > 240 else text
        return {
            "status": status,
            "attempt": int(attempt or 1),
            "agent_key": agent_key,
            "input_summary": input_summary,
            "output_preview": text_preview,
            "artifact_ids": arts,
            "error": error,
            "handoff": handoff,
            "hitl": out.get("hitl") if isinstance(out.get("hitl"), dict) else None,
            "has_output": bool(out),
        }

    # P36.1: Request tracking methods for idempotency (lazy import to avoid circular dependency)
    def _get_request_tracking_service(self):
        """Lazy import of request tracking service."""
        if self._request_tracking_service is None:
            try:
                from request_tracking import get_request_tracking_service
                self._request_tracking_service = get_request_tracking_service()
            except ImportError:
                self._request_tracking_service = None
        return self._request_tracking_service

    # P36.2: Outbox pattern methods for distributed transactions
    def _get_outbox_processor(self):
        """Lazy import of outbox processor."""
        if self._outbox_processor is None:
            try:
                from outbox import get_outbox_processor
                self._outbox_processor = get_outbox_processor()
            except ImportError:
                self._outbox_processor = None
        return self._outbox_processor

    def write_outbox_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        target_stream: str,
    ) -> str | None:
        """Write an outbox event for reliable message delivery."""
        processor = self._get_outbox_processor()
        if not processor:
            return None
        return processor.write_event(event_type, payload, target_stream)

    def track_a2a_request(
        self,
        idempotency_key: str,
        task_id: str,
        node_id: str,
        agent_id: str,
        request_json: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Track an A2A request for idempotency."""
        service = self._get_request_tracking_service()
        if not service:
            return None
        return service.track_request(
            idempotency_key, task_id, node_id, agent_id, request_json
        )

    def get_a2a_request(self, idempotency_key: str) -> dict[str, Any] | None:
        """Get an existing A2A request by idempotency key."""
        service = self._get_request_tracking_service()
        if not service:
            return None
        return service.get_request(idempotency_key)

    def mark_a2a_request_running(self, idempotency_key: str) -> bool:
        """Mark an A2A request as running."""
        service = self._get_request_tracking_service()
        if not service:
            return False
        return service.mark_request_running(idempotency_key)

    def mark_a2a_request_completed(
        self,
        idempotency_key: str,
        response_json: dict[str, Any],
    ) -> bool:
        """Mark an A2A request as completed."""
        service = self._get_request_tracking_service()
        if not service:
            return False
        return service.mark_request_completed(idempotency_key, response_json)

    def mark_a2a_request_failed(
        self,
        idempotency_key: str,
        error_message: str,
    ) -> bool:
        """Mark an A2A request as failed."""
        service = self._get_request_tracking_service()
        if not service:
            return False
        return service.mark_request_failed(idempotency_key, error_message)

    def mark_a2a_request_unknown(
        self,
        idempotency_key: str,
        reason: str = "timeout",
    ) -> bool:
        """Mark an A2A request as unknown (timeout/uncertain result)."""
        service = self._get_request_tracking_service()
        if not service:
            return False
        return service.mark_request_unknown(idempotency_key, reason)

    def is_a2a_request_completed(self, idempotency_key: str) -> bool:
        """Check if an A2A request is already completed."""
        service = self._get_request_tracking_service()
        if not service:
            return False
        return service.is_request_completed(idempotency_key)

    def get_cached_a2a_response(self, idempotency_key: str) -> dict[str, Any] | None:
        """Get cached response for a completed A2A request."""
        service = self._get_request_tracking_service()
        if not service:
            return None
        return service.get_cached_response(idempotency_key)

    def get_node_idempotency_key(self, task_id: str, node_key: str) -> str | None:
        """Get the stable idempotency key for a node."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT idempotency_key
                FROM task_nodes
                WHERE task_id = %s::uuid AND node_key = %s
                """,
                (task_id, node_key),
            ).fetchone()
            return row["idempotency_key"] if row else None

    def get_node_info(self, task_id: str, node_key: str) -> dict[str, Any] | None:
        """Get node information for request tracking."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id::text, task_id::text, node_key, agent_key, status, idempotency_key
                FROM task_nodes
                WHERE task_id = %s::uuid AND node_key = %s
                """,
                (task_id, node_key),
            ).fetchone()
            return dict(row) if row else None

# Import new components after class definitions to avoid circular imports
from .job_queue import JobQueue
from .event_publisher import EventPublisher
