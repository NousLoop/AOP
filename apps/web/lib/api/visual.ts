import { request, cachedGet } from "./client";

export type VisualGraphNode = {
  id: string;
  type: "task" | "agent" | "tool" | "data" | "execution" | "error" | string;
  label: string;
  status: string;
  agent_id?: string | null;
  execution_id?: string | null;
  parent_id?: string | null;
  metadata?: Record<string, unknown>;
};

export type VisualGraphEdge = {
  id: string;
  source: string;
  target: string;
  type: string;
  status?: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
};

export type VisualRuntimeEvent = {
  event_id?: string;
  event_type: string;
  original_event_type?: string;
  task_id?: string;
  root_task_id?: string;
  correlation_id?: string;
  execution_id?: string;
  agent_id?: string;
  parent_agent_id?: string;
  timestamp?: string;
  sequence?: number | null;
  payload?: Record<string, unknown>;
};

export type VisualGraphSnapshot = {
  task_id: string;
  nodes: VisualGraphNode[];
  edges: VisualGraphEdge[];
  events: VisualRuntimeEvent[];
  status: string;
  sequence: number;
  task?: {
    created_at?: string;
    updated_at?: string;
    finished_at?: string;
    status?: string;
  };
};

export function getTaskVisualGraph(taskId: string, opts?: { bust?: boolean }) {
  return cachedGet(
    `graph:${taskId}`,
    1_200,
    () =>
      request<VisualGraphSnapshot>(
        `/v1/tasks/${encodeURIComponent(taskId)}/graph`,
      ),
    { bust: opts?.bust },
  );
}

export function getTaskVisualEvents(taskId: string) {
  return request<{
    task_id: string;
    root_task_id: string;
    events: VisualRuntimeEvent[];
    count: number;
    sequence: number;
  }>(`/v1/tasks/${encodeURIComponent(taskId)}/visual-events`);
}
