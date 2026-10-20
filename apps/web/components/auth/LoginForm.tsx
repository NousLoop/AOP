"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { clearLegacySessionToken, login } from "@/lib/api";

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("admin@aop.local");
  const [password, setPassword] = useState("aop_admin_dev");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    clearLegacySessionToken();
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      const next = searchParams.get("next");
      router.replace(next && next.startsWith("/") ? next : "/");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-[70vh] items-center justify-center px-4 py-12">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-ink-900/60 p-8 shadow-xl">
        <div className="flex items-center gap-3">
          <img src="/logo.svg" alt="" width={72} height={72} className="h-[72px] w-[72px]" />
          <div className="font-display text-3xl text-mist-100">A2A OS</div>
        </div>
        <p className="mt-1 font-mono text-[11px] uppercase tracking-[0.18em] text-mist-400">
          Control plane login
        </p>
        <form onSubmit={onSubmit} className="mt-8 space-y-4">
          <label className="block">
            <span className="font-mono text-[10px] uppercase text-mist-400">Email</span>
            <input
              id="login-email"
              name="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              className="mt-1 w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2.5 text-sm text-mist-100"
              required
            />
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase text-mist-400">Password</span>
            <input
              id="login-password"
              name="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              className="mt-1 w-full rounded-xl border border-white/10 bg-ink-800/60 px-3 py-2.5 text-sm text-mist-100"
              required
            />
          </label>
          {error ? (
            <div className="rounded-lg border border-signal-warm/30 bg-signal-warm/10 px-3 py-2 font-mono text-xs text-signal-warm">
              {error}
            </div>
          ) : null}
          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-xl bg-signal py-2.5 font-mono text-xs font-semibold uppercase tracking-[0.14em] text-ink-950 disabled:opacity-40"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="mt-6 text-xs text-mist-500">
          开发默认：admin@aop.local / aop_admin_dev（可用 SEED_ADMIN_PASSWORD 覆盖）。
          会话写入 HttpOnly Cookie（经 /api/auth/login），浏览器 JS 读不到 token。
          服务端可用 AOP_API_KEY；生产请设 AUTH_REQUIRED=true。
        </p>
        <Link href="/" className="mt-4 inline-block font-mono text-[11px] text-signal">
          ← 返回控制台
        </Link>
      </div>
    </div>
  );
}
