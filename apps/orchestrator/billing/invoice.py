"""Invoice preview / snapshot from usage metering (Phase 30).

Produces Stripe-shaped line items without charging cards.
Optional STRIPE_SECRET_KEY enables a checkout-session *preview* payload only.
"""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from db import connect

from billing import BillingService, DEFAULT_TENANT_ID, load_prices
from billing.stripe_checkout import (
    StripeCheckoutError,
    create_checkout_session,
    stripe_configured,
)
from billing.webhook import (
    WebhookError,
    extract_checkout_session,
    payment_outcome,
    verify_stripe_signature,
)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def build_invoice_from_usage(
    usage: dict[str, Any],
    *,
    invoice_number: str | None = None,
    issuer: str | None = None,
) -> dict[str, Any]:
    """Pure transform: usage → invoice document."""
    now = _utc_now()
    tid = str(usage.get("tenant_id") or DEFAULT_TENANT_ID)
    days = int(usage.get("window_days") or 30)
    currency = str(usage.get("currency") or "USD")
    period_end = now
    since_raw = usage.get("since")
    try:
        period_start = (
            datetime.fromisoformat(str(since_raw).replace("Z", "+00:00"))
            if since_raw
            else now
        )
    except ValueError:
        period_start = now

    lines: list[dict[str, Any]] = []
    task_total = int((usage.get("tasks") or {}).get("total") or 0)
    task_cost = float(usage.get("task_cost_usd") or 0)
    task_unit = float((usage.get("prices") or {}).get("task_created") or 0)
    if task_total > 0 or task_cost > 0:
        lines.append(
            {
                "id": "task_created",
                "description": f"Task creation × {task_total}",
                "quantity": task_total,
                "unit_amount_usd": task_unit,
                "amount_usd": round(task_cost, 6),
            }
        )

    for row in (usage.get("agent_runs") or {}).get("by_agent_key") or []:
        qty = int(row.get("runs") or 0)
        if qty <= 0:
            continue
        agent_key = str(row.get("agent_key") or "unknown")
        unit = float(row.get("unit_price_usd") or 0)
        amt = float(row.get("estimated_cost_usd") or 0)
        lines.append(
            {
                "id": f"agent:{agent_key}",
                "description": f"Agent runs · {agent_key} × {qty}",
                "quantity": qty,
                "unit_amount_usd": unit,
                "amount_usd": round(amt, 6),
                "agent_key": agent_key,
            }
        )

    subtotal = round(sum(float(x["amount_usd"]) for x in lines), 6)
    total = subtotal  # tax/discount hooks later
    number = invoice_number or _default_invoice_number(tid, now)

    stripe_lines = [
        {
            "price_data": {
                "currency": currency.lower(),
                "product_data": {"name": item["description"][:120]},
                "unit_amount": int(round(float(item["unit_amount_usd"]) * 100)),
            },
            "quantity": max(1, int(item["quantity"])) if float(item["unit_amount_usd"]) > 0 else 1,
        }
        for item in lines
        if float(item["amount_usd"]) > 0
    ]

    doc = {
        "invoice_number": number,
        "tenant_id": tid,
        "status": "draft",
        "currency": currency,
        "window_days": days,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "issued_at": now.isoformat(),
        "issuer": issuer
        or os.getenv("BILLING_ISSUER", "AOP Platform (estimate)"),
        "line_items": lines,
        "subtotal_usd": subtotal,
        "tax_usd": 0.0,
        "total_usd": total,
        "note": "Estimate only — not a tax invoice or Stripe charge.",
        "stripe": {
            "mode": "payment",
            "line_items": stripe_lines,
            "checkout_ready": bool(stripe_lines),
            "secret_configured": bool(os.getenv("STRIPE_SECRET_KEY", "").strip()),
        },
        "usage_snapshot": {
            "tasks": usage.get("tasks"),
            "agent_runs_total": (usage.get("agent_runs") or {}).get("total"),
            "estimated_total_usd": usage.get("estimated_total_usd"),
        },
    }
    return doc

def invoice_to_markdown(invoice: dict[str, Any]) -> str:
    lines = [
        f"# Invoice {invoice.get('invoice_number')}",
        "",
        f"- Tenant: `{invoice.get('tenant_id')}`",
        f"- Status: {invoice.get('status')}",
        f"- Currency: {invoice.get('currency')}",
        f"- Period: {invoice.get('period_start')} → {invoice.get('period_end')}",
        f"- Issuer: {invoice.get('issuer')}",
        "",
        "| Description | Qty | Unit USD | Amount USD |",
        "|---|---:|---:|---:|",
    ]
    for item in invoice.get("line_items") or []:
        lines.append(
            f"| {item.get('description')} | {item.get('quantity')} | "
            f"{float(item.get('unit_amount_usd') or 0):.4f} | "
            f"{float(item.get('amount_usd') or 0):.4f} |"
        )
    lines.extend(
        [
            "",
            f"**Subtotal:** ${float(invoice.get('subtotal_usd') or 0):.4f}",
            f"**Tax:** ${float(invoice.get('tax_usd') or 0):.4f}",
            f"**Total:** ${float(invoice.get('total_usd') or 0):.4f}",
            "",
            f"_{invoice.get('note') or ''}_",
            "",
        ]
    )
    return "\n".join(lines)

def _default_invoice_number(tenant_id: str, when: datetime) -> str:
    digest = hashlib.sha1(f"{tenant_id}:{when.date().isoformat()}".encode()).hexdigest()[:6].upper()
    return f"AOP-{when.strftime('%Y%m')}-{digest}"

class InvoiceService:
    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        billing: BillingService | None = None,
    ):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id
        self.billing = billing or BillingService(
            database_url=self.database_url, tenant_id=tenant_id
        )

    def preview(self, *, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        usage = self.billing.usage(days=days, tenant_id=tenant_id)
        return build_invoice_from_usage(usage)

    def preview_markdown(self, *, days: int = 30, tenant_id: str | None = None) -> str:
        return invoice_to_markdown(self.preview(days=days, tenant_id=tenant_id))

    def create(
        self,
        *,
        days: int = 30,
        tenant_id: str | None = None,
        status: str = "draft",
    ) -> dict[str, Any]:
        tid = tenant_id or self.tenant_id
        usage = self.billing.usage(days=days, tenant_id=tid)
        now = _utc_now()
        number = _default_invoice_number(tid, now) + f"-{uuid.uuid4().hex[:4].upper()}"
        doc = build_invoice_from_usage(usage, invoice_number=number)
        doc["status"] = status if status in {"draft", "issued", "void"} else "draft"

        with connect(self.database_url) as conn:
            row = conn.execute(
                """
                INSERT INTO billing_invoices (
                  tenant_id, invoice_number, currency, window_days,
                  period_start, period_end, subtotal_usd, total_usd,
                  status, line_items, usage_snapshot, metadata
                ) VALUES (
                  %s::uuid, %s, %s, %s,
                  %s::timestamptz, %s::timestamptz, %s, %s,
                  %s, %s::jsonb, %s::jsonb, %s::jsonb
                )
                RETURNING id::text, created_at
                """,
                (
                    tid,
                    doc["invoice_number"],
                    doc["currency"],
                    doc["window_days"],
                    doc["period_start"],
                    doc["period_end"],
                    doc["subtotal_usd"],
                    doc["total_usd"],
                    doc["status"],
                    json.dumps(doc["line_items"]),
                    json.dumps(doc["usage_snapshot"]),
                    json.dumps({"stripe": doc.get("stripe"), "issuer": doc.get("issuer")}),
                ),
            ).fetchone()
            conn.commit()

        doc["id"] = row["id"]
        doc["created_at"] = row["created_at"].isoformat()
        return doc

    def list(self, *, tenant_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        tid = tenant_id or self.tenant_id
        limit = max(1, min(int(limit), 200))
        with connect(self.database_url) as conn:
            rows = conn.execute(
                """
                SELECT id::text, invoice_number, currency, window_days,
                       period_start, period_end, subtotal_usd::float AS subtotal_usd,
                       total_usd::float AS total_usd, status, created_at
                FROM billing_invoices
                WHERE tenant_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tid, limit),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            for key in ("period_start", "period_end", "created_at"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            out.append(item)
        return out

    def get(self, invoice_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        tid = tenant_id or self.tenant_id
        with connect(self.database_url) as conn:
            row = conn.execute(
                """
                SELECT id::text, tenant_id::text, invoice_number, currency, window_days,
                       period_start, period_end,
                       subtotal_usd::float AS subtotal_usd,
                       total_usd::float AS total_usd,
                       status, line_items, usage_snapshot, metadata, created_at
                FROM billing_invoices
                WHERE id = %s::uuid AND tenant_id = %s::uuid
                """,
                (invoice_id, tid),
            ).fetchone()
        if not row:
            return None
        doc = dict(row)
        for key in ("period_start", "period_end", "created_at"):
            if doc.get(key) is not None:
                doc[key] = doc[key].isoformat()
        meta = doc.pop("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        doc["issuer"] = (meta or {}).get("issuer")
        doc["stripe"] = (meta or {}).get("stripe")
        doc["tax_usd"] = 0.0
        doc["note"] = "Estimate only — not a tax invoice or Stripe charge."
        return doc

    def create_checkout(
        self,
        invoice_id: str,
        *,
        tenant_id: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        tid = tenant_id or self.tenant_id
        inv = self.get(invoice_id, tenant_id=tid)
        if not inv:
            raise LookupError(f"invoice not found: {invoice_id}")
        if float(inv.get("total_usd") or 0) <= 0:
            raise ValueError("invoice total must be > 0")

        session = create_checkout_session(inv, dry_run=dry_run)
        with connect(self.database_url) as conn:
            row = conn.execute(
                """
                INSERT INTO billing_checkout_sessions (
                  tenant_id, invoice_id, stripe_session_id, status, url,
                  amount_total_cents, currency, payload
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb
                )
                RETURNING id::text, created_at
                """,
                (
                    tid,
                    invoice_id,
                    session["id"],
                    session.get("status") or ("dry_run" if session.get("dry_run") else "open"),
                    session.get("url"),
                    session.get("amount_total"),
                    session.get("currency") or "usd",
                    json.dumps(session),
                ),
            ).fetchone()
            # Mark invoice issued when a live session is created
            if not session.get("dry_run"):
                conn.execute(
                    """
                    UPDATE billing_invoices
                    SET status = CASE WHEN status = 'draft' THEN 'issued' ELSE status END,
                        metadata = COALESCE(metadata, '{}'::jsonb)
                          || jsonb_build_object('checkout_session_id', %s::text)
                    WHERE id = %s::uuid AND tenant_id = %s::uuid
                    """,
                    (session["id"], invoice_id, tid),
                )
            conn.commit()

        return {
            "checkout": session,
            "record_id": row["id"],
            "created_at": row["created_at"].isoformat(),
            "invoice_id": invoice_id,
            "invoice_number": inv.get("invoice_number"),
            "stripe_configured": stripe_configured(),
        }

    def list_checkouts(
        self, *, tenant_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        tid = tenant_id or self.tenant_id
        limit = max(1, min(int(limit), 100))
        with connect(self.database_url) as conn:
            rows = conn.execute(
                """
                SELECT id::text, invoice_id::text, stripe_session_id, status, url,
                       amount_total_cents, currency, payment_status, paid_at, created_at
                FROM billing_checkout_sessions
                WHERE tenant_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tid, limit),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            for key in ("created_at", "paid_at"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            out.append(item)
        return out

    def handle_stripe_webhook(
        self,
        payload: bytes,
        sig_header: str | None,
    ) -> dict[str, Any]:
        event = verify_stripe_signature(payload, sig_header)
        event_id = str(event.get("id") or "").strip()
        event_type = str(event.get("type") or "")
        if not event_id:
            raise WebhookError("event.id required")

        with connect(self.database_url) as conn:
            existing = conn.execute(
                "SELECT id FROM billing_webhook_events WHERE stripe_event_id = %s",
                (event_id,),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "duplicate": True,
                    "stripe_event_id": event_id,
                    "event_type": event_type,
                }

            session = extract_checkout_session(event)
            if session is None:
                conn.execute(
                    """
                    INSERT INTO billing_webhook_events (stripe_event_id, event_type, payload)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (event_id, event_type, json.dumps(event)),
                )
                conn.commit()
                return {
                    "ok": True,
                    "ignored": True,
                    "stripe_event_id": event_id,
                    "event_type": event_type,
                    "note": "event type not handled",
                }

            sid = str(session.get("id") or "")
            checkout_status, invoice_status = payment_outcome(event_type, session)
            payment_status = str(session.get("payment_status") or "")
            paid = checkout_status == "paid"

            row = conn.execute(
                """
                SELECT id::text, invoice_id::text, tenant_id::text
                FROM billing_checkout_sessions
                WHERE stripe_session_id = %s
                LIMIT 1
                """,
                (sid,),
            ).fetchone()

            invoice_id = None
            if row:
                invoice_id = row.get("invoice_id")
                conn.execute(
                    """
                    UPDATE billing_checkout_sessions
                    SET status = %s,
                        payment_status = %s,
                        paid_at = CASE WHEN %s THEN COALESCE(paid_at, now()) ELSE paid_at END,
                        payload = COALESCE(payload, '{}'::jsonb) || %s::jsonb
                    WHERE stripe_session_id = %s
                    """,
                    (
                        checkout_status,
                        payment_status or checkout_status,
                        paid,
                        json.dumps({"last_event": event_type, "session": session}),
                        sid,
                    ),
                )
                if invoice_id and invoice_status:
                    if invoice_status == "paid":
                        conn.execute(
                            """
                            UPDATE billing_invoices
                            SET status = 'paid',
                                paid_at = COALESCE(paid_at, now()),
                                metadata = COALESCE(metadata, '{}'::jsonb)
                                  || jsonb_build_object(
                                       'paid_via', 'stripe_webhook',
                                       'stripe_event_id', %s::text
                                     )
                            WHERE id = %s::uuid
                            """,
                            (event_id, invoice_id),
                        )
                    elif invoice_status == "void":
                        conn.execute(
                            """
                            UPDATE billing_invoices
                            SET status = CASE WHEN status = 'paid' THEN status ELSE 'void' END
                            WHERE id = %s::uuid
                            """,
                            (invoice_id,),
                        )

            # Also match by client_reference_id if session row missing
            if not row:
                ref = str(session.get("client_reference_id") or "").strip()
                if ref:
                    inv = conn.execute(
                        """
                        SELECT id::text FROM billing_invoices WHERE id = %s::uuid LIMIT 1
                        """,
                        (ref,),
                    ).fetchone()
                    if inv and paid:
                        invoice_id = inv["id"]
                        conn.execute(
                            """
                            UPDATE billing_invoices
                            SET status = 'paid', paid_at = COALESCE(paid_at, now())
                            WHERE id = %s::uuid
                            """,
                            (invoice_id,),
                        )

            conn.execute(
                """
                INSERT INTO billing_webhook_events (
                  stripe_event_id, event_type, checkout_session_id, invoice_id, payload
                ) VALUES (%s, %s, %s, %s::uuid, %s::jsonb)
                """,
                (
                    event_id,
                    event_type,
                    sid or None,
                    invoice_id,
                    json.dumps(event),
                ),
            )
            conn.commit()

        grant: dict[str, Any] | None = None
        if invoice_id and invoice_status == "paid":
            try:
                with connect(self.database_url) as conn:
                    inv_row = conn.execute(
                        """
                        SELECT tenant_id::text, total_usd::float AS total_usd
                        FROM billing_invoices WHERE id = %s::uuid
                        """,
                        (invoice_id,),
                    ).fetchone()
                if inv_row:
                    from quota import QuotaService

                    grant = QuotaService(
                        database_url=self.database_url,
                        tenant_id=inv_row["tenant_id"],
                    ).grant_after_payment(
                        tenant_id=inv_row["tenant_id"],
                        invoice_id=invoice_id,
                        amount_usd=float(inv_row.get("total_usd") or 0),
                        stripe_event_id=event_id,
                    )
            except Exception as exc:  # noqa: BLE001
                grant = {"skipped": True, "error": str(exc)}

        return {
            "ok": True,
            "duplicate": False,
            "stripe_event_id": event_id,
            "event_type": event_type,
            "checkout_session_id": sid or None,
            "invoice_id": invoice_id,
            "checkout_status": checkout_status,
            "invoice_status": invoice_status,
            "quota_grant": grant,
        }

    def simulate_checkout_paid(
        self,
        stripe_session_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Dev helper: synthesize checkout.session.completed with payment_status=paid."""
        allow = os.getenv("STRIPE_WEBHOOK_ALLOW_UNSIGNED", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        from billing.webhook import webhook_secret

        if webhook_secret() and not allow:
            raise PermissionError(
                "simulate requires STRIPE_WEBHOOK_ALLOW_UNSIGNED=1 when secret is set"
            )

        event = {
            "id": f"evt_sim_{uuid.uuid4().hex[:16]}",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": stripe_session_id,
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "status": "complete",
                    "client_reference_id": None,
                }
            },
        }
        payload = json.dumps(event).encode("utf-8")
        prev = os.environ.get("STRIPE_WEBHOOK_ALLOW_UNSIGNED")
        os.environ["STRIPE_WEBHOOK_ALLOW_UNSIGNED"] = "1"
        try:
            return self.handle_stripe_webhook(payload, None)
        finally:
            if prev is None:
                os.environ.pop("STRIPE_WEBHOOK_ALLOW_UNSIGNED", None)
            else:
                os.environ["STRIPE_WEBHOOK_ALLOW_UNSIGNED"] = prev
