"""Phase 31: Stripe Checkout Session dry-run / params."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from billing.stripe_checkout import (
    StripeCheckoutError,
    build_checkout_params,
    create_checkout_session,
    stripe_configured,
)


def _invoice():
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "invoice_number": "AOP-202603-TEST",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "currency": "USD",
        "total_usd": 0.055,
        "line_items": [
            {
                "description": "Task creation × 2",
                "quantity": 2,
                "unit_amount_usd": 0.02,
                "amount_usd": 0.04,
            },
            {
                "description": "Agent runs · claude-code × 3",
                "quantity": 3,
                "unit_amount_usd": 0.005,
                "amount_usd": 0.015,
            },
        ],
        "stripe": {
            "line_items": [
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": "Task creation × 2"},
                        "unit_amount": 2,
                    },
                    "quantity": 2,
                }
            ]
        },
    }


def test_stripe_configured_env(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert stripe_configured() is False
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    assert stripe_configured() is True


def test_build_checkout_params_has_line_items():
    params = build_checkout_params(_invoice())
    assert params["mode"] == "payment"
    assert "line_items[0][price_data][unit_amount]" in params
    assert params["metadata[invoice_number]"] == "AOP-202603-TEST"


def test_create_checkout_dry_run_without_secret(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    session = create_checkout_session(_invoice())
    assert session["dry_run"] is True
    assert session["id"].startswith("cs_test_dry_")
    assert session["url"]
    assert session["amount_total"] == 6  # 0.055 * 100 rounded? 5.5 -> 6? int(round(0.055*100))=6 yes


def test_create_checkout_empty_lines_raises():
    with pytest.raises(StripeCheckoutError, match="no billable"):
        create_checkout_session(
            {"currency": "USD", "total_usd": 0, "line_items": [], "stripe": {}}
        )


def test_create_checkout_live_mocked(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_live")
    fake = MagicMock()
    fake.status_code = 200
    fake.content = b'{"id":"cs_test_abc","object":"checkout.session","url":"https://checkout.stripe.com/c/pay/cs_test_abc","mode":"payment","status":"open","amount_total":6,"currency":"usd"}'
    fake.json.return_value = {
        "id": "cs_test_abc",
        "object": "checkout.session",
        "url": "https://checkout.stripe.com/c/pay/cs_test_abc",
        "mode": "payment",
        "status": "open",
        "amount_total": 6,
        "currency": "usd",
        "payment_status": "unpaid",
    }
    with patch("billing.stripe_checkout.httpx.post", return_value=fake) as post:
        session = create_checkout_session(_invoice(), dry_run=False)
    assert session["dry_run"] is False
    assert session["id"] == "cs_test_abc"
    assert post.called


def test_create_checkout_force_dry_even_with_secret(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_live")
    session = create_checkout_session(_invoice(), dry_run=True)
    assert session["dry_run"] is True
