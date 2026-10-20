import { request, requestOrchPreferred } from "./client";
import type { AgentIdentityCode } from "../identities";

export type AgentIdentity = AgentIdentityCode;

export type Agent = {
  agent_id: string;
  agent_key: string;
  name: string;
  description?: string;
  status: string;
  endpoint?: string;
  skills: string[];
  priority?: number;
  identity?: AgentIdentity | string;
  owner_user_id?: string;
  owner_email?: string;
  owner_display_name?: string;
};

export function isWorkAgentKey(agentKey?: string | null) {
  const k = (agentKey || "").trim().toLowerCase();
  if (!k) return false;
  return !k.startsWith("aop-node") && !k.startsWith("node-local");
}

export function listAgents(params?: { skill?: string; status?: string }) {
  const q = new URLSearchParams();
  if (params?.skill) q.set("skill", params.skill);
  if (params?.status) q.set("status", params.status);
  const qs = q.toString();
  return request<{ agents: Agent[] }>(`/v1/agents${qs ? `?${qs}` : ""}`).then(
    (data) => ({
      ...data,
      agents: (data.agents || []).filter((a) => isWorkAgentKey(a.agent_key)),
    }),
  );
}

export function registerAgent(endpoint: string) {
  return request<Record<string, unknown>>("/v1/agents/register", {
    method: "POST",
    body: JSON.stringify({ endpoint }),
  });
}

export function getAgent(agentId: string) {
  return request<Agent>(`/v1/agents/${agentId}`);
}

export function updateAgentMeta(
  agentId: string,
  body: { identity?: string; owner_user_id?: string },
) {
  return request<Agent>(`/v1/agents/${encodeURIComponent(agentId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function healthCheckAgent(agentId: string) {
  return request<Record<string, unknown>>(`/v1/agents/${agentId}/health`, {
    method: "POST",
  });
}

export function enableAgent(agentId: string) {
  return request<Record<string, unknown>>(`/v1/agents/${agentId}/enable`, {
    method: "POST",
  });
}

export function disableAgent(agentId: string) {
  return request<Record<string, unknown>>(`/v1/agents/${agentId}/disable`, {
    method: "POST",
  });
}

export function probeHealth() {
  return request<{
    probed: number;
    online: number;
    results: Array<Record<string, unknown>>;
  }>("/v1/health/probe", { method: "POST" });
}

export type AgentPerformance = {
  agent_id: string;
  agent_key: string;
  name: string;
  status: string;
  priority: number;
  request_count: number;
  success_count: number;
  failure_count: number;
  avg_latency_ms: number;
  success_rate: number;
};

export function getAgentStats(limit = 50) {
  return request<{ agents: AgentPerformance[] }>(
    `/v1/stats/agents?limit=${limit}`,
  );
}

export function previewRouter(skill: string) {
  return request<{
    skill: string;
    smart: boolean;
    selected: string | null;
    candidates: Array<{
      agent_id: string;
      agent_key: string;
      name: string;
      score: number;
      priority: number;
      score_breakdown?: Record<string, number>;
    }>;
  }>(`/v1/router/preview?skill=${encodeURIComponent(skill)}`);
}

export type AgentRuntimeHealth = {
  agent_id?: string;
  state?: string;
  active_tasks?: number;
  last_seen?: string;
  load?: number;
  version?: string;
  max_concurrency?: number;
  [key: string]: unknown;
};

export type AgentCapacity = {
  agent_id: string;
  health?: AgentRuntimeHealth | Record<string, unknown>;
  queue_depth?: number;
  quota?: Record<string, unknown>;
  accepts?: boolean;
  reason?: string;
};

export type AgentReliability = {
  agent_id: string;
  sample_size?: number;
  availability?: number;
  success_rate?: number;
  note?: string;
};

export function getAgentRuntimeHealth(agentId: string) {
  return request<AgentRuntimeHealth>(
    `/v1/agent-runtime/${encodeURIComponent(agentId)}/health`,
  );
}

/** Prefer Gateway; optional NEXT_PUBLIC_ORCHESTRATOR_BASE for local bypass. */
export function getAgentCapacity(agentId: string) {
  return requestOrchPreferred<AgentCapacity>(
    `/v1/agents/${encodeURIComponent(agentId)}/capacity`,
  );
}

export function getAgentReliability(agentId: string) {
  return requestOrchPreferred<AgentReliability>(
    `/v1/agents/${encodeURIComponent(agentId)}/reliability`,
  );
}

export function heartbeatAgent(agentId: string, body?: Record<string, unknown>) {
  return request<AgentRuntimeHealth>(
    `/v1/agent-runtime/${encodeURIComponent(agentId)}/heartbeat`,
    { method: "POST", body: JSON.stringify(body || {}) },
  );
}

export function drainAgent(agentId: string) {
  return request<AgentRuntimeHealth>(
    `/v1/agent-runtime/${encodeURIComponent(agentId)}/drain`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

// Scheduling
