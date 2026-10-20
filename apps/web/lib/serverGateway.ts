import { cookies } from "next/headers";

/** HttpOnly session cookie set by /api/auth/login (Next origin). */
export const SESSION_COOKIE = "aop_session";

export function gatewayBase(): string {
  return (
    process.env.AOP_GATEWAY_URL ||
    process.env.NEXT_PUBLIC_API_BASE ||
    "http://127.0.0.1:8080"
  ).replace(/\/$/, "");
}

/** Prefer user session; fall back to server-side key (never NEXT_PUBLIC_*). */
export function upstreamAuthFromCookie(cookieToken: string | undefined): string {
  const session = (cookieToken || "").trim();
  if (session) return session;
  return (
    process.env.AOP_API_KEY ||
    process.env.GATEWAY_API_KEY ||
    ""
  ).trim();
}

export async function readSessionCookie(): Promise<string> {
  const jar = await cookies();
  return (jar.get(SESSION_COOKIE)?.value || "").trim();
}

export function sessionCookieOptions(maxAgeSec: number) {
  const secure =
    process.env.COOKIE_SECURE === "true" ||
    process.env.NODE_ENV === "production";
  return {
    httpOnly: true,
    secure,
    sameSite: "lax" as const,
    path: "/",
    maxAge: Math.max(60, maxAgeSec),
  };
}
