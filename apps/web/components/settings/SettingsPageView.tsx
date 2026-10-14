"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  createApiKey,
  createCheckout,
  createInvoice,
  getBillingUsage,
  getEgressPolicy,
  getGatewayHealth,
  getInvoicePreview,
  getQuotaStatus,
  getRbacMe,
  inviteTeammate,
  invoiceMarkdownUrl,
  listApiKeys,
  listAuditLogs,
  listCheckouts,
  listInvoices,
  listRbacRoles,
  listTeammates,
  probeHealth,
  revokeApiKey,
  simulateCheckoutPaid,
  updateEgress,
  updateQuota,
  type ApiKeyRecord,
  type AuditLogEntry,
  type AuthUser,
  type BillingUsage,
  type CheckoutResult,
  type EgressPolicy,
  type Invoice,
  type QuotaStatus,
  type RbacRole,
} from "@/lib/api";
import { TenantPanel } from "@/components/settings/TenantPanel";
import { GovernancePanel } from "@/components/settings/GovernancePanel";
import { ChaosPlaybook } from "@/components/ops/ChaosPlaybook";
import { usePreflight } from "@/hooks/usePreflight";

const NAV_GROUPS = [
  {
    id: "access",
    label: "访问",
    items: [
      { id: "keys", label: "API Keys", hint: "密钥创建与撤销" },
      { id: "team", label: "同事", hint: "邀请登录与角色" },
      { id: "rbac", label: "权限", hint: "角色与 scopes" },
    ],
  },
  {
    id: "org",
    label: "租户",
    items: [
      { id: "tenant", label: "租户", hint: "配额 · 预算 · 策略" },
      { id: "governance", label: "治理", hint: "委托审计与检查" },
    ],
  },
  {
    id: "cost",
    label: "计费",
    items: [
      { id: "billing", label: "用量", hint: "成本与发票" },
      { id: "quotas", label: "配额", hint: "日限额与并发" },
    ],
  },
  {
    id: "ops",
    label: "运维",
    items: [
      { id: "egress", label: "出站", hint: "Browser URL 策略" },
      { id: "monitor", label: "监控", hint: "健康探测与故障恢复" },
      { id: "storage", label: "存储", hint: "MinIO / artifacts" },
      { id: "audit", label: "审计", hint: "操作日志" },
    ],
  },
] as const;

type TabId = (typeof NAV_GROUPS)[number]["items"][number]["id"];

const ALL_TABS: { id: TabId; label: string; hint: string; group: string }[] =
  NAV_GROUPS.flatMap((g) =>
    g.items.map((item) => ({ ...item, group: g.label })),
  );

const TAB_META: Record<TabId, { label: string; hint: string }> = Object.fromEntries(
  ALL_TABS.map((t) => [t.id, { label: t.label, hint: t.hint }]),
) as Record<TabId, { label: string; hint: string }>;

const ROLE_HINTS: Record<string, string> = {
  viewer: "只读任务与 Agent，不能跑任务或改密钥",
  operator: "跑任务、审批、查看审计；不能邀请同事或管密钥",
  admin: "全部权限，含邀请同事与 API Key",
};

const FALLBACK_ROLES: RbacRole[] = [
  { role: "viewer", scopes: ["task.read", "agent.read", "memory.read"] },
  {
    role: "operator",
    scopes: [
      "task.read",
      "task.write",
      "agent.read",
      "agent.write",
      "memory.read",
      "memory.write",
      "audit.read",
    ],
  },
  { role: "admin", scopes: ["*"] },
];

function usd(n: number | undefined) {
  return `$${(n ?? 0).toFixed(4)}`;
}

export function SettingsPageView() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const tabFromUrl = searchParams.get("tab") as TabId | null;
  const rootFromUrl = searchParams.get("root_task_id");
  const [tab, setTab] = useState<TabId>(
    tabFromUrl && ALL_TABS.some((t) => t.id === tabFromUrl) ? tabFromUrl : "keys",
  );
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [roles, setRoles] = useState<RbacRole[]>(FALLBACK_ROLES);
  const [me, setMe] = useState<Record<string, unknown> | null>(null);
  const [audits, setAudits] = useState<AuditLogEntry[]>([]);
  const [billing, setBilling] = useState<BillingUsage | null>(null);
  const [billingDays, setBillingDays] = useState(30);
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [lastCheckout, setLastCheckout] = useState<CheckoutResult | null>(null);
  const [checkouts, setCheckouts] = useState<
    Array<{
      id: string;
      stripe_session_id: string;
      status: string;
      payment_status?: string;
      paid_at?: string;
    }>
  >([]);
  const [quota, setQuota] = useState<QuotaStatus | null>(null);
  const [egress, setEgress] = useState<EgressPolicy | null>(null);
  const [egressForm, setEgressForm] = useState({
    mode: "open",
    patterns: "",
    enabled: true,
  });
  const [quotaForm, setQuotaForm] = useState({
    max_tasks_per_day: 100,
    max_agent_runs_per_day: 500,
    max_concurrent_tasks: 20,
    max_estimated_usd_per_month: 100,
    enabled: true,
  });
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("console-key");
  const [role, setRole] = useState("operator");
  const [createdPlain, setCreatedPlain] = useState<string | null>(null);
  const [teammates, setTeammates] = useState<AuthUser[]>([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState("operator");
  const [inviteWithKey, setInviteWithKey] = useState(false);
  const [inviteSecret, setInviteSecret] = useState<string | null>(null);
  const [inviteKey, setInviteKey] = useState<string | null>(null);
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  const [probe, setProbe] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { snapshot: preflight } = usePreflight();

  useEffect(() => {
    if (tabFromUrl && ALL_TABS.some((t) => t.id === tabFromUrl)) {
      setTab(tabFromUrl);
    }
  }, [tabFromUrl]);

  function selectTab(id: TabId) {
    setTab(id);
    const q = new URLSearchParams(searchParams.toString());
    q.set("tab", id);
    router.replace(`/settings?${q.toString()}`);
  }

  const meta = useMemo(() => TAB_META[tab], [tab]);

  async function loadKeys() {
    try {
      const data = await listApiKeys();
      setKeys(data.api_keys || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load keys failed");
    }
  }

  async function loadTeammates() {
    try {
      const data = await listTeammates();
      setTeammates(data.users || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load teammates failed");
    }
  }

  async function loadRbac() {
    try {
      const [r, m] = await Promise.all([listRbacRoles(), getRbacMe()]);
      if (r.roles?.length) setRoles(r.roles);
      setMe(m as unknown as Record<string, unknown>);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load rbac failed");
    }
  }

  async function loadAudits() {
    try {
      const data = await listAuditLogs(40);
      setAudits(data.audit_logs || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load audit failed");
    }
  }

  async function loadBilling(days = billingDays) {
    try {
      const [data, inv, listed, cos] = await Promise.all([
        getBillingUsage(days),
        getInvoicePreview(days),
        listInvoices(10).catch(() => ({ invoices: [] as Invoice[] })),
        listCheckouts(10).catch(() => ({ checkouts: [] })),
      ]);
      setBilling(data);
      setInvoice(inv);
      setInvoices(listed.invoices || []);
      setCheckouts(cos.checkouts || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load billing failed");
    }
  }

  async function onCreateInvoice() {
    setBusy(true);
    try {
      await createInvoice(billingDays, "draft");
      await loadBilling();
    } catch (err) {
      setError(err instanceof Error ? err.message : "create invoice failed");
    } finally {
      setBusy(false);
    }
  }

  async function onCheckout(invoiceId: string, dryRun?: boolean) {
    setBusy(true);
    try {
      const result = await createCheckout(invoiceId, dryRun);
      setLastCheckout(result);
      setError(null);
      if (result.checkout?.url && !result.checkout.dry_run) {
        window.open(result.checkout.url, "_blank", "noopener,noreferrer");
      }
      await loadBilling();
    } catch (err) {
      setError(err instanceof Error ? err.message : "checkout failed");
    } finally {
      setBusy(false);
    }
  }

  async function onSimulatePaid(sessionId: string) {
    setBusy(true);
    try {
      await simulateCheckoutPaid(sessionId);
      await loadBilling();
    } catch (err) {
      setError(err instanceof Error ? err.message : "simulate paid failed");
    } finally {
      setBusy(false);
    }
  }

  async function loadQuota() {
    try {
      const data = await getQuotaStatus();
      setQuota(data);
      setQuotaForm({
        max_tasks_per_day: data.limits.max_tasks_per_day,
        max_agent_runs_per_day: data.limits.max_agent_runs_per_day,
        max_concurrent_tasks: data.limits.max_concurrent_tasks,
        max_estimated_usd_per_month: data.limits.max_estimated_usd_per_month,
        enabled: data.limits.enabled,
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load quotas failed");
    }
  }

  async function loadEgress() {
    try {
      const data = await getEgressPolicy();
      setEgress(data);
      setEgressForm({
        mode: data.mode || "open",
        patterns: data.patterns || "",
        enabled: data.enabled,
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "load egress failed");
    }
  }

  useEffect(() => {
    if (tab === "keys") loadKeys();
    if (tab === "team") loadTeammates();
    if (tab === "rbac") loadRbac();
    if (tab === "audit") loadAudits();
    if (tab === "billing") loadBilling();
    if (tab === "quotas") loadQuota();
    if (tab === "egress") loadEgress();
    if (tab === "monitor") {
      getGatewayHealth()
        .then((h) => setHealth(h as unknown as Record<string, unknown>))
        .catch(() => setHealth(null));
    }
  }, [tab]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setCreatedPlain(null);
    try {
      const rec = await createApiKey(name.trim() || "api-key", { role });
      setCreatedPlain(rec.api_key || null);
      await loadKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : "create failed");
    } finally {
      setBusy(false);
    }
  }

  async function onInvite(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setInviteSecret(null);
    setInviteKey(null);
    try {
      const rec = await inviteTeammate({
        email: inviteEmail.trim(),
        display_name: inviteName.trim() || undefined,
        role: inviteRole,
        with_api_key: inviteWithKey,
      });
      setInviteSecret(rec.temporary_password);
      setInviteKey(rec.api_key?.api_key || null);
      setInviteEmail("");
      setInviteName("");
      await loadTeammates();
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "invite failed");
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke(id: string) {
    setBusy(true);
    try {
      await revokeApiKey(id);
      await loadKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : "revoke failed");
    } finally {
      setBusy(false);
    }
  }

  async function onProbe() {
    setBusy(true);
    try {
      const data = await probeHealth();
      setProbe(`probed ${data.probed}, online ${data.online}`);
      const h = await getGatewayHealth();
      setHealth(h as unknown as Record<string, unknown>);
    } catch (err) {
      setError(err instanceof Error ? err.message : "probe failed");
    } finally {
      setBusy(false);
    }
  }

  async function onSaveQuota(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await updateQuota(quotaForm);
      await loadQuota();
    } catch (err) {
      setError(err instanceof Error ? err.message : "save quota failed");
    } finally {
      setBusy(false);
    }
  }

  async function onSaveEgress(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await updateEgress(egressForm);
      await loadEgress();
    } catch (err) {
      setError(err instanceof Error ? err.message : "save egress failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-[calc(100vh-4rem)] flex-col lg:flex-row">
      <aside className="w-full shrink-0 border-b border-white/10 lg:w-52 lg:border-b-0 lg:border-r">
        <div className="flex items-end justify-between gap-3 px-4 py-4 lg:block lg:px-4 lg:py-5">
          <div>
            <h1 className="font-display text-xl text-mist-100 lg:text-2xl">设置</h1>
            <p className="mt-0.5 hidden font-mono text-[11px] text-mist-500 lg:block">
              访问 · 租户 · 计费 · 运维
            </p>
          </div>
        </div>
        <nav className="flex gap-4 overflow-x-auto px-3 pb-3 lg:block lg:space-y-4 lg:overflow-visible lg:px-2 lg:pb-4">
          {NAV_GROUPS.map((group) => (
            <div key={group.id} className="shrink-0 lg:shrink">
              <div className="px-1 pb-1 font-mono text-[10px] uppercase tracking-[0.16em] text-mist-500">
                {group.label}
              </div>
              <div className="flex gap-0.5 lg:flex-col">
                {group.items.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => selectTab(item.id)}
                    className={`rounded-lg px-2.5 py-1.5 text-left transition lg:w-full lg:py-2 ${
                      tab === item.id
                        ? "bg-signal/10 text-signal"
                        : "text-mist-300 hover:bg-white/5 hover:text-mist-100"
                    }`}
                  >
                    <div className="whitespace-nowrap font-mono text-[11px] uppercase tracking-[0.1em]">
                      {item.label}
                    </div>
                    <div className="mt-0.5 hidden font-mono text-[10px] text-mist-500 lg:block">
                      {item.hint}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>
      </aside>

      <section className="min-w-0 flex-1 px-4 py-5 md:px-8 md:py-6">
        <div className="mb-5 max-w-3xl">
          <div className="font-display text-xl text-mist-100">{meta.label}</div>
          <p className="mt-1 text-sm text-mist-400">{meta.hint}</p>
          {error ? (
            <div className="mt-3 font-mono text-xs text-signal-warm">{error}</div>
          ) : null}
        </div>

        <div className="max-w-3xl">
        {tab === "tenant" ? <TenantPanel /> : null}

        {tab === "governance" ? (
          <GovernancePanel initialRootTaskId={rootFromUrl} />
        ) : null}

        {tab === "keys" ? (
          <div className="space-y-5">
            <form
              onSubmit={onCreate}
              className="flex flex-wrap items-end gap-3 rounded-xl border border-white/10 bg-ink-900/40 p-4"
            >
              <label className="block min-w-[10rem] flex-1">
                <span className="font-mono text-[10px] uppercase text-mist-400">名称</span>
                <input
                  id="settings-key-name"
                  name="api-key-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
                />
              </label>
              <label className="block min-w-[8rem]">
                <span className="font-mono text-[10px] uppercase text-mist-400">角色</span>
                <select
                  id="settings-key-role"
                  name="api-key-role"
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
                >
                  {roles.map((r) => (
                    <option key={r.role} value={r.role}>
                      {r.role}
                    </option>
                  ))}
                </select>
                <p className="mt-1 max-w-[16rem] text-[11px] text-mist-500">
                  {ROLE_HINTS[role] || (roles.find((r) => r.role === role)?.scopes || []).join(" · ")}
                </p>
              </label>
              <button
                type="submit"
                disabled={busy}
                className="rounded-xl bg-signal px-4 py-2 font-mono text-xs font-semibold uppercase text-ink-950 disabled:opacity-40"
              >
                创建
              </button>
            </form>
            {createdPlain ? (
              <div className="rounded-xl border border-signal/30 bg-signal/10 p-3 font-mono text-xs text-signal">
                明文密钥（仅显示一次）：{createdPlain}
              </div>
            ) : null}
            <div className="overflow-hidden rounded-xl border border-white/10">
              {keys.length === 0 ? (
                <p className="px-4 py-8 text-center font-mono text-xs text-mist-500">暂无 API Key</p>
              ) : (
                <div className="divide-y divide-white/10">
                  {keys.map((k) => (
                    <div
                      key={k.id}
                      className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
                    >
                      <div className="min-w-0">
                        <div className="text-sm text-mist-100">{k.name}</div>
                        <div className="mt-0.5 truncate font-mono text-[11px] text-mist-400">
                          {k.key_prefix}… · {k.status}
                          {(k.scopes || []).length
                            ? ` · ${(k.scopes || []).slice(0, 4).join(", ")}`
                            : ""}
                        </div>
                      </div>
                      <button
                        type="button"
                        disabled={busy || k.status === "revoked"}
                        onClick={() => onRevoke(k.id)}
                        className="shrink-0 rounded-lg border border-white/15 px-3 py-1 font-mono text-[10px] uppercase text-mist-300 disabled:opacity-40"
                      >
                        撤销
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : null}

        {tab === "team" ? (
          <div className="space-y-5">
            <form
              onSubmit={onInvite}
              className="space-y-3 rounded-xl border border-white/10 bg-ink-900/40 p-4"
            >
              <p className="text-sm text-mist-400">
                邀请同事用邮箱登录 Console。临时密码只显示一次；可选同时发一把同角色 API Key。
              </p>
              <div className="flex flex-wrap gap-3">
                <label className="block min-w-[12rem] flex-1">
                  <span className="font-mono text-[10px] uppercase text-mist-400">Email</span>
                  <input
                    type="email"
                    required
                    value={inviteEmail}
                    onChange={(e) => setInviteEmail(e.target.value)}
                    className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
                    placeholder="sam@company.com"
                  />
                </label>
                <label className="block min-w-[8rem] flex-1">
                  <span className="font-mono text-[10px] uppercase text-mist-400">显示名</span>
                  <input
                    value={inviteName}
                    onChange={(e) => setInviteName(e.target.value)}
                    className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
                  />
                </label>
                <label className="block min-w-[8rem]">
                  <span className="font-mono text-[10px] uppercase text-mist-400">角色</span>
                  <select
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value)}
                    className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
                  >
                    {roles.map((r) => (
                      <option key={r.role} value={r.role}>
                        {r.role}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <p className="text-[11px] text-mist-500">{ROLE_HINTS[inviteRole] || ""}</p>
              <label className="flex items-center gap-2 text-sm text-mist-300">
                <input
                  type="checkbox"
                  checked={inviteWithKey}
                  onChange={(e) => setInviteWithKey(e.target.checked)}
                />
                同时创建 API Key（脚本/CI 用）
              </label>
              <button
                type="submit"
                disabled={busy || !inviteEmail.trim()}
                className="rounded-xl bg-signal px-4 py-2 font-mono text-xs font-semibold uppercase text-ink-950 disabled:opacity-40"
              >
                邀请
              </button>
            </form>
            {inviteSecret ? (
              <div className="space-y-1 rounded-xl border border-signal/30 bg-signal/10 p-3 font-mono text-xs text-signal">
                <div>临时密码（只显示一次）：{inviteSecret}</div>
                {inviteKey ? <div>API Key（只显示一次）：{inviteKey}</div> : null}
                <div className="text-mist-400">请立刻发给对方，登录页 /login</div>
              </div>
            ) : null}
            <div className="overflow-hidden rounded-xl border border-white/10">
              {teammates.length === 0 ? (
                <p className="px-4 py-8 text-center font-mono text-xs text-mist-500">暂无同事</p>
              ) : (
                <div className="divide-y divide-white/10">
                  {teammates.map((u) => (
                    <div key={u.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                      <div>
                        <div className="text-sm text-mist-100">{u.display_name || u.email}</div>
                        <div className="mt-0.5 font-mono text-[11px] text-mist-400">
                          {u.email} · {u.role} · {u.status || "active"}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : null}

        {tab === "rbac" ? (
          <div className="space-y-5">
            <div className="overflow-hidden rounded-2xl border border-white/10">
              <table className="w-full text-left text-sm">
                <thead className="bg-ink-900/80 font-mono text-[10px] uppercase tracking-[0.14em] text-mist-400">
                  <tr>
                    <th className="px-4 py-3">Role</th>
                    <th className="px-4 py-3">Scopes</th>
                  </tr>
                </thead>
                <tbody>
                  {roles.map((r) => (
                    <tr key={r.role} className="border-t border-white/10">
                      <td className="px-4 py-3 font-mono text-signal-dim">{r.role}</td>
                      <td className="px-4 py-3 font-mono text-xs text-mist-300">
                        {(r.scopes || []).join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="border-t border-white/10 px-4 py-3 text-xs text-mist-400">
                创建 API Key 时可选择角色；网关鉴权时展开为具体 scopes。
              </p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-4">
              <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
                当前身份 /v1/rbac/me
              </div>
              <pre className="mt-2 overflow-auto font-mono text-[11px] text-mist-300">
                {JSON.stringify(me, null, 2) || "—"}
              </pre>
            </div>
          </div>
        ) : null}

        {tab === "billing" ? (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-2">
              {[7, 30, 90].map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => {
                    setBillingDays(d);
                    loadBilling(d);
                  }}
                  className={`rounded-lg px-3 py-1.5 font-mono text-[11px] uppercase ${
                    billingDays === d
                      ? "bg-signal/15 text-signal"
                      : "border border-white/10 text-mist-400"
                  }`}
                >
                  {d}d
                </button>
              ))}
              <button
                type="button"
                onClick={() => loadBilling()}
                className="ml-auto font-mono text-[10px] uppercase text-signal"
              >
                刷新
              </button>
            </div>
            {billing ? (
              <>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-4">
                    <div className="font-mono text-[10px] uppercase text-mist-400">
                      预估合计
                    </div>
                    <div className="mt-2 font-mono text-2xl text-signal">
                      {usd(billing.estimated_total_usd)}
                    </div>
                    <div className="mt-1 font-mono text-[10px] text-mist-500">
                      {billing.currency} · {billing.window_days}d
                    </div>
                  </div>
                  <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-4">
                    <div className="font-mono text-[10px] uppercase text-mist-400">
                      Tasks
                    </div>
                    <div className="mt-2 font-mono text-2xl text-mist-100">
                      {billing.tasks?.total ?? 0}
                    </div>
                    <div className="mt-1 font-mono text-[10px] text-mist-500">
                      {usd(billing.task_cost_usd)} · done{" "}
                      {billing.tasks?.completed ?? 0}
                    </div>
                  </div>
                  <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-4">
                    <div className="font-mono text-[10px] uppercase text-mist-400">
                      Agent Runs
                    </div>
                    <div className="mt-2 font-mono text-2xl text-mist-100">
                      {billing.agent_runs?.total ?? 0}
                    </div>
                    <div className="mt-1 font-mono text-[10px] text-mist-500">
                      {usd(billing.agent_runs?.estimated_cost_usd)}
                    </div>
                  </div>
                </div>
                <div className="overflow-hidden rounded-2xl border border-white/10">
                  <div className="border-b border-white/10 px-4 py-3 font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
                    By Agent
                  </div>
                  {(billing.agent_runs?.by_agent_key || []).length === 0 ? (
                    <p className="px-4 py-8 text-center text-sm text-mist-400">
                      窗口内无 agent runs
                    </p>
                  ) : (
                    <table className="w-full text-left text-sm">
                      <thead className="bg-ink-900/80 font-mono text-[10px] uppercase text-mist-400">
                        <tr>
                          <th className="px-4 py-2">Agent</th>
                          <th className="px-4 py-2">Runs</th>
                          <th className="px-4 py-2">Unit</th>
                          <th className="px-4 py-2">Est.</th>
                        </tr>
                      </thead>
                      <tbody>
                        {billing.agent_runs.by_agent_key.map((row) => (
                          <tr key={row.agent_key} className="border-t border-white/10">
                            <td className="px-4 py-2 font-mono text-xs text-signal-dim">
                              {row.agent_key}
                            </td>
                            <td className="px-4 py-2 font-mono text-xs text-mist-300">
                              {row.runs}
                            </td>
                            <td className="px-4 py-2 font-mono text-xs text-mist-400">
                              {usd(row.unit_price_usd)}
                            </td>
                            <td className="px-4 py-2 font-mono text-xs text-mist-200">
                              {usd(row.estimated_cost_usd)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
                <p className="text-xs text-mist-500">{billing.note}</p>
                {invoice ? (
                  <div className="space-y-3 rounded-2xl border border-white/10 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
                        Invoice Preview {invoice.invoice_number}
                      </div>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={onCreateInvoice}
                        className="ml-auto rounded-lg border border-signal/30 px-3 py-1 font-mono text-[10px] uppercase text-signal disabled:opacity-40"
                      >
                        保存快照
                      </button>
                      <a
                        href={invoiceMarkdownUrl(billingDays)}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded-lg border border-white/15 px-3 py-1 font-mono text-[10px] uppercase text-mist-300"
                      >
                        Markdown
                      </a>
                    </div>
                    <div className="font-mono text-sm text-mist-100">
                      Total {usd(invoice.total_usd)} · {invoice.line_items?.length || 0} lines
                    </div>
                    <div className="font-mono text-[10px] text-mist-500">
                      Stripe checkout ready:{" "}
                      {invoice.stripe?.checkout_ready ? "yes" : "no"} · secret{" "}
                      {invoice.stripe?.secret_configured ? "set" : "unset"}
                    </div>
                    {lastCheckout ? (
                      <div className="rounded-xl border border-white/10 bg-ink-950 p-3 font-mono text-[11px] text-mist-300">
                        Last checkout: {lastCheckout.checkout.id} ·{" "}
                        {lastCheckout.checkout.dry_run ? "dry-run" : "live"} ·{" "}
                        {lastCheckout.checkout.url ? (
                          <a
                            href={lastCheckout.checkout.url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-signal"
                          >
                            open
                          </a>
                        ) : (
                          "no url"
                        )}
                      </div>
                    ) : null}
                    {invoices.length > 0 ? (
                      <div className="divide-y divide-white/10 border-t border-white/10 pt-2">
                        {invoices.map((inv) => (
                          <div
                            key={inv.id || inv.invoice_number}
                            className="flex flex-wrap items-center justify-between gap-2 py-2 font-mono text-[11px] text-mist-400"
                          >
                            <span>
                              {inv.invoice_number} · {usd(inv.total_usd)} · {inv.status}
                            </span>
                            {inv.id && (inv.total_usd || 0) > 0 ? (
                              <span className="flex gap-1">
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => onCheckout(inv.id!, true)}
                                  className="rounded border border-white/15 px-2 py-0.5 uppercase text-mist-300 disabled:opacity-40"
                                >
                                  Dry-run
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => onCheckout(inv.id!)}
                                  className="rounded border border-signal/30 px-2 py-0.5 uppercase text-signal disabled:opacity-40"
                                >
                                  Checkout
                                </button>
                              </span>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {checkouts.length > 0 ? (
                      <div className="space-y-2 border-t border-white/10 pt-3">
                        <div className="font-mono text-[10px] uppercase text-mist-400">
                          Checkout Sessions
                        </div>
                        {checkouts.map((c) => (
                          <div
                            key={c.id}
                            className="flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-mist-400"
                          >
                            <span>
                              {c.stripe_session_id.slice(0, 24)}… · {c.status}
                              {c.paid_at ? " · paid" : ""}
                            </span>
                            {c.status !== "paid" ? (
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => onSimulatePaid(c.stripe_session_id)}
                                className="rounded border border-white/15 px-2 py-0.5 uppercase text-mist-300 disabled:opacity-40"
                              >
                                Mark paid
                              </button>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </>
            ) : (
              <p className="text-sm text-mist-400">加载中…</p>
            )}
          </div>
        ) : null}

        {tab === "quotas" ? (
          <div className="space-y-5">
            {quota ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-4">
                  <div className="font-mono text-[10px] uppercase text-mist-400">
                    Today / Concurrent
                  </div>
                  <div className="mt-2 font-mono text-sm text-mist-100">
                    tasks {quota.usage.tasks_today}/{quota.limits.max_tasks_per_day}
                  </div>
                  <div className="mt-1 font-mono text-sm text-mist-100">
                    runs {quota.usage.agent_runs_today}/{quota.limits.max_agent_runs_per_day}
                  </div>
                  <div className="mt-1 font-mono text-sm text-mist-100">
                    concurrent {quota.usage.concurrent_tasks}/
                    {quota.limits.max_concurrent_tasks}
                  </div>
                </div>
                <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-4">
                  <div className="font-mono text-[10px] uppercase text-mist-400">
                    30d Est. USD
                  </div>
                  <div className="mt-2 font-mono text-2xl text-signal">
                    ${quota.usage.estimated_usd_30d.toFixed(4)}
                  </div>
                  <div className="mt-1 font-mono text-[10px] text-mist-500">
                    limit ${quota.limits.max_estimated_usd_per_month.toFixed(2)} ·
                    enforcement {quota.enforcement ? "on" : "off"}
                  </div>
                </div>
              </div>
            ) : (
              <p className="text-sm text-mist-400">加载中…</p>
            )}
            <form onSubmit={onSaveQuota} className="space-y-3 rounded-2xl border border-white/10 p-4">
              <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
                Edit Limits
              </div>
              {(
                [
                  ["max_tasks_per_day", "Tasks / day"],
                  ["max_agent_runs_per_day", "Runs / day"],
                  ["max_concurrent_tasks", "Concurrent"],
                  ["max_estimated_usd_per_month", "USD / 30d"],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="block">
                  <span className="font-mono text-[10px] uppercase text-mist-400">{label}</span>
                  <input
                    id={`settings-quota-${key}`}
                    name={key}
                    type="number"
                    step={key.includes("usd") ? "0.01" : "1"}
                    value={quotaForm[key]}
                    onChange={(e) =>
                      setQuotaForm((f) => ({
                        ...f,
                        [key]: key.includes("usd")
                          ? Number(e.target.value)
                          : parseInt(e.target.value || "0", 10),
                      }))
                    }
                    className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
                  />
                </label>
              ))}
              <label className="flex items-center gap-2 font-mono text-xs text-mist-300">
                <input
                  id="settings-quota-enabled"
                  name="quota-enabled"
                  type="checkbox"
                  checked={quotaForm.enabled}
                  onChange={(e) =>
                    setQuotaForm((f) => ({ ...f, enabled: e.target.checked }))
                  }
                />
                enabled
              </label>
              <button
                type="submit"
                disabled={busy}
                className="rounded-xl bg-signal px-4 py-2 font-mono text-xs font-semibold uppercase text-ink-950 disabled:opacity-40"
              >
                保存
              </button>
            </form>
          </div>
        ) : null}

        {tab === "egress" ? (
          <form onSubmit={onSaveEgress} className="space-y-4 rounded-2xl border border-white/10 p-4">
            <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
              Browser 出站 · enforcement {egress?.enforcement ? "on" : "off"}
            </div>
            <p className="text-xs text-mist-500">
              作用于 skill <span className="font-mono">browser-automation</span> 文本中的 URL。
              模式 open / allowlist / denylist。通配 <span className="font-mono">*.example.com</span>。
            </p>
            <label className="block">
              <span className="font-mono text-[10px] uppercase text-mist-400">Mode</span>
              <select
                id="settings-egress-mode"
                name="egress-mode"
                value={egressForm.mode}
                onChange={(e) => setEgressForm((f) => ({ ...f, mode: e.target.value }))}
                className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
              >
                <option value="open">open</option>
                <option value="allowlist">allowlist</option>
                <option value="denylist">denylist</option>
              </select>
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase text-mist-400">Patterns</span>
              <input
                id="settings-egress-patterns"
                name="egress-patterns"
                value={egressForm.patterns}
                onChange={(e) => setEgressForm((f) => ({ ...f, patterns: e.target.value }))}
                placeholder="example.com, *.wikipedia.org"
                className="mt-1 block w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2 text-sm text-mist-100"
              />
            </label>
            <label className="flex items-center gap-2 font-mono text-xs text-mist-300">
              <input
                id="settings-egress-enabled"
                name="egress-enabled"
                type="checkbox"
                checked={egressForm.enabled}
                onChange={(e) => setEgressForm((f) => ({ ...f, enabled: e.target.checked }))}
              />
              enabled
            </label>
            <button
              type="submit"
              disabled={busy}
              className="rounded-xl bg-signal px-4 py-2 font-mono text-xs font-semibold uppercase text-ink-950 disabled:opacity-40"
            >
              保存
            </button>
          </form>
        ) : null}

        {tab === "monitor" ? (
          <div className="space-y-4 rounded-2xl border border-white/10 bg-ink-900/50 p-5">
            <ChaosPlaybook steps={preflight?.recover_steps} />
            <button
              type="button"
              disabled={busy}
              onClick={onProbe}
              className="rounded-xl border border-signal/30 px-4 py-2 font-mono text-xs uppercase tracking-[0.14em] text-signal"
            >
              运行 Health Probe
            </button>
            {probe ? <div className="font-mono text-xs text-mist-200">{probe}</div> : null}
            <pre className="overflow-auto rounded-xl border border-white/10 bg-ink-950 p-3 font-mono text-[11px] text-mist-300">
              {JSON.stringify(health, null, 2) || "—"}
            </pre>
          </div>
        ) : null}

        {tab === "storage" ? (
          <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-5 text-sm text-mist-300">
            <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
              MinIO（开发默认）
            </div>
            <ul className="mt-3 space-y-1 font-mono text-xs">
              <li>Endpoint: 127.0.0.1:9000</li>
              <li>Bucket: aop-artifacts</li>
              <li>Path: s3://aop-artifacts/tasks/{"{task_id}"}/{"{node}"}/…</li>
              <li>Console: http://127.0.0.1:9001</li>
            </ul>
          </div>
        ) : null}

        {tab === "audit" ? (
          <div className="overflow-hidden rounded-2xl border border-white/10">
            <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
              <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-mist-400">
                Audit Logs
              </div>
              <button
                type="button"
                onClick={loadAudits}
                className="font-mono text-[10px] uppercase text-signal"
              >
                刷新
              </button>
            </div>
            {audits.length === 0 ? (
              <p className="px-4 py-8 text-center text-sm text-mist-400">暂无审计记录</p>
            ) : (
              <div className="divide-y divide-white/10">
                {audits.map((a) => (
                  <div key={a.id} className="px-4 py-3">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="text-sm text-mist-100">
                        {a.summary || a.action}
                      </span>
                      <span className="font-mono text-[10px] text-mist-500">
                        {a.created_at || ""}
                      </span>
                    </div>
                    <div className="mt-1 font-mono text-[11px] text-mist-500">
                      {a.action}
                      {a.resource_type ? ` · ${a.resource_type}` : ""}
                      {a.ip ? ` · ${a.ip}` : ""}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : null}
        </div>
      </section>
    </div>
  );
}
