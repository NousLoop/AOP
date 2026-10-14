/** Shared status colors for TaskFlowDag (no React — safe for node + parent). */

export const STATUS_COLOR: Record<string, string> = {
  pending: "#4a665c",
  ready: "#ffb454",
  running: "#3dffa8",
  success: "#7eaea0",
  waiting_for_user: "#fbbf24",
  waiting_for_agent: "#7dd3fc",
  failed: "#ff6b6b",
  retrying: "#ffb454",
  cancelled: "#6b7280",
};

export const STATUS_LABEL: Record<string, string> = {
  pending: "等待",
  ready: "就绪",
  running: "执行中",
  success: "完成",
  waiting_for_user: "待系统审",
  waiting_for_agent: "待 Agent 审",
  failed: "失败",
  retrying: "重试",
  cancelled: "已取消",
  completed: "已完成",
};

export function isBalanceShortage(text?: string | null): boolean {
  if (!text) return false;
  const t = text.toLowerCase();
  if (text.includes("余额不足")) return true;
  if (
    /\b402\b/.test(text) &&
    (t.includes("insufficient") ||
      t.includes("api error") ||
      t.includes("payment required") ||
      t.includes("quota"))
  ) {
    return true;
  }
  return t.includes("insufficient balance") || t.includes("insufficient_quota");
}

export function flowVisualStatus(status: string, error?: string | null): string {
  if (isBalanceShortage(error) && (status === "success" || status === "completed")) {
    return "failed";
  }
  return status;
}

export function flowStatusLabel(status: string, error?: string | null): string {
  if (isBalanceShortage(error)) return "余额不足";
  return STATUS_LABEL[status] || status;
}
