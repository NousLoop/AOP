"""Task evaluation: heuristic scoring for completed / failed tasks."""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import os
from datetime import datetime, timezone
from typing import Any

from db import connect
from evaluation.rubric import score_task
from psycopg.types.json import Jsonb

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

class EvaluationService:
    def __init__(self, database_url: str | None = None):
        self.database_url = resolve_database_url(database_url)

    def evaluate(
        self,
        task_id: str,
        *,
        method: str = "heuristic",
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        with connect(self.database_url) as conn:
            task = conn.execute(
                """
                SELECT id::text AS task_id, tenant_id::text AS tenant_id, title, status,
                       progress, result_json, input_json, created_at, finished_at, updated_at
                FROM tasks WHERE id = %s::uuid
                """,
                (task_id,),
            ).fetchone()
            if not task:
                raise ValueError(f"task not found: {task_id}")
            tid = tenant_id or task["tenant_id"] or DEFAULT_TENANT_ID
            nodes = conn.execute(
                """
                SELECT node_key, agent_key, status, error_message, output_json,
                       started_at, finished_at
                FROM task_nodes WHERE task_id = %s::uuid
                """,
                (task_id,),
            ).fetchall()

            scored = score_task(dict(task), [dict(n) for n in nodes])
            now = _utc_now()
            row = conn.execute(
                """
                INSERT INTO task_evaluations (
                  tenant_id, task_id, method, score, grade, dimensions, summary, details, created_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s
                )
                ON CONFLICT (task_id, method) DO UPDATE SET
                  score = EXCLUDED.score,
                  grade = EXCLUDED.grade,
                  dimensions = EXCLUDED.dimensions,
                  summary = EXCLUDED.summary,
                  details = EXCLUDED.details,
                  created_at = EXCLUDED.created_at
                RETURNING id::text AS id, tenant_id::text AS tenant_id, task_id::text AS task_id,
                          method, score, grade, dimensions, summary, details, created_at
                """,
                (
                    tid,
                    task_id,
                    method,
                    scored["score"],
                    scored["grade"],
                    Jsonb(scored["dimensions"]),
                    scored["summary"],
                    Jsonb(scored["details"]),
                    now,
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO task_events (task_id, node_id, event_type, message, payload, created_at)
                VALUES (%s::uuid, NULL, 'task.evaluated', %s, %s::jsonb, %s)
                """,
                (
                    task_id,
                    f"Evaluation {scored['grade']} ({scored['score']})",
                    Jsonb({"score": scored["score"], "grade": scored["grade"], "method": method}),
                    now,
                ),
            )
            conn.commit()
            return dict(row) if row else scored

    def get_for_task(self, task_id: str, *, method: str = "heuristic") -> dict[str, Any] | None:
        with connect(self.database_url) as conn:
            row = conn.execute(
                """
                SELECT id::text AS id, tenant_id::text AS tenant_id, task_id::text AS task_id,
                       method, score, grade, dimensions, summary, details, created_at
                FROM task_evaluations
                WHERE task_id = %s::uuid AND method = %s
                """,
                (task_id, method),
            ).fetchone()
        return dict(row) if row else None

    def list(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 50,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        with connect(self.database_url) as conn:
            clauses = ["1=1"]
            args: list[Any] = []
            if tenant_id:
                clauses.append("e.tenant_id = %s::uuid")
                args.append(tenant_id)
            if min_score is not None:
                clauses.append("e.score >= %s")
                args.append(min_score)
            args.append(limit)
            rows = conn.execute(
                f"""
                SELECT e.id::text AS id, e.tenant_id::text AS tenant_id, e.task_id::text AS task_id,
                       e.method, e.score, e.grade, e.dimensions, e.summary, e.details, e.created_at,
                       t.title AS task_title, t.status AS task_status
                FROM task_evaluations e
                JOIN tasks t ON t.id = e.task_id
                WHERE {' AND '.join(clauses)}
                ORDER BY e.created_at DESC
                LIMIT %s
                """,
                args,
            ).fetchall()
        return [dict(r) for r in rows]

    def overview(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        with connect(self.database_url) as conn:
            args: list[Any] = []
            where = ""
            if tenant_id:
                where = "WHERE tenant_id = %s::uuid"
                args.append(tenant_id)
            row = conn.execute(
                f"""
                SELECT COUNT(*)::int AS total,
                       COALESCE(AVG(score), 0)::float AS avg_score,
                       COUNT(*) FILTER (WHERE grade = 'A')::int AS grade_a,
                       COUNT(*) FILTER (WHERE grade = 'B')::int AS grade_b,
                       COUNT(*) FILTER (WHERE grade = 'C')::int AS grade_c,
                       COUNT(*) FILTER (WHERE grade IN ('D','F'))::int AS grade_df
                FROM task_evaluations
                {where}
                """,
                args,
            ).fetchone()
        return dict(row) if row else {}
