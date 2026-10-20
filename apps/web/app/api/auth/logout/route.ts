import { NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  gatewayBase,
  readSessionCookie,
  sessionCookieOptions,
} from "@/lib/serverGateway";

export async function POST() {
  const token = await readSessionCookie();
  if (token) {
    try {
      await fetch(`${gatewayBase()}/v1/auth/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
    } catch {
      /* still clear cookie */
    }
  }
  const res = NextResponse.json({ logged_out: true });
  res.cookies.set(SESSION_COOKIE, "", { ...sessionCookieOptions(0), maxAge: 0 });
  return res;
}
