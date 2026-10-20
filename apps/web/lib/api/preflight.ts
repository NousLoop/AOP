import { request } from "./client";

export type PreflightService = {
  ok: boolean;
  detail?: string;
  hint?: string | null;
  worker_id?: string | null;
  pending?: number | null;
  pending_outbox?: number;
  oldest_pending_s?: number | null;
  queued?: number;
  ttl_s?: number;
  stuck_running?: number;
  waiting_hitl?: number;
};

export type PreflightAgent = {
  agent_key: string;
  name?: string;
  registry_status?: string;
  endpoint?: string;
  reachable: boolean;
  runner_ready: boolean;
  runner_reason?: string | null;
  runner_mode?: string | null;
};

export type PreflightSnapshot = {
  status: "ready" | "degraded" | "blocked";
  can_run: boolean;
  blocking: string[];
  warnings: string[];
  hints: string[];
  ready_agents: number;
  reachable_agents: number;
  registered_agents: number;
  /** Tasks in waiting_for_user — Inbox queue depth */
  waiting_hitl?: number;
  waiting_agent?: number;
  stuck_running?: number;
  services: {
    orchestrator?: PreflightService;
    postgres?: PreflightService;
    redis?: PreflightService;
    worker?: PreflightService;
    outbox?: PreflightService;
  };
  agents: PreflightAgent[];
  dev_up?: string;
  recover_steps?: RecoverStep[];
};

export type RecoverStep = {
  id: string;
  title: string;
  symptom: string;
  fix: string;
  console: string;
  active?: boolean;
};

export function getPreflight() {
  return request<PreflightSnapshot>("/v1/preflight");
}
