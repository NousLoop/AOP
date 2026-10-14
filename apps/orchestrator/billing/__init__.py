"""Tenant usage billing estimates (Phase 26).

Computes USD estimates from tasks + agent_runs using env price table.
Does not charge cards — metering / invoice preview only.
"""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from db import connect

# Default list prices (USD). Override via BILLING_PRICES_JSON.
_DEFAULT_PRICES: dict[str, Any] = {
    "currency": "USD",
    "task_created": 0.02,
    "agent_run": 0.01,
    "per_skill": {
        "claude-code": 0.01,
        "deepseek-harness": 0.008,
        "pi": 0.008,
        "openclaw": 0.008,
        "hermes": 0.008,
    },
}

def load_prices() -> dict[str, Any]:
    raw = os.getenv("BILLING_PRICES_JSON", "").strip()
    if not raw:
        return dict(_DEFAULT_PRICES)
    try:
        data = json.loads(raw)
        out = dict(_DEFAULT_PRICES)
        out.update({k: v for k, v in data.items() if k != "per_skill"})
        if isinstance(data.get("per_skill"), dict):
            skills = dict(_DEFAULT_PRICES["per_skill"])
            skills.update(data["per_skill"])
            out["per_skill"] = skills
        return out
    except json.JSONDecodeError:
        return dict(_DEFAULT_PRICES)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

class BillingService:
    def __init__(self, database_url: str | None = None, tenant_id: str = DEFAULT_TENANT_ID):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id
        self.prices = load_prices()

    def usage(self, *, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        days = max(1, min(int(days), 366))
        tid = tenant_id or self.tenant_id
        since = _utc_now() - timedelta(days=days)
        prices = self.prices
        task_price = float(prices.get("task_created") or 0)
        run_price = float(prices.get("agent_run") or 0)
        skill_prices: dict[str, float] = {
            str(k): float(v) for k, v in (prices.get("per_skill") or {}).items()
        }

        with connect(self.database_url) as conn:
            task_row = conn.execute(
                """
                SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE status = 'completed')::int AS completed,
                       COUNT(*) FILTER (WHERE status = 'failed')::int AS failed,
                       COUNT(*) FILTER (WHERE status IN ('running', 'waiting_for_user'))::int AS active
                FROM tasks
                WHERE tenant_id = %s::uuid AND created_at >= %s
                """,
                (tid, since),
            ).fetchone()

            runs = conn.execute(
                """
                SELECT
                  COALESCE(n.agent_key, 'unknown') AS agent_key,
                  COUNT(*)::int AS runs,
                  COUNT(*) FILTER (WHERE r.status = 'success')::int AS success,
                  COUNT(*) FILTER (WHERE r.status = 'failed')::int AS failed,
                  COALESCE(SUM(r.latency_ms), 0)::bigint AS latency_ms_sum,
                  COALESCE(SUM(r.cost_usd), 0)::float AS stored_cost_usd
                FROM agent_runs r
                LEFT JOIN task_nodes n ON n.id = r.node_id
                WHERE r.tenant_id = %s::uuid AND r.created_at >= %s
                GROUP BY 1
                ORDER BY runs DESC
                """,
                (tid, since),
            ).fetchall()

            daily = conn.execute(
                """
                SELECT date_trunc('day', created_at)::date AS day,
                       COUNT(*)::int AS tasks
                FROM tasks
                WHERE tenant_id = %s::uuid AND created_at >= %s
                GROUP BY 1
                ORDER BY 1
                """,
                (tid, since),
            ).fetchall()

        task_total = int((task_row or {}).get("total") or 0)
        task_cost = round(task_total * task_price, 6)

        by_agent: list[dict[str, Any]] = []
        run_total = 0
        run_cost = 0.0
        stored_cost = 0.0
        for row in runs:
            agent_key = row["agent_key"] or "unknown"
            n = int(row["runs"] or 0)
            run_total += n
            unit = skill_prices.get(agent_key, run_price)
            est = round(n * unit, 6)
            run_cost += est
            stored = float(row.get("stored_cost_usd") or 0)
            stored_cost += stored
            by_agent.append(
                {
                    "agent_key": agent_key,
                    "runs": n,
                    "success": int(row.get("success") or 0),
                    "failed": int(row.get("failed") or 0),
                    "unit_price_usd": unit,
                    "estimated_cost_usd": est,
                    "stored_cost_usd": round(stored, 6),
                    "latency_ms_sum": int(row.get("latency_ms_sum") or 0),
                }
            )

        estimated_total = round(task_cost + run_cost, 6)
        return {
            "tenant_id": tid,
            "window_days": days,
            "since": since.isoformat(),
            "currency": prices.get("currency") or "USD",
            "prices": {
                "task_created": task_price,
                "agent_run_default": run_price,
                "per_skill": skill_prices,
            },
            "tasks": dict(task_row or {}),
            "task_cost_usd": task_cost,
            "agent_runs": {
                "total": run_total,
                "by_agent_key": by_agent,
                "estimated_cost_usd": round(run_cost, 6),
                "stored_cost_usd": round(stored_cost, 6),
            },
            "estimated_total_usd": estimated_total,
            "daily_tasks": [
                {"day": str(r["day"]), "tasks": int(r["tasks"])} for r in daily
            ],
            "note": "Estimates only — not a payment charge. Set BILLING_PRICES_JSON to customize.",
        }

    def summary(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """Compact 7d / 30d totals for dashboard widgets."""
        return {
            "d7": self.usage(days=7, tenant_id=tenant_id),
            "d30": self.usage(days=30, tenant_id=tenant_id),
        }
