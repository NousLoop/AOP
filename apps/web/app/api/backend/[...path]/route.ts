import { NextRequest, NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  gatewayBase,
  upstreamAuthFromCookie,
} from "@/lib/serverGateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

async function proxy(req: NextRequest, pathParts: string[]) {
  const path = pathParts.map(encodeURIComponent).join("/");
  const url = new URL(req.url);
  const target = `${gatewayBase()}/${path}${url.search}`;

  const headers = new Headers();
  const pass = [
    "content-type",
    "accept",
    "accept-language",
    "x-request-id",
    "x-idempotency-key",
  ];
  for (const name of pass) {
    const v = req.headers.get(name);
    if (v) headers.set(name, v);
  }

  const cookieToken = req.cookies.get(SESSION_COOKIE)?.value;
  const auth = upstreamAuthFromCookie(cookieToken);
  if (auth) {
    headers.set("Authorization", `Bearer ${auth}`);
    if (!auth.startsWith("aop_sess_")) {
      headers.set("X-API-Key", auth);
    }
  }

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: "no-store",
    redirect: "manual",
  };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.arrayBuffer();
  }

  const upstream = await fetch(target, init);
  const outHeaders = new Headers();
  const forward = ["content-type", "content-disposition", "cache-control"];
  for (const name of forward) {
    const v = upstream.headers.get(name);
    if (v) outHeaders.set(name, v);
  }
  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: outHeaders,
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(req: NextRequest, ctx: Ctx) {
  const { path } = await ctx.params;
  if (!path?.length) {
    return NextResponse.json({ error: "path required" }, { status: 400 });
  }
  // Login must go through /api/auth/login so the token never hits the browser.
  if (path[0] === "v1" && path[1] === "auth" && path[2] === "login") {
    return NextResponse.json(
      { error: "use POST /api/auth/login" },
      { status: 400 },
    );
  }
  try {
    return await proxy(req, path);
  } catch (err) {
    const message = err instanceof Error ? err.message : "proxy failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
export const HEAD = handle;
