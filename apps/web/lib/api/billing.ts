import { request } from "./client";

export type BillingSkillRow = {
  agent_key: string;
  runs: number;
  success: number;
  failed: number;
  unit_price_usd: number;
  estimated_cost_usd: number;
  stored_cost_usd: number;
  latency_ms_sum: number;
};

export type BillingUsage = {
  tenant_id: string;
  window_days: number;
  since: string;
  currency: string;
  prices: {
    task_created: number;
    agent_run_default: number;
    per_skill: Record<string, number>;
  };
  tasks: {
    total?: number;
    completed?: number;
    failed?: number;
    active?: number;
  };
  task_cost_usd: number;
  agent_runs: {
    total: number;
    by_agent_key: BillingSkillRow[];
    estimated_cost_usd: number;
    stored_cost_usd: number;
  };
  estimated_total_usd: number;
  daily_tasks: Array<{ day: string; tasks: number }>;
  note?: string;
};

export function getBillingUsage(days = 30) {
  return request<BillingUsage>(`/v1/billing/usage?days=${days}`);
}

export function getBillingSummary() {
  return request<{ d7: BillingUsage; d30: BillingUsage }>("/v1/billing/summary");
}

export type InvoiceLineItem = {
  id: string;
  description: string;
  quantity: number;
  unit_amount_usd: number;
  amount_usd: number;
  skill?: string;
};

export type Invoice = {
  id?: string;
  invoice_number: string;
  tenant_id: string;
  status: string;
  currency: string;
  window_days: number;
  period_start: string;
  period_end: string;
  issued_at?: string;
  created_at?: string;
  issuer?: string;
  line_items: InvoiceLineItem[];
  subtotal_usd: number;
  tax_usd?: number;
  total_usd: number;
  note?: string;
  stripe?: {
    mode?: string;
    checkout_ready?: boolean;
    secret_configured?: boolean;
    line_items?: unknown[];
  };
};

export function getInvoicePreview(days = 30) {
  return request<Invoice>(`/v1/billing/invoice?days=${days}`);
}

export function createInvoice(days = 30, status = "draft") {
  return request<Invoice>("/v1/billing/invoices", {
    method: "POST",
    body: JSON.stringify({ days, status }),
  });
}

export function listInvoices(limit = 20) {
  return request<{ invoices: Invoice[] }>(`/v1/billing/invoices?limit=${limit}`);
}

export function invoiceMarkdownUrl(days = 30) {
  return `/v1/billing/invoice.md?days=${days}`;
}

export type CheckoutResult = {
  checkout: {
    id: string;
    url?: string;
    status?: string;
    dry_run?: boolean;
    amount_total?: number;
    currency?: string;
    note?: string;
  };
  record_id: string;
  invoice_id: string;
  invoice_number?: string;
  stripe_configured: boolean;
};

export function createCheckout(invoiceId: string, dryRun?: boolean) {
  return request<CheckoutResult>(`/v1/billing/invoices/${invoiceId}/checkout`, {
    method: "POST",
    body: JSON.stringify(dryRun === undefined ? {} : { dry_run: dryRun }),
  });
}

export function listCheckouts(limit = 10) {
  return request<{
    checkouts: Array<{
      id: string;
      invoice_id?: string;
      stripe_session_id: string;
      status: string;
      url?: string;
      amount_total_cents?: number;
      currency?: string;
      payment_status?: string;
      paid_at?: string;
      created_at?: string;
    }>;
  }>(`/v1/billing/checkouts?limit=${limit}`);
}

export function simulateCheckoutPaid(stripeSessionId: string) {
  return request<Record<string, unknown>>("/v1/billing/checkouts/simulate-paid", {
    method: "POST",
    body: JSON.stringify({ stripe_session_id: stripeSessionId }),
  });
}

export type QuotaStatus = {
  tenant_id: string;
  enforcement: boolean;
  limits: {
    tenant_id?: string;
    max_tasks_per_day: number;
    max_agent_runs_per_day: number;
    max_concurrent_tasks: number;
    max_estimated_usd_per_month: number;
    enabled: boolean;
    exists?: boolean;
    updated_at?: string | null;
  };
  usage: {
    tasks_today: number;
    agent_runs_today: number;
    concurrent_tasks: number;
    estimated_usd_30d: number;
    as_of?: string;
  };
  headroom: {
    tasks_today: number;
    agent_runs_today: number;
    concurrent_tasks: number;
    estimated_usd_30d: number;
  };
};

export function getQuotaStatus() {
  return request<QuotaStatus>("/v1/quotas");
}

export function updateQuota(body: {
  max_tasks_per_day?: number;
  max_agent_runs_per_day?: number;
  max_concurrent_tasks?: number;
  max_estimated_usd_per_month?: number;
  enabled?: boolean;
}) {
  return request<QuotaStatus["limits"]>("/v1/quotas", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export type EgressPolicy = {
  tenant_id: string;
  mode: "open" | "allowlist" | "denylist" | string;
  patterns: string;
  pattern_list?: string[];
  enabled: boolean;
  exists?: boolean;
  enforcement?: boolean;
  updated_at?: string | null;
};

export function getEgressPolicy() {
  return request<EgressPolicy>("/v1/egress");
}

export function updateEgress(body: {
  mode?: string;
  patterns?: string;
  enabled?: boolean;
}) {
  return request<EgressPolicy>("/v1/egress", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
