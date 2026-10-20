import catalog from "@aop-catalogs/harness_profiles.json";

/** Shared via packages/catalogs/harness_profiles.json (aopd only). */

export const CLI_A2A_LISTEN = catalog.cli_a2a_listen;
export const CLI_A2A_BASE = `http://${CLI_A2A_LISTEN}`;

/** Registry / router agent_key values (aopd-claude-code, …). */
export const WORK_AGENT_KEYS = catalog.agents.map((p) => p.key);

/** aop-node plugin folder ids used in /agents/<id>/ paths. */
export const PLUGIN_IDS = catalog.agents.map(
  (p) => p.plugin_id || p.key.replace(/^aopd-/, ""),
);

/** @deprecated use WORK_AGENT_KEYS */
export const LEGACY_AGENT_KEYS = WORK_AGENT_KEYS;

type HarnessAgent = (typeof catalog.agents)[number];

function pluginIdOf(p: HarnessAgent): string {
  return p.plugin_id || p.key.replace(/^aopd-/, "");
}

function displayNameOf(p: HarnessAgent): string {
  return p.name || p.key;
}

/** Lookup by registry key (`aopd-claude-code`) or plugin id (`claude-code`). */
export function findHarnessAgent(keyOrPlugin?: string | null): HarnessAgent | undefined {
  const k = (keyOrPlugin || "").trim();
  if (!k) return undefined;
  return catalog.agents.find((p) => p.key === k || pluginIdOf(p) === k);
}

/** UI label from catalog `name`; falls back to key / plugin id. */
export function workAgentLabel(keyOrPlugin?: string | null): string {
  const k = (keyOrPlugin || "").trim();
  if (!k) return "";
  const hit = findHarnessAgent(k);
  return hit ? displayNameOf(hit) : k;
}

export const AOPD_AGENT_PRESETS = catalog.agents.map((p) => {
  const pluginId = pluginIdOf(p);
  const path = p.path || `/agents/${pluginId}`;
  return {
    id: pluginId,
    label: displayNameOf(p),
    key: p.key,
    endpoint: `${CLI_A2A_BASE}${path.endsWith("/") ? path : `${path}/`}`,
  };
});

export const DEFAULT_REGISTER_ENDPOINT =
  AOPD_AGENT_PRESETS[0]?.endpoint || `${CLI_A2A_BASE}/agents/claude-code/`;
