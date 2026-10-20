import { request, API_BASE, cachedGet } from "./client";
import type { ArtifactItem } from "./artifacts";
import type { ExecutionEventDict } from "./runtime";
import { invalidateTaskCache } from "@/lib/taskFetchCache";
export { invalidateTaskCache };

export type TaskNode = {
  id: string;
  agent_key: string;
  status: string;
  agent_id?: string | null;
  agent_name?: string | null;
  agent_endpoint?: string | null;
  a2a_task_id?: string | null;
  attempt?: number;
  error_message?: string | null;
  /** Truncated node output; used to detect provider 402 on wrongly-completed hops. */
  output_excerpt?: string | null;
  handoff?: Record<string, unknown> | null;
  hitl?: {
    mode?: string;
    approver_agent?: string;
    system_approved?: boolean;
    agent_approved?: boolean;
    reason?: string;
  } | null;
  checkpoint_at?: string | null;
  checkpoint_attempt?: number | null;
  checkpoint_artifact_ids?: string[] | null;
  checkpoint_error?: string | null;
  checkpoint_status?: string | null;
};

export type TaskPlan = {
  title?: string;
  goal?: string;
    nodes: Array<{
    id: string;
    agent_key?: string;
    depends_on?: string[];
    approval_mode?: string | null;
    approver_agent?: string | null;
    requires_approval?: boolean;
  }>;
};

export type TaskSummary = {
  task_id: string;
  title?: string;
  status: string;
  progress?: number;
  created_at?: string;
  updated_at?: string;
  finished_at?: string;
  /** OS ExecutionRecord.state when the DAG row is mirrored. */
  os_state?: string;
};

export type TaskDetail = {
  task_id: string;
  title?: string;
  status: string;
  progress?: number;
  input_json?: { type?: string; content?: string };
  os_state?: string;
  plan_json?: TaskPlan;
  result_json?: {
    summary?: string;
    artifacts?: Array<Record<string, unknown>>;
  };
  nodes: TaskNode[];
  created_at?: string;
  updated_at?: string;
  finished_at?: string;
  /** Phase 3+ execution snapshot when returned by orchestrator */
  execution?: {
    execute?: { root_task_id?: string; state?: string; [key: string]: unknown } | null;
    delegate?: { root_task_id?: string; [key: string]: unknown } | null;
    [key: string]: unknown;
  } | null;
  collaboration_graph?: unknown;
  runtime_graph?: unknown;
};

export type TaskEvent = {
  event_type: string;
  message?: string;
  payload?: Record<string, unknown>;
  ts?: string;
};

export function createTask(content: string) {
  return request<{
    task_id: string;
    status: string;
    title?: string;
    plan?: TaskPlan;
    ready_nodes?: string[];
    nodes?: TaskNode[];
  }>("/v1/tasks", {
    method: "POST",
    body: JSON.stringify({
      input: { type: "text", content },
    }),
  });
}

export function listTasks(params?: { status?: string; limit?: number }) {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  if (params?.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  return request<{ tasks: TaskSummary[] }>(`/v1/tasks${qs ? `?${qs}` : ""}`);
}

export function getTask(taskId: string, opts?: { include_graph?: boolean; bust?: boolean }) {
  if (opts?.bust) invalidateTaskCache(taskId);
  const q = opts?.include_graph ? "?include_graph=true" : "";
  const path = `/v1/tasks/${taskId}${q}`;
  return cachedGet(`task:${taskId}${q}`, 2_500, () => request<TaskDetail>(path));
}

export function cancelTask(taskId: string) {
  return request<{ task_id: string; status: string; cancelled?: boolean }>(
    `/v1/tasks/${taskId}/cancel`,
    { method: "POST" },
  );
}

export function getTaskEvents(taskId: string) {
  return cachedGet(`events:${taskId}`, 2_500, () =>
    request<{ events: TaskEvent[] }>(`/v1/tasks/${taskId}/events?limit=80`),
  );
}

export function getTaskArtifacts(taskId: string) {
  return cachedGet(`artifacts:${taskId}`, 8_000, () =>
    request<{ task_id: string; artifacts: ArtifactItem[] }>(
      `/v1/tasks/${taskId}/artifacts`,
    ),
  );
}

export async function downloadTaskPackage(
  taskId: string,
  filename?: string,
): Promise<void> {
  const headers: Record<string, string> = {};
  
  const res = await fetch(
    `${API_BASE}/v1/tasks/${encodeURIComponent(taskId)}/package`,
    { cache: "no-store", credentials: "include" },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/i.exec(cd);
  const name = filename || match?.[1] || `aop-run-${taskId.slice(0, 8)}.zip`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function taskReportUrl(taskId: string): string {
  return `${API_BASE}/v1/tasks/${encodeURIComponent(taskId)}/report`;
}

export function approveTask(
  taskId: string,
  nodeKey?: string,
  humanInput?: string,
  actor: "system" | "agent" = "system",
) {
  return request<{
    task_id: string;
    node_key: string;
    approved: boolean;
    has_human_input?: boolean;
    status?: string;
    enqueued_nodes?: string[];
    approval?: Record<string, unknown>;
  }>(`/v1/tasks/${taskId}/approve`, {
    method: "POST",
    body: JSON.stringify({
      node_key: nodeKey || null,
      input: humanInput?.trim() || null,
      actor,
    }),
  });
}

export function rejectTask(taskId: string, reason?: string, nodeKey?: string) {
  return request<{
    task_id: string;
    node_key: string;
    rejected: boolean;
    status?: string;
  }>(`/v1/tasks/${taskId}/reject`, {
    method: "POST",
    body: JSON.stringify({
      node_key: nodeKey || null,
      reason: reason || "rejected by user",
    }),
  });
}

export type ExecutionRecord = {
  state?: string;
  attempt?: number;
  max_retries?: number;
  agent_id?: string;
  operation?: string;
  idempotency_key?: string;
  ownership_token?: string;
  error?: string | null;
  branch_lineage?: unknown;
  visited_agents?: string[];
  created_at?: string;
  updated_at?: string;
  started_at?: string;
  finished_at?: string;
  [key: string]: unknown;
};

export type TaskExecutionView = {
  task_id: string;
  execute: ExecutionRecord | null;
  delegate: ExecutionRecord | null;
  events?: ExecutionEventDict[];
  recovery?: { class?: string; [key: string]: unknown } | string | null;
};

export function getTaskExecution(taskId: string) {
  return request<TaskExecutionView>(`/v1/tasks/${encodeURIComponent(taskId)}/execution`);
}

export function recoverTask(taskId: string) {
  return request<Record<string, unknown>>(`/v1/tasks/${encodeURIComponent(taskId)}/recover`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

/** Visual Runtime — Live Agent Execution Graph snapshot */

export function pauseTask(taskId: string) {
  return request<{ task_id: string; paused: boolean; execution?: ExecutionRecord }>(
    `/v1/tasks/${encodeURIComponent(taskId)}/pause`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function resumeTask(taskId: string) {
  return request<{ task_id: string; resumed: boolean; execution?: ExecutionRecord }>(
    `/v1/tasks/${encodeURIComponent(taskId)}/resume`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export type NodeCheckpoint = {
  id?: number;
  task_id?: string;
  node_id?: string;
  node_key: string;
  attempt: number;
  status: string;
  snapshot_json?: Record<string, unknown>;
  created_at?: string;
};

export function listTaskCheckpoints(
  taskId: string,
  params?: { node_key?: string; limit?: number },
) {
  const q = new URLSearchParams();
  if (params?.node_key) q.set("node_key", params.node_key);
  if (params?.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  return request<{ task_id: string; checkpoints: NodeCheckpoint[]; count: number }>(
    `/v1/tasks/${encodeURIComponent(taskId)}/checkpoints${qs ? `?${qs}` : ""}`,
  );
}

export function replayTaskNode(
  taskId: string,
  nodeKey: string,
  opts?: { clear_downstream?: boolean },
) {
  return request<{
    task_id: string;
    node_key: string;
    replayed: boolean;
    next_attempt?: number;
    cleared_downstream?: string[];
    enqueued_nodes?: string[];
    status?: string;
  }>(`/v1/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeKey)}/replay`, {
    method: "POST",
    body: JSON.stringify({
      clear_downstream: opts?.clear_downstream !== false,
    }),
  });
}

// Agent runtime lifecycle (gateway-safe path for health/heartbeat/drain)

export type CostAggregate = {
  task_id?: string;
  root_task_id?: string;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  gpu_seconds?: number;
  cpu_seconds?: number;
  wall_time_ms?: number;
  estimated_cost?: number;
  count?: number;
  [key: string]: unknown;
};

export type CostItem = {
  agent_id?: string;
  attempt?: number;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  gpu_seconds?: number;
  cpu_seconds?: number;
  wall_time_ms?: number;
  estimated_cost?: number;
  [key: string]: unknown;
};

export type CostBreakdown = {
  root_task_id?: string;
  total?: CostAggregate;
  by_agent?: Record<string, CostAggregate>;
  items?: CostItem[];
};

export function getTaskCost(taskId: string) {
  return request<CostAggregate & { items?: CostItem[] }>(
    `/v1/tasks/${encodeURIComponent(taskId)}/cost`,
  );
}

export function getTaskCostBreakdown(taskId: string) {
  return request<CostBreakdown>(
    `/v1/tasks/${encodeURIComponent(taskId)}/cost/breakdown`,
  );
}
