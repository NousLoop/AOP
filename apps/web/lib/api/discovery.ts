import { request } from "./client";

export type DiscoverCandidate = {
  agent_id: string;
  agent_key?: string;
  name?: string;
  status?: string;
  endpoint?: string;
  skills?: string[];
  priority?: number;
  score?: number;
  score_breakdown?: Record<string, number>;
  source?: string;
  [key: string]: unknown;
};

export type DiscoverResult = {
  required_skills?: string[];
  match_all_skills?: boolean;
  capabilities?: Record<string, unknown>;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  tenant_id?: string;
  candidates: DiscoverCandidate[];
  selected?: string | null;
  selected_agent?: DiscoverCandidate | null;
  filter_stats?: Record<string, unknown>;
};

export type DiscoverRequest = {
  required_skills?: string[];
  capabilities?: Record<string, unknown>;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  match_all_skills?: boolean;
  exclude_agent_ids?: string[];
  limit?: number;
};

export function discoverAgents(body: DiscoverRequest) {
  return request<DiscoverResult>("/v1/discover", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function routeRequest(body: DiscoverRequest & { skill?: string }) {
  return request<DiscoverResult>("/v1/route", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type SkillManifest = {
  name: string;
  version?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  requirements?: Record<string, unknown>;
  dependencies?: Record<string, unknown>;
  tags?: string[];
  status?: string;
  providers?: string[];
  [key: string]: unknown;
};

export function listSkills() {
  return request<{ skills: SkillManifest[] }>("/v1/skills");
}

export function getSkill(name: string) {
  return request<SkillManifest>(`/v1/skills/${encodeURIComponent(name)}`);
}

export function registerSkill(body: Partial<SkillManifest> & { name: string }) {
  return request<SkillManifest>("/v1/skills", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function searchSkills(body: {
  skill?: string;
  name?: string;
  q?: string;
  capability?: string;
  input?: string;
  output?: string;
  modality?: string;
  resource?: string;
  version?: string;
  tags?: string[];
}) {
  return request<{ skills: SkillManifest[]; count: number }>("/v1/skills/search", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function discoverBySkill(body: {
  skill: string;
  version?: string;
  version_constraint?: string;
}) {
  return request<Record<string, unknown>>("/v1/discover/skill", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function invokeBySkill(body: {
  skill: string;
  version?: string;
  version_constraint?: string;
}) {
  return request<Record<string, unknown>>("/v1/invoke/skill", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
