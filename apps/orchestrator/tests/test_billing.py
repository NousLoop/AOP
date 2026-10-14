"""Phase 26: billing price table + usage aggregation."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from billing import BillingService, load_prices


def test_load_prices_defaults(monkeypatch):
    monkeypatch.delenv("BILLING_PRICES_JSON", raising=False)
    prices = load_prices()
    assert prices["currency"] == "USD"
    assert prices["task_created"] == 0.02
    assert "claude-code" in prices["per_skill"]
    assert "web-search" not in prices["per_skill"]


def test_load_prices_override(monkeypatch):
    monkeypatch.setenv(
        "BILLING_PRICES_JSON",
        json.dumps(
            {
                "task_created": 0.05,
                "per_skill": {"claude-code": 0.1},
            }
        ),
    )
    prices = load_prices()
    assert prices["task_created"] == 0.05
    assert prices["per_skill"]["claude-code"] == 0.1


def test_load_prices_invalid_json(monkeypatch):
    monkeypatch.setenv("BILLING_PRICES_JSON", "{not-json")
    prices = load_prices()
    assert prices["task_created"] == 0.02


def test_usage_aggregation_mocked(monkeypatch):
    monkeypatch.delenv("BILLING_PRICES_JSON", raising=False)
    task_row = {"total": 2, "completed": 1, "failed": 0, "active": 1}
    run_rows = [
        {
            "agent_key": "claude-code",
            "runs": 3,
            "success": 2,
            "failed": 1,
            "latency_ms_sum": 900,
            "stored_cost_usd": 0.0,
        }
    ]
    daily_rows = [{"day": date(2026, 3, 1), "tasks": 2}]

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=task_row)),
        MagicMock(fetchall=MagicMock(return_value=run_rows)),
        MagicMock(fetchall=MagicMock(return_value=daily_rows)),
    ]

    with patch("billing.connect", return_value=conn):
        out = BillingService().usage(days=0)  # clamps to 1

    assert out["window_days"] == 1
    assert out["tasks"]["total"] == 2
    assert out["task_cost_usd"] == 0.04  # 2 * 0.02
    assert out["agent_runs"]["total"] == 3
    assert out["agent_runs"]["by_agent_key"][0]["estimated_cost_usd"] == 0.03  # 3 * 0.01
    assert out["estimated_total_usd"] == 0.07
    assert out["daily_tasks"][0]["tasks"] == 2
