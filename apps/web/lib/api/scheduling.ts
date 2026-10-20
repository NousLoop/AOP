import { request } from "./client";
import type { DiscoverCandidate } from "./discovery";

export type ScheduleRequirement = {
  skill?: string;
  version_constraint?: string;
  input_types?: string[];
  output_types?: string[];
  require_gpu?: boolean;
  require_streaming?: boolean;
  [key: string]: unknown;
};

export type ScheduleDecision = {
  selected?: DiscoverCandidate | null;
  candidates?: DiscoverCandidate[];
  excluded?: Array<{ agent_id?: string; reason?: string }>;
  reason?: string;
  scores?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SchedulePreviewResult = {
  preview?: boolean;
  decision?: ScheduleDecision;
  budget?: Record<string, unknown>;
  tenant_id?: string;
  selected?: DiscoverCandidate | null;
  reason?: string;
};

export type RecoveryPlan = {
  classification?: string;
  action?: "abort" | "reselect" | "retry_same" | string;
  exclude_agent_ids?: string[];
  reason?: string;
  [key: string]: unknown;
};

export function listSchedulingCandidates(skill: string, limit = 20) {
  const q = new URLSearchParams({ skill, limit: String(limit) });
  return request<{
    candidates: DiscoverCandidate[];
    excluded: Array<{ agent_id?: string; reason?: string }>;
    count: number;
    tenant_id?: string;
  }>(`/v1/scheduling/candidates?${q}`);
}

export function previewSchedule(body: {
  agents?: DiscoverCandidate[];
  requirement?: ScheduleRequirement;
  exclude_agent_ids?: string[];
  estimated_cost?: number;
  skill?: string;
}) {
  return request<SchedulePreviewResult>("/v1/scheduling/preview", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function selectSchedule(body: {
  agents?: DiscoverCandidate[];
  requirement?: ScheduleRequirement;
  exclude_agent_ids?: string[];
  estimated_cost?: number;
  skill?: string;
}) {
  return request<SchedulePreviewResult>("/v1/scheduling/select", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function simulateSchedule(body: Record<string, unknown>) {
  return request<Record<string, unknown>>("/v1/scheduling/simulate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getRecoveryPlan(body: {
  error_code?: string;
  error?: string;
  agent_id?: string;
  agent_state?: string;
  exclude_agent_ids?: string[];
}) {
  return request<RecoveryPlan>("/v1/scheduling/recovery-plan", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
