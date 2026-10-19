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

export const AOPD_AGENT_PRESETS = catalog.agents.map((p) => {
  const pluginId = p.plugin_id || p.key.replace(/^aopd-/, "");
  const path = p.path || `/agents/${pluginId}`;
  return {
    id: pluginId,
    label: p.key,
    key: p.key,
    endpoint: `${CLI_A2A_BASE}${path.endsWith("/") ? path : `${path}/`}`,
  };
});

export const DEFAULT_REGISTER_ENDPOINT =
  AOPD_AGENT_PRESETS[0]?.endpoint || `${CLI_A2A_BASE}/agents/claude-code/`;
