/**
 * Split lib/api.ts into lib/api/*.ts and restore HttpOnly BFF auth.
 * Usage: node scripts/split-api.mjs
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const srcPath = path.join(root, "lib", "api.ts");
const outDir = path.join(root, "lib", "api");
const lines = fs.readFileSync(srcPath, "utf8").split(/\r?\n/);

function idx(pred, from = 0) {
  for (let i = from; i < lines.length; i++) if (pred(lines[i])) return i;
  return -1;
}

function block(startPred, endPred) {
  const s = idx(startPred);
  const e = endPred ? idx(endPred, s + 1) : lines.length;
  if (s < 0) throw new Error(`start not found: ${startPred}`);
  if (e < 0) throw new Error(`end not found after ${s}: ${endPred}`);
  return lines.slice(s, e).join("\n").trimEnd() + "\n";
}

function write(name, content) {
  fs.writeFileSync(path.join(outDir, name), content.endsWith("\n") ? content : content + "\n");
  console.log("wrote", name);
}

fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

// ---------- client.ts (auth + request + helpers) ----------
const client = `import { cachedGet } from "@/lib/taskFetchCache";

/** Absolute Gateway URL (CLI snippets, WebSocket). Not used for browser REST. */
export const GATEWAY_PUBLIC_BASE = (
  process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8080"
).replace(/\\/$/, "");

/**
 * Browser REST goes through same-origin BFF (\`/api/backend\`) so the session
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
  const res = await fetch(\`\${API_BASE}\${path}\`, {
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
    throw new Error(\`\${res.status} \${text}\`);
  }
  return res.json() as Promise<T>;
}

function parseApiErrorBody(raw: string): { code?: string; message?: string } {
  const m = raw.match(/^\\d{3}\\s+([\\s\\S]*)/);
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
  if (/^429\\b/.test(raw) || parsed.code?.startsWith("quota_")) {
    return parsed.message || QUOTA_COPY.quota_concurrent;
  }
  if (/^403\\b/.test(raw) || /\\b403\\b/.test(raw)) {
    return "无权访问其他租户数据";
  }
  if (/^404\\b/.test(raw)) return "资源不存在或接口未部署";
  if (/^503\\b/.test(raw)) return "服务暂不可用";
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
  const res = await fetch(\`\${base.replace(/\\/$/, "")}\${path}\`, {
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
    throw new Error(\`\${res.status} \${text}\`);
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
`;
write("client.ts", client);

const hdr = (extra = "") =>
  `import { request, API_BASE, GATEWAY_PUBLIC_BASE, cachedGet, requestOrchPreferred, clearLegacySessionToken } from "./client";\n${extra}`.trimEnd() + "\n";

write(
  "preflight.ts",
  `import { request } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type PreflightService"),
      (l) => l.startsWith("export type ApiKeyRecord"),
    ),
);

write(
  "tasks.ts",
  `import { request, API_BASE, cachedGet } from "./client";
import type { ArtifactItem } from "./artifacts";
export { invalidateTaskCache } from "@/lib/taskFetchCache";

` +
    block(
      (l) => l.startsWith("export type TaskNode"),
      (l) => l.startsWith("export type AgentIdentity"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export function createTask"),
      (l) => l.startsWith("export function listArtifacts"),
    )
      .replace(
        /const token = authHeader\(\);[\s\S]*?headers\.Authorization[\s\S]*?\n  \}/,
        "",
      )
      .replace(
        /const headers: Record<string, string> = \{\};\n  const res = await fetch\(/,
        "const res = await fetch(",
      )
      .replace(
        /\{ headers, cache: "no-store" \}/,
        '{ cache: "no-store", credentials: "include" }',
      ) +
    "\n" +
    block(
      (l) => l.startsWith("export function approveTask"),
      (l) => l.startsWith("export type AgentPerformance"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export type ExecutionRecord"),
      (l) => l.startsWith("export type VisualGraphNode"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export function pauseTask"),
      (l) => l.startsWith("export type AgentRuntimeHealth"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export type CostAggregate"),
      (l) => l.startsWith("export type TenantBudgetScope"),
    ),
);

write(
  "artifacts.ts",
  `import { request } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type ArtifactItem"),
      (l) => l.startsWith("export type OverviewStats"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export type OverviewStats"),
      (l) => l.startsWith("export type PreflightService"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export function listArtifacts"),
      (l) => l.startsWith("export function isWorkAgentKey"),
    ),
);

write(
  "agents.ts",
  `import { request, requestOrchPreferred } from "./client";
import type { AgentIdentityCode } from "../identities";

` +
    block(
      (l) => l.startsWith("export type AgentIdentity"),
      (l) => l.startsWith("export type ArtifactItem"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export function isWorkAgentKey"),
      (l) => l.startsWith("export type MarketplaceInstallMeta"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export function enableAgent"),
      (l) => l.startsWith("export function listApiKeys"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export type AgentPerformance"),
      (l) => l.startsWith("export type BillingSkillRow"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export type AgentRuntimeHealth"),
      (l) => l.startsWith("export type ScheduleRequirement"),
    ),
);

write(
  "marketplace.ts",
  `import { request, API_BASE, GATEWAY_PUBLIC_BASE } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type MarketplaceInstallMeta"),
      (l) => l.startsWith("export function enableAgent"),
    )
      .replace(
        /const base = API_BASE\.replace\(\/\\\/\$\/, ""\);/,
        "const base = GATEWAY_PUBLIC_BASE;",
      )
      .replace(
        /Prefer Gateway-reachable one-liners \(same host as NEXT_PUBLIC_API_BASE\)\./,
        "Prefer Gateway-reachable one-liners (absolute Gateway host, not the BFF).",
      )
      .replace(
        /const headers: Record<string, string> = \{\};\n  const token = authHeader\(\);[\s\S]*?const res = await fetch\(\n    `\$\{API_BASE\}\/v1\/marketplace/,
        "const res = await fetch(\n    `${API_BASE}/v1/marketplace",
      )
      .replace(
        /\{ headers, cache: "no-store" \}/,
        '{ cache: "no-store", credentials: "include" }',
      )
      // fallback if marketplace still uses API_BASE for deploy commands
      .replace(
        /const base = API_BASE\.replace\(\/\\\\\/\$\/, ""\);/,
        "const base = GATEWAY_PUBLIC_BASE;",
      ),
);

// Fix marketplace deploy base more simply after write
{
  let m = fs.readFileSync(path.join(outDir, "marketplace.ts"), "utf8");
  m = m.replace(
    /const base = API_BASE\.replace\(\/\\\/\$\/, ""\);/,
    "const base = GATEWAY_PUBLIC_BASE;",
  );
  // also handle already-GATEWAY or plain API_BASE without replace
  if (m.includes("marketplaceDeployCommands") && m.includes("const base = API_BASE")) {
    m = m.replace(
      /export function marketplaceDeployCommands\(pkg: MarketplacePackage\) \{\n  const base = API_BASE[^;]*;/,
      "export function marketplaceDeployCommands(pkg: MarketplacePackage) {\n  const base = GATEWAY_PUBLIC_BASE;",
    );
  }
  // strip authHeader download pattern if still present
  m = m.replace(
    /export async function downloadMarketplaceBundle\([\s\S]*?Promise<void> \{\n  const headers: Record<string, string> = \{\};\n  const token = authHeader\(\);\n  if \(token\) \{[\s\S]*?\n  \}\n  const res = await fetch\(/,
    "export async function downloadMarketplaceBundle(\n  packageId: string,\n  filename?: string,\n): Promise<void> {\n  const res = await fetch(",
  );
  m = m.replace(
    /\{ headers, cache: "no-store" \}/g,
    '{ cache: "no-store", credentials: "include" }',
  );
  fs.writeFileSync(path.join(outDir, "marketplace.ts"), m);
}

write(
  "auth.ts",
  `import { request, clearLegacySessionToken } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type ApiKeyRecord"),
      (l) => l.startsWith("async function request"),
    ) +
    "\n" +
    block(
      (l) => l.startsWith("export function listApiKeys"),
      (l) => l.startsWith("export type TaskEvaluation"),
    )
      .replace(
        /export function login\(email: string, password: string\) \{[\s\S]*?^\}/m,
        `export async function login(email: string, password: string) {
  clearLegacySessionToken();
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(\`\${res.status} \${text}\`);
  }
  return res.json() as Promise<{
    token_type?: string;
    expires_at: string;
    user: AuthUser;
    scopes: string[];
  }>;
}`,
      )
      .replace(
        /export function logout\(\) \{\n  return request<\{ logged_out: boolean \}>\("\/v1\/auth\/logout", \{ method: "POST" \}\);\n\}/,
        `export async function logout() {
  clearLegacySessionToken();
  const res = await fetch("/api/auth/logout", {
    method: "POST",
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(\`\${res.status} \${text}\`);
  }
  return res.json() as Promise<{ logged_out: boolean }>;
}`,
      ),
);

write(
  "evaluation.ts",
  `import { request, cachedGet } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type TaskEvaluation"),
      (l) => l.startsWith("export function approveTask"),
    ).replace(/^export \{ invalidateTaskCache \} from.*"@\/lib\/taskFetchCache";\n*/m, ""),
);

write(
  "billing.ts",
  `import { request } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type BillingSkillRow"),
      (l) => l.startsWith("// ─── A2A OS") || l.startsWith("const ORCHESTRATOR_BASE"),
    ),
);

write(
  "discovery.ts",
  `import { request } from "./client";\n\n` +
    block(
      (l) => l.startsWith("export type DiscoverCandidate") || l.startsWith("// Discover"),
      (l) => l.startsWith("// Runtime") || l.startsWith("export type RuntimeEdgeInput"),
    ).replace(/^\/\/ Discover[^\n]*\n/, "") +
    "\n" +
    block(
      (l) => l.startsWith("// Skills") || l.startsWith("export type SkillManifest"),
      (l) => l.startsWith("export { API_KEY"),
    ).replace(/^\/\/ Skills\n/, ""),
);

write(
  "runtime.ts",
  `import { request } from "./client";
import type { ArtifactItem } from "./artifacts";

` +
    block(
      (l) => l.startsWith("export type RuntimeEdgeInput"),
      (l) => l.startsWith("// Execution") || l.startsWith("export type ExecutionRecord"),
    ),
);

write(
  "visual.ts",
  `import { request, cachedGet } from "./client";\n\n` +
    block(
      (l) => l.startsWith("/** Visual Runtime") || l.startsWith("export type VisualGraphNode"),
      (l) => l.startsWith("export function pauseTask"),
    ).replace(/^\/\*\* Visual Runtime[^\n]*\n/, ""),
);

write(
  "scheduling.ts",
  `import { request } from "./client";
import type { DiscoverCandidate } from "./discovery";

` +
    block(
      (l) => l.startsWith("// Scheduling") || l.startsWith("export type ScheduleRequirement"),
      (l) => l.startsWith("// Cost") || l.startsWith("export type CostAggregate"),
    ).replace(/^\/\/ Scheduling\n/, ""),
);

write(
  "tenant.ts",
  `import { request } from "./client";
import type { QuotaStatus } from "./billing";
import type { CostAggregate, CostItem } from "./tasks";
import type { SchedulingPolicyDoc } from "./scheduling";

` +
    // SchedulingPolicyDoc is in tenant section in original - check
    block(
      (l) => l.startsWith("// Tenants") || l.startsWith("export type TenantBudgetScope"),
      (l) => l.startsWith("// Skills") || l.startsWith("export type SkillManifest"),
    ).replace(/^\/\/ Tenants\n/, ""),
);

// Fix tenant — SchedulingPolicyDoc is in the tenant block in original file
{
  let t = fs.readFileSync(path.join(outDir, "tenant.ts"), "utf8");
  if (t.includes("SchedulingPolicyDoc") && t.includes('from "./scheduling"')) {
    // SchedulingPolicyDoc lives in tenant block originally - remove wrong import if defined locally
    if (t.includes("export type SchedulingPolicyDoc")) {
      t = t.replace(
        /import type \{ SchedulingPolicyDoc \} from "\.\/scheduling";\n/,
        "",
      );
    }
  }
  // CostItem may not be needed
  if (!t.includes("CostItem")) {
    t = t.replace(
      /import type \{ CostAggregate, CostItem \} from "\.\/tasks";/,
      'import type { CostAggregate } from "./tasks";',
    );
  }
  fs.writeFileSync(path.join(outDir, "tenant.ts"), t);
}

write(
  "index.ts",
  `/** Domain-split API surface. Prefer \`@/lib/api\` or \`@/lib/api/<domain>\`. */
export * from "./client";
export * from "./preflight";
export * from "./tasks";
export * from "./artifacts";
export * from "./agents";
export * from "./marketplace";
export * from "./auth";
export * from "./evaluation";
export * from "./billing";
export * from "./discovery";
export * from "./runtime";
export * from "./visual";
export * from "./scheduling";
export * from "./tenant";
`,
);

fs.writeFileSync(
  srcPath,
  `/** Compatibility barrel — implementation lives in \`lib/api/\`. */\nexport * from "./api/index";\n`,
);
console.log("replaced lib/api.ts with barrel");
console.log("done");
