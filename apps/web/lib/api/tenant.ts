import { request } from "./client";
import type { QuotaStatus } from "./billing";
import type { CostAggregate } from "./tasks";

export type TenantBudgetScope = {
  limit?: number;
  spent?: number;
  remaining?: number;
  [key: string]: unknown;
};

export type TenantBudget = {
  tenant?: TenantBudgetScope;
  day?: TenantBudgetScope;
  month?: TenantBudgetScope;
  task?: TenantBudgetScope;
  [key: string]: TenantBudgetScope | undefined;
};

export type SchedulingPolicyDoc = {
  max_cost?: number;
  max_latency_ms?: number;
  require_gpu?: boolean;
  require_streaming?: boolean;
  priority?: number;
  tenant_weight?: number;
  [key: string]: unknown;
};

export function getTenantQuota(tenantId: string) {
  return request<QuotaStatus | Record<string, unknown>>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/quota`,
  );
}

export function getTenantBudget(tenantId: string) {
  return request<TenantBudget>(`/v1/tenants/${encodeURIComponent(tenantId)}/budget`);
}

export function getTenantCost(tenantId: string) {
  return request<CostAggregate & { tenant_id?: string; trend?: Array<{ day: string; cost: number }> }>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/cost`,
  );
}

export function getTenantPolicy(tenantId: string) {
  return request<{ tenant_id: string; policy: SchedulingPolicyDoc }>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/policy`,
  );
}

export function setTenantPolicy(tenantId: string, policy: SchedulingPolicyDoc) {
  return request<{ tenant_id: string; policy: SchedulingPolicyDoc }>(
    `/v1/tenants/${encodeURIComponent(tenantId)}/policy`,
    { method: "POST", body: JSON.stringify({ policy }) },
  );
}
