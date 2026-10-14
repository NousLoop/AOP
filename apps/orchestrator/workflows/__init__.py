"""Workflow templates: reusable Task DAGs."""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from db import connect
from psycopg.types.json import Jsonb

from planner.dag import PlanNode, TaskPlan, validate_plan
from router import hitl_skills

DEFAULT_RESEARCH_DAG = {
    "title": "Claude Code Agent",
    "nodes": [
        {"id": "run", "agent_key": "claude-code"},
    ],
}

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return s or "workflow"

class WorkflowService:
    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id

    def ensure_defaults(self) -> None:
        """Seed a built-in research workflow if registry is empty."""
        with connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM workflows WHERE tenant_id = %s::uuid",
                (self.tenant_id,),
            ).fetchone()
            if row and int(row["c"]) > 0:
                return
        self.create(
            name="Enterprise Research",
            description="Single hop to Claude Researcher",
            dag=DEFAULT_RESEARCH_DAG,
            workflow_key="enterprise-research",
            publish=True,
        )

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self.ensure_defaults()
        sql = """
            SELECT w.id::text AS workflow_id, w.workflow_key, w.name, w.description,
                   w.status, w.current_version, w.created_at, w.updated_at,
                   v.dag_json
            FROM workflows w
            LEFT JOIN workflow_versions v
              ON v.workflow_id = w.id AND v.version = w.current_version
            WHERE w.tenant_id = %s::uuid
        """
        args: list[Any] = [self.tenant_id]
        if status:
            sql += " AND w.status = %s"
            args.append(status)
        sql += " ORDER BY w.updated_at DESC"
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._serialize(r) for r in rows]

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        with connect(self.database_url) as conn:
            row = conn.execute(
                """
                SELECT w.id::text AS workflow_id, w.workflow_key, w.name, w.description,
                       w.status, w.current_version, w.created_at, w.updated_at,
                       v.dag_json
                FROM workflows w
                LEFT JOIN workflow_versions v
                  ON v.workflow_id = w.id AND v.version = w.current_version
                WHERE w.tenant_id = %s::uuid
                  AND (w.id::text = %s OR w.workflow_key = %s)
                """,
                (self.tenant_id, workflow_id, workflow_id),
            ).fetchone()
        return self._serialize(row) if row else None

    def create(
        self,
        *,
        name: str,
        description: str = "",
        dag: dict[str, Any],
        workflow_key: str | None = None,
        version: str = "1.0.0",
        publish: bool = True,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        key = workflow_key or _slugify(name)
        plan = self._dag_to_plan(dag, goal=dag.get("goal") or name, title=name)
        validate_plan(plan)
        dag_json = {
            "title": plan.title,
            "nodes": [n.to_dict() for n in plan.nodes],
        }

        now = _utc_now()
        wf_id = str(uuid.uuid4())
        status = "published" if publish else "draft"
        with connect(self.database_url) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO workflows (
                      id, tenant_id, workflow_key, name, description, status,
                      current_version, created_at, updated_at
                    ) VALUES (
                      %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        wf_id,
                        self.tenant_id,
                        key,
                        name,
                        description,
                        status,
                        version,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO workflow_versions (workflow_id, version, dag_json, is_active, created_at)
                    VALUES (%s::uuid, %s, %s::jsonb, true, %s)
                    """,
                    (wf_id, version, Jsonb(dag_json), now),
                )
        got = self.get(wf_id)
        assert got is not None
        return got

    def build_plan(self, workflow_id: str, *, goal: str, title: str | None = None) -> TaskPlan:
        wf = self.get(workflow_id)
        if not wf:
            raise ValueError(f"workflow not found: {workflow_id}")
        if wf.get("status") not in {"published", "draft"}:
            raise ValueError(f"workflow status not runnable: {wf.get('status')}")
        dag = wf.get("dag") or {}
        return self._dag_to_plan(dag, goal=goal, title=title or wf.get("name") or "workflow")

    def _dag_to_plan(self, dag: dict[str, Any], *, goal: str, title: str) -> TaskPlan:
        nodes_raw = dag.get("nodes") or []
        nodes = []
        for n in nodes_raw:
            key = str(n.get("agent_key") or "")
            nodes.append(
                PlanNode(
                    id=str(n["id"]),
                    agent_key=key,
                    depends_on=[str(d) for d in (n.get("depends_on") or [])],
                    requires_approval=bool(n.get("requires_approval"))
                    or key in hitl_skills()
                    or str(n.get("approval_mode") or "").strip().lower() in {"system", "both"},
                    approval_mode=(
                        str(n["approval_mode"]).strip().lower()
                        if isinstance(n.get("approval_mode"), str) and n["approval_mode"].strip()
                        else None
                    ),
                    approver_agent=(
                        str(n["approver_agent"]).strip()
                        if isinstance(n.get("approver_agent"), str) and n["approver_agent"].strip()
                        else None
                    ),
                )
            )
        return TaskPlan(title=title, goal=goal, nodes=nodes)

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        dag = row.get("dag_json") or {}
        if isinstance(dag, str):
            import json

            dag = json.loads(dag)
        return {
            "workflow_id": row["workflow_id"],
            "workflow_key": row["workflow_key"],
            "name": row["name"],
            "description": row.get("description") or "",
            "status": row["status"],
            "version": row.get("current_version"),
            "dag": dag,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
