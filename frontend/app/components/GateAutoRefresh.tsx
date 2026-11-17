"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** A simulated gate runs async — real HTTP against the customer's agent, then grading. The server
 *  component renders whatever the row says right now, so this re-fetches it until the run lands.
 *  Mounted only while `finished_at` is null, so a settled gate costs nothing. */
export function GateAutoRefresh({
  everyMs = 4000,
  label = "driving conversations…",
}: {
  everyMs?: number;
  /** What is in progress — the same poller serves audit runs. */
  label?: string;
}) {
  const router = useRouter();
  useEffect(() => {
    const t = setInterval(() => router.refresh(), everyMs);
    return () => clearInterval(t);
  }, [router, everyMs]);

  return (
    <span className="flex items-center gap-2 font-mono text-[11.5px] text-info">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-info" />
      {label}
    </span>
  );
}
