import { NextResponse } from "next/server";
import type { NextFetchEvent, NextRequest } from "next/server";

// One guard, branching on the build-time auth mode. The Clerk path is dynamically imported inside the
// handler so local/dev runs never load Clerk (clerkMiddleware throws without keys). Node runtime — do
// NOT set runtime="edge" (the dynamic import relies on it).
const MODE = process.env.NEXT_PUBLIC_AUTH_MODE ?? "dev";
const SESSION_COOKIE = "tracely_session";

const PUBLIC = [
  /^\/$/, // the marketing landing page
  // The rest of the (marketing) group — every route app/sitemap.ts advertises. Behind the wall they
  // 307 to /login, so Google indexes a login page at a URL we told it to crawl.
  /^\/(llm-evaluation|llm-as-a-judge|langfuse-alternatives|agent-skill)$/,
  /^\/login/,
  /^\/register/,
  /^\/accept-invite/,
  // Password recovery has to be reachable while signed OUT — that is the entire point of it.
  /^\/forgot-password/,
  /^\/reset-password/,
  /^\/sign-in/,
  /^\/sign-up/,
  /^\/share\//, // public conversation links — the token in the path is the credential
  /^\/api\/share\/[^/]+\/replay/, // the share page's fleet replay fetch — same credential model
  // Crawler-facing files. Behind the wall they 307 to /login, so Google indexes a login page and
  // Slack unfurls nothing — the sitemap silently never gets read.
  /^\/(sitemap\.xml|robots\.txt|opengraph-image)/,
  /^\/api\/health/,
  /^\/api\/auth\//,
  // PostHog reverse proxy (next.config.mjs). Behind the wall a logged-out visitor's flags/recorder/
  // event calls 307 to /login — no pageviews and no session replay for the whole marketing funnel.
  /^\/ingest\//,
];
const isPublic = (p: string) => PUBLIC.some((re) => re.test(p));

export default async function middleware(req: NextRequest, ev: NextFetchEvent) {
  if (MODE === "clerk") {
    const { clerkMiddleware, createRouteMatcher } = await import("@clerk/nextjs/server");
    // Keep in sync with PUBLIC above — Clerk mode has its own matcher, so a route added to only
    // one of the two lists is public in local mode and a redirect loop in Clerk mode.
    const isPublicClerk = createRouteMatcher([
      "/",
      "/llm-evaluation",
      "/llm-as-a-judge",
      "/langfuse-alternatives",
      "/agent-skill",
      "/sign-in(.*)",
      "/sign-up(.*)",
      "/share/(.*)",
      "/api/share/(.*)/replay",
      "/sitemap.xml",
      "/robots.txt",
      "/opengraph-image(.*)",
      "/api/health",
      "/api/auth/(.*)",
      "/ingest/(.*)",
    ]);
    return clerkMiddleware(async (auth, r) => {
      if (!isPublicClerk(r)) await auth.protect();
    })(req, ev);
  }

  if (MODE === "local") {
    const { pathname } = req.nextUrl;
    if (isPublic(pathname)) return NextResponse.next();
    if (!req.cookies.get(SESSION_COOKIE)?.value) {
      // API calls get a clean 401 (the client fetches degrade gracefully); pages redirect to /login.
      if (pathname.startsWith("/api/")) {
        return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
      }
      const url = req.nextUrl.clone();
      url.pathname = "/login";
      url.searchParams.set("next", pathname);
      return NextResponse.redirect(url);
    }
    return NextResponse.next();
  }

  return NextResponse.next(); // dev: open, no auth wall
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|woff2?|ttf)).*)",
  ],
};
