import { NextRequest, NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  gatewayBase,
  sessionCookieOptions,
} from "@/lib/serverGateway";

export async function POST(req: NextRequest) {
  let body: { email?: string; password?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }
  const email = (body.email || "").trim();
  const password = body.password || "";
  if (!email || !password) {
    return NextResponse.json(
      { error: "email and password required" },
      { status: 400 },
    );
  }

  const upstream = await fetch(`${gatewayBase()}/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });
  const text = await upstream.text();
  let data: Record<string, unknown> = {};
  try {
    data = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  } catch {
    data = { raw: text };
  }
  if (!upstream.ok) {
    return NextResponse.json(data, { status: upstream.status });
  }

  const token = typeof data.token === "string" ? data.token : "";
  if (!token) {
    return NextResponse.json(
      { error: "login response missing token" },
      { status: 502 },
    );
  }

  let maxAge = 60 * 60 * 24;
  if (typeof data.expires_at === "string") {
    const exp = Date.parse(data.expires_at);
    if (!Number.isNaN(exp)) {
      maxAge = Math.max(60, Math.floor((exp - Date.now()) / 1000));
    }
  }

  // Never return the raw session token to the browser — cookie only.
  const { token: _omit, ...safe } = data;
  const res = NextResponse.json(safe);
  res.cookies.set(SESSION_COOKIE, token, sessionCookieOptions(maxAge));
  return res;
}
