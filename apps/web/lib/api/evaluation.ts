import { request, cachedGet } from "./client";

export type TaskEvaluation = {
  id?: string;
  tenant_id?: string;
  task_id: string;
  method: string;
  score: number;
  grade: string;
  dimensions?: Record<string, number>;
  summary?: string;
  details?: Record<string, unknown>;
  created_at?: string;
  task_title?: string;
  task_status?: string;
};

export type EvaluationOverview = {
  total: number;
  avg_score: number;
  grade_a: number;
  grade_b: number;
  grade_c: number;
  grade_df: number;
};

export function getTaskEvaluation(taskId: string) {
  return cachedGet(`eval:${taskId}`, 20_000, () =>
    request<TaskEvaluation>(`/v1/tasks/${taskId}/evaluation`),
  );
}

export function evaluateTask(taskId: string, method = "heuristic") {
  const qs = method ? `?method=${encodeURIComponent(method)}` : "";
  return request<TaskEvaluation>(`/v1/tasks/${taskId}/evaluate${qs}`, {
    method: "POST",
  });
}

export function listEvaluations(params?: { limit?: number; min_score?: number }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.min_score != null) q.set("min_score", String(params.min_score));
  const qs = q.toString();
  return request<{ evaluations: TaskEvaluation[] }>(
    `/v1/evaluations${qs ? `?${qs}` : ""}`,
  );
}

export function getEvaluationOverview() {
  return request<EvaluationOverview>("/v1/evaluations/overview");
}

export type MemoryEntry = {
  id?: string;
  task_id: string;
  memory_key: string;
  content: string;
  metadata?: Record<string, unknown>;
  updated_at?: string;
};

export function getTaskMemory(taskId: string) {
  return request<{ task_id: string; memories: MemoryEntry[] }>(
    `/v1/tasks/${taskId}/memory`,
  );
}

export function putTaskMemory(
  taskId: string,
  memoryKey: string,
  content: string,
  metadata?: Record<string, unknown>,
) {
  return request<MemoryEntry>(`/v1/tasks/${taskId}/memory`, {
    method: "PUT",
    body: JSON.stringify({
      memory_key: memoryKey,
      content,
      metadata: metadata || {},
    }),
  });
}

export type MetricsSnapshot = {
  generated_at?: string;
  window_hours?: number;
  tasks?: Record<string, number>;
  agents?: Record<string, number>;
  agent_runs?: Record<string, number>;
  memories?: { total?: number };
  evaluations?: { total?: number; avg_score?: number };
};

export function getMetrics(hours = 24) {
  return request<MetricsSnapshot>(`/v1/metrics?hours=${hours}`);
}
