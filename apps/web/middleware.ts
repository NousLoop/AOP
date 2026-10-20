import { NextRequest, NextResponse } from "next/server";

const SESSION_COOKIE = "aop_session";

function authRequired(): boolean {
  const v = (
    process.env.AUTH_REQUIRED ||
    process.env.NEXT_PUBLIC_AUTH_REQUIRED ||
    ""
  )
    .trim()
    .toLowerCase();
  return v === "1" || v === "true" || v === "yes" || v === "on";
}

export function middleware(req: NextRequest) {
  if (!authRequired()) {
    return NextResponse.next();
  }

  const { pathname } = req.nextUrl;
  if (
    pathname === "/login" ||
    pathname.startsWith("/api/auth/") ||
    pathname.startsWith("/_next/") ||
    pathname === "/favicon.ico" ||
    pathname === "/favicon.svg" ||
    pathname === "/logo.svg" ||
    pathname === "/icon.svg"
  ) {
    return NextResponse.next();
  }

  // API routes return 401 from Gateway/BFF; pages need a session cookie.
  if (pathname.startsWith("/api/")) {
    return NextResponse.next();
  }

  const session = req.cookies.get(SESSION_COOKIE)?.value;
  if (!session) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image).*)"],
};
