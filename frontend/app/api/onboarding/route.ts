import { NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// One round trip for the onboarding quest widget: fans out to the counts the steps derive from.
// Same proxy idiom as every other app/api route — TRACELY_KEY/TRACELY_API never reach the
// browser. Every leg degrades to null: a flaky backend must not break the shell.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET() {
  const headers = await authHeaders();
  const get = async (path: string): Promise<any> => {
    try {
      const r = await fetch(`${API}${path}`, { headers, cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch {
      return null;
    }
  };
  const [stats, evaluators, gates, llm, me, trends, sessions, milestones] = await Promise.all([
    get("/api/stats"),
    get("/api/evaluators"),
    get("/api/gates?limit=1"),
    get("/api/project/llm-key"),
    get("/auth/me"),
    get("/api/trends?days=2"),
    get("/api/sessions?limit=1"),
    get("/api/onboarding/milestones"),
  ]);
  // UTC day-key, matching the backend's trends buckets — the client uses the same convention
  const today = new Date().toISOString().slice(0, 10);
  const todayRow = trends?.daily?.find((d: { date: string }) => d.date === today);
  return NextResponse.json({
    traces: stats?.traces ?? 0,
    failures: stats?.auto_failures ?? 0,
    clusters: stats?.open_clusters ?? 0,
    cases: stats?.cases ?? 0,
    evaluators: Array.isArray(evaluators) ? evaluators.length : 0,
    gates: gates?.total ?? 0,
    llm_key: Boolean(llm?.configured),
    // the user's own ingest key (already shown to them on /settings/api-keys) — NOT TRACELY_KEY
    ingest_key: me?.ingest_keys?.[0] ?? null,
    endpoint: process.env.NEXT_PUBLIC_TRACELY_PUBLIC_API ?? "http://localhost:8000",
    traces_today: todayRow?.traces ?? 0,
    failures_today: todayRow?.failures ?? 0,
    gate_today: Boolean(gates?.items?.[0]?.created_at?.startsWith(today)),
    thread_id: sessions?.[0]?.thread ?? null,
    // durable milestones (W6): the quest's outcome steps read these, sample rows excluded
    milestones: milestones?.items ?? [],
  });
}
