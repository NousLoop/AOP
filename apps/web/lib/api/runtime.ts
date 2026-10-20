import { request } from "./client";
import type { ArtifactItem } from "./artifacts";

export type RuntimeEdgeInput = {
  caller_agent_id: string;
  target_agent_id: string;
  root_task_id?: string;
  parent_task_id?: string;
  task_id?: string;
  correlation_id?: string;
  skill?: string;
  depth?: number;
  status?: string;
  tenant_id?: string;
};

export type CollaborationNode = {
  id: string;
  type: "agent" | "task" | string;
  label?: string;
  role?: "caller" | "target" | string;
  task_id?: string;
  status?: string;
  skill?: string;
  depth?: number;
  agent_id?: string;
  state?: string;
  active_tasks?: number;
  last_seen?: string;
  task_count?: number;
  [key: string]: unknown;
};

export type CollaborationHandoff = {
  from?: string;
  to?: string[] | string;
  reason?: string;
  artifact_ids?: string[];
  confidence?: number;
  skill?: string;
  node_key?: string;
  [key: string]: unknown;
};

export type CollaborationLink = {
  id: string;
  source: string;
  target: string;
  task_id?: string;
  parent_task_id?: string;
  skill?: string;
  /** Structured handoff reason: why this call happened */
  reason?: string;
  artifact_ids?: string[];
  handoff?: CollaborationHandoff | null;
  depth?: number;
  status?: string;
  correlation_id?: string;
  created_at?: string;
  [key: string]: unknown;
};

export type CollaborationHandoffSummary = {
  node_key?: string;
  task_id?: string;
  skill?: string;
  status?: string;
  reason?: string;
  from?: string;
  to?: string[] | string;
  artifact_ids?: string[];
  confidence?: number;
};

export type CollaborationGraph = {
  root_task_id: string;
  kind?: string;
  source?: string;
  node_count?: number;
  link_count?: number;
  max_depth?: number;
  agents?: string[];
  nodes: CollaborationNode[];
  links: CollaborationLink[];
  tree?: unknown[];
  edges?: unknown[];
  /** All MinIO artifacts for the root task */
  artifacts?: ArtifactItem[];
  /** Intermediate handoff payloads (中间传递) */
  handoffs?: CollaborationHandoffSummary[];
  /** Leaf-node / final result files (最终产物) */
  final_artifacts?: ArtifactItem[];
};

export type RuntimeGraph = {
  root_task_id: string;
  edges?: unknown[];
  nodes?: unknown[];
  tree?: unknown[];
  agents?: string[];
  max_depth?: number;
};

export type ExecutionEventDict = {
  event_type?: string;
  event_id?: string;
  timestamp?: string;
  root_task_id?: string;
  correlation_id?: string;
  task_id?: string;
  agent_id?: string;
  parent_task_id?: string;
  payload?: Record<string, unknown>;
  sequence?: number;
  [key: string]: unknown;
};

export function recordRuntimeEdge(body: RuntimeEdgeInput) {
  return request<Record<string, unknown>>("/v1/runtime/edges", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Raw runtime edges — prefer getCollaborationGraph for Console. */
export function getRuntimeGraph(rootTaskId: string) {
  return request<RuntimeGraph>(`/v1/runtime/graph/${encodeURIComponent(rootTaskId)}`);
}

/** Canonical enriched collaboration graph (nodes + links + lifecycle). */
export function getCollaborationGraph(rootTaskId: string) {
  return request<CollaborationGraph>(
    `/v1/collaboration/graph/${encodeURIComponent(rootTaskId)}`,
  );
}

/** Same as getCollaborationGraph plus attached Task row. */
export function getTaskCollaborationGraph(taskId: string) {
  return request<CollaborationGraph>(
    `/v1/tasks/${encodeURIComponent(taskId)}/collaboration-graph`,
  );
}

export function getCollaborationEvents(rootTaskId: string) {
  return request<{ root_task_id: string; events: ExecutionEventDict[]; count: number }>(
    `/v1/collaboration/events/${encodeURIComponent(rootTaskId)}`,
  );
}

// Governance
export type GovernanceCheckRequest = {
  root_task_id?: string;
  caller_agent_id: string;
  target_agent_id: string;
  caller_task_id?: string;
  correlation_id?: string;
  tenant_id?: string;
  requested_units?: number;
  units_estimated?: number;
  deadline?: string;
  claimed_depth?: number;
};

export type GovernanceCheckResult = {
  allowed: boolean;
  code?: string;
  reason?: string;
  context?: Record<string, unknown>;
  queued?: boolean;
  queue?: Record<string, unknown>;
};

export type GovernanceDenial = {
  code?: string;
  reason?: string;
  root_task_id?: string;
  caller_agent_id?: string;
  target_agent_id?: string;
  created_at?: string;
  timestamp?: string;
  context?: Record<string, unknown>;
  [key: string]: unknown;
};

export function governanceCheck(body: GovernanceCheckRequest) {
  return request<GovernanceCheckResult>("/v1/governance/check", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function governanceRelease(body?: Record<string, unknown>) {
  return request<{ released: boolean }>("/v1/governance/release", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
}

export function listGovernanceDenials(params?: {
  root_task_id?: string;
  code?: string;
  limit?: number;
}) {
  const q = new URLSearchParams();
  if (params?.root_task_id) q.set("root_task_id", params.root_task_id);
  if (params?.code) q.set("code", params.code);
  if (params?.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return request<{ denials: GovernanceDenial[]; count: number }>(
    `/v1/governance/denials${qs ? `?${qs}` : ""}`,
  );
}
