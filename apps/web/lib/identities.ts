import catalog from "@aop-catalogs/identities.json";

/** Shared with Gateway / agent-runtime via packages/catalogs/identities.json */
export const DEFAULT_IDENTITY = catalog.default;

export type AgentIdentityCode = (typeof catalog.items)[number]["code"];

export const IDENTITY_OPTIONS = catalog.items.map((it) => ({
  value: it.code,
  label: it.label,
}));

export function identityLabel(code?: string) {
  const hit = IDENTITY_OPTIONS.find((o) => o.value === code);
  return hit?.label || code || IDENTITY_OPTIONS[0]?.label || "助理";
}
