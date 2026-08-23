"use client";

import posthog from "posthog-js";
import { Suspense, useEffect } from "react";
import { usePathname, useSearchParams } from "next/navigation";

// Product analytics for the funnel: landing → signup → first key → first trace. Wired only when
// NEXT_PUBLIC_POSTHOG_KEY is set at build time — self-hosters without a key ship zero analytics
// code paths. Events go through our own /ingest rewrite (next.config.mjs) so ad blockers that
// kill *.posthog.com don't blind the funnel.
// ponytail: pageviews + autocapture only; add explicit capture() calls when a question needs one.

const KEY = process.env.NEXT_PUBLIC_POSTHOG_KEY;

if (typeof window !== "undefined" && KEY && !posthog.__loaded) {
  posthog.init(KEY, {
    api_host: "/ingest",
    ui_host: "https://us.posthog.com",
    capture_pageview: false, // captured manually below — app-router navigations aren't page loads
    capture_pageleave: true,
    persistence: "localStorage+cookie",
  });
}

function Pageviews() {
  const pathname = usePathname();
  const search = useSearchParams();
  useEffect(() => {
    if (!KEY || !pathname) return;
    const qs = search?.toString();
    posthog.capture("$pageview", { $current_url: window.origin + pathname + (qs ? `?${qs}` : "") });
  }, [pathname, search]);
  return null;
}

export function PostHogProvider({ children }: { children: React.ReactNode }) {
  if (!KEY) return <>{children}</>;
  return (
    <>
      {/* useSearchParams needs a Suspense boundary during static rendering */}
      <Suspense fallback={null}>
        <Pageviews />
      </Suspense>
      {children}
    </>
  );
}
