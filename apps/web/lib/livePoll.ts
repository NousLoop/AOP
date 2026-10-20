/** Shared live-poll cadence for Tasks / Visual Runtime. */

/** Drawer task/events when graph WS is down (HTTP-only). */
export const TASK_POLL_MS = 8_000;
/** Drawer reconcile while graph WS is healthy. */
export const TASK_POLL_MS_WHEN_WS = 20_000;

/** Graph HTTP reconcile while WS connected. */
export const GRAPH_POLL_MS_WHEN_WS = 15_000;
/** Graph HTTP when WS disconnected. */
export const GRAPH_POLL_MS = 4_000;

export function pageVisible(): boolean {
  return typeof document === "undefined" || !document.hidden;
}
