"""Phase 30: invoice builder from usage (no DB)."""

from __future__ import annotations

from billing.invoice import build_invoice_from_usage, invoice_to_markdown


def _sample_usage():
    return {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "window_days": 30,
        "since": "2026-02-16T00:00:00+00:00",
        "currency": "USD",
        "prices": {"task_created": 0.02, "agent_run_default": 0.01, "per_skill": {}},
        "tasks": {"total": 2, "completed": 1, "failed": 0, "active": 1},
        "task_cost_usd": 0.04,
        "agent_runs": {
            "total": 3,
            "by_agent_key": [
                {
                    "agent_key": "claude-code",
                    "runs": 3,
                    "unit_price_usd": 0.01,
                    "estimated_cost_usd": 0.03,
                }
            ],
            "estimated_cost_usd": 0.03,
            "stored_cost_usd": 0.0,
        },
        "estimated_total_usd": 0.07,
    }


def test_build_invoice_line_items():
    inv = build_invoice_from_usage(_sample_usage())
    assert inv["currency"] == "USD"
    assert inv["status"] == "draft"
    assert inv["total_usd"] == 0.07
    assert len(inv["line_items"]) == 2
    assert inv["stripe"]["checkout_ready"] is True
    assert inv["invoice_number"].startswith("AOP-")


def test_invoice_markdown_contains_total():
    inv = build_invoice_from_usage(_sample_usage())
    md = invoice_to_markdown(inv)
    assert "# Invoice" in md
    assert "0.0700" in md or "**Total:**" in md
    assert "claude-code" in md


def test_empty_usage_invoice():
    inv = build_invoice_from_usage(
        {
            "tenant_id": "t",
            "window_days": 7,
            "currency": "USD",
            "prices": {},
            "tasks": {"total": 0},
            "task_cost_usd": 0,
            "agent_runs": {"total": 0, "by_agent_key": []},
            "estimated_total_usd": 0,
        }
    )
    assert inv["total_usd"] == 0
    assert inv["stripe"]["checkout_ready"] is False
