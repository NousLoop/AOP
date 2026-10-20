import { request } from "./client";

export type ArtifactItem = {
  task_id?: string;
  node_id?: string;
  name?: string;
  uri?: string;
  url?: string;
  mime_type?: string;
  type?: string;
  size?: number;
  last_modified?: string;
};

export type OverviewStats = {
  total_tasks: number;
  running: number;
  completed: number;
  failed: number;
  success_rate: number;
  agents_online: number;
  agents_degraded: number;
  agents_offline: number;
  agents_total: number;
  queue_waiting: number;
  trend: Array<{ day: string; count: number }>;
};

export function listArtifacts(params?: {
  task_id?: string;
  type?: string;
  limit?: number;
}) {
  const q = new URLSearchParams();
  if (params?.task_id) q.set("task_id", params.task_id);
  if (params?.type) q.set("type", params.type);
  if (params?.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  return request<{ artifacts: ArtifactItem[] }>(`/v1/artifacts${qs ? `?${qs}` : ""}`);
}

export function getOverviewStats() {
  return request<OverviewStats>("/v1/stats/overview");
}
