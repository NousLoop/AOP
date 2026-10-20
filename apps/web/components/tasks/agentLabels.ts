/** Agent key / display helpers — no React Flow (safe for drawers & lists). */

import { workAgentLabel } from "@/lib/harness";

/** Prefer assigned agent_key; fall back to name / plan hop id. */
export function hopAgentKey(node: {
  id?: string;
  agent_key?: string | null;
  agent_name?: string | null;
}): string {
  const assigned = (node.agent_key || "").trim();
  if (assigned) return assigned;
  return (node.agent_name || node.id || "agent").trim();
}

export function agentDisplayName(
  key?: string | null,
  fallbackName?: string | null,
): string {
  const k = (key || "").trim();
  if (fallbackName && fallbackName.trim()) return fallbackName.trim();
  if (!k) return "Agent";
  return workAgentLabel(k) || k;
}
