import { cachedGet } from "@/lib/taskFetchCache";

/** Absolute Gateway URL (CLI snippets, WebSocket). Not used for browser REST. */
export const GATEWAY_PUBLIC_BASE = (
  process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8080"
).replace(/\/$/, "");

/**
 * Browser REST goes through same-origin BFF (`/api/backend`) so the session
 * stays in an HttpOnly cookie. Server/SSR talks to Gateway directly.
 */
export const API_BASE =
  typeof window !== "undefined"
    ? "/api/backend"
    : process.env.AOP_GATEWAY_URL ||
      process.env.NEXT_PUBLIC_API_BASE ||
      "http://127.0.0.1:8080";

/** @deprecated Prefer server-side AOP_API_KEY on the BFF. */
export const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

export const ORCHESTRATOR_BASE =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_ORCHESTRATOR_BASE) || "";

const LEGACY_SESSION_KEY = "aop_session_token";
const DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001";

const QUOTA_COPY: Record<string, string> = {
  quota_concurrent: "并发任务已满。到 Settings「配额」调高，或取消卡住的 running 任务。",
  quota_tasks_per_day: "今日任务数已达上限。到 Settings「配额」调高，或明天再试。",
  quota_runs_per_day: "今日 Agent 调用次数已达上限。到 Settings「配额」调高。",
  quota_usd_month: "本月预估费用已达上限。到 Settings「配额」调高。",
};

/** Clear pre-HttpOnly localStorage sessions (no longer used for auth). */
export function clearLegacySessionToken() {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(LEGACY_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

/** @deprecated Session is HttpOnly cookie — always empty in JS. */
export function getSessionToken(): string {
  return "";
}

/** @deprecated Prefer logout() which clears the HttpOnly cookie via BFF. */
export function setSessionToken(_token: string | null) {
  clearLegacySessionToken();
}

export async function request<T>(
  path: string,
  init?: RequestInit & { __sessionRetried?: boolean },
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    cache: "no-store",
    credentials: "include",
  });
  if (!res.ok) {
    const text = await res.text();
    if (res.status === 401 && !init?.__sessionRetried && typeof window !== "undefined") {
      try {
        await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      } catch {
        /* ignore */
      }
      clearLegacySessionToken();
      const { __sessionRetried: _ignored, ...rest } = init || {};
      return request<T>(path, { ...rest, __sessionRetried: true });
    }
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

function parseApiErrorBody(raw: string): { code?: string; message?: string } {
  const m = raw.match(/^\d{3}\s+([\s\S]*)/);
  const body = (m ? m[1] : raw).trim();
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    const detail = parsed.detail ?? parsed;
    if (typeof detail === "string") return { message: detail };
    if (detail && typeof detail === "object") {
      const d = detail as { code?: string; message?: string };
      return { code: d.code, message: d.message };
    }
  } catch {
    /* not JSON */
  }
  return { message: body };
}

/** Human-readable API error (403 cross-tenant, 404/503, quota 429, etc.). */
export function apiErrorMessage(err: unknown, fallback = "请求失败"): string {
  const raw = err instanceof Error ? err.message : String(err || fallback);
  if (/failed to fetch|network|timeout|AbortError/i.test(raw)) {
    return "网络超时或无法连接网关";
  }
  const parsed = parseApiErrorBody(raw);
  if (parsed.code && QUOTA_COPY[parsed.code]) return QUOTA_COPY[parsed.code];
  if (/^429\b/.test(raw) || parsed.code?.startsWith("quota_")) {
    return parsed.message || QUOTA_COPY.quota_concurrent;
  }
  if (/^403\b/.test(raw) || /\b403\b/.test(raw)) {
    return "无权访问其他租户数据";
  }
  if (/^404\b/.test(raw)) return "资源不存在或接口未部署";
  if (/^503\b/.test(raw)) return "服务暂不可用";
  const msg = parsed.message || raw;
  if (msg.length > 180) return msg.slice(0, 180) + "…";
  return msg || fallback;
}

export function getDefaultTenantId() {
  return (
    (typeof process !== "undefined" && process.env.NEXT_PUBLIC_DEFAULT_TENANT_ID) ||
    DEFAULT_TENANT_ID
  );
}

export async function requestAbsolute<T>(
  base: string,
  path: string,
  init?: RequestInit & { __sessionRetried?: boolean },
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(`${base.replace(/\/$/, "")}${path}`, {
    ...init,
    headers,
    cache: "no-store",
    credentials: base.startsWith("/") ? "include" : init?.credentials,
  });
  if (!res.ok) {
    const text = await res.text();
    if (res.status === 401 && !init?.__sessionRetried && typeof window !== "undefined") {
      try {
        await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      } catch {
        /* ignore */
      }
      clearLegacySessionToken();
      const { __sessionRetried: _ignored, ...rest } = init || {};
      return requestAbsolute<T>(base, path, { ...rest, __sessionRetried: true });
    }
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

/** Prefer Orchestrator direct base when set (local dev for capacity/reliability). */
export function requestOrchPreferred<T>(path: string, init?: RequestInit) {
  if (ORCHESTRATOR_BASE) {
    return requestAbsolute<T>(ORCHESTRATOR_BASE, path, init);
  }
  return request<T>(path, init);
}

export { cachedGet };
