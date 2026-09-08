"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, verdictVariant } from "./ui";
import type { CaseCandidate } from "@/app/lib/api";

/**
 * Candidate selection (W5 §4): the exact compatible runs (same input) first; pasting a trace id
 * stays as the advanced path. Verifying re-grades the candidate through the shared contract and
 * lands the page on the comparison for it.
 */
export function CandidatePicker({ caseId, candidates, selected }: { caseId: string; candidates: CaseCandidate[]; selected?: string }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [manual, setManual] = useState("");
  const [showManual, setShowManual] = useState(false);
  const router = useRouter();
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;

  async function verify(traceId: string) {
    setBusy(traceId);
    setErr(null);
    try {
      const r = await fetch("/api/replay", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ caseId, candidateTraceId: traceId }),
        signal: controller?.signal,
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) {
        setErr(typeof data?.detail === "string" ? data.detail : `Verification failed (HTTP ${r.status})`);
        return;
      }
      router.push(`/cases/${caseId}?candidate=${encodeURIComponent(traceId)}`);
      router.refresh();
    } catch (e) {
      if ((e as Error).name !== "AbortError") setErr("Verification failed: could not reach the server.");
    } finally {
      setBusy(null);
    }
  }

  const others = candidates.filter((c) => !c.is_source);
  return (
    <div className="space-y-2 text-[12.5px]">
      {others.length === 0 ? (
        <p className="text-fg-muted">
          No other run with this exact input yet. Run the command above (or your CI), then verify it here.
        </p>
      ) : (
        <ul className="divide-y divide-line/50 rounded-lg border border-line" aria-label="compatible candidate runs">
          {others.map((c) => (
            <li key={c.trace_id} className={`flex flex-wrap items-center gap-3 px-3 py-2 ${c.trace_id === selected ? "bg-signal/[0.06]" : ""}`}>
              <code className="font-mono text-[11.5px] text-fg">{c.trace_id.slice(0, 14)}…</code>
              <span className="font-mono text-[11px] text-fg-faint">{c.env}{c.run_id ? ` · run ${c.run_id}` : ""} · {c.ts.slice(0, 16).replace("T", " ")}</span>
              {c.level === "ERROR" && <span className="font-mono text-[11px] text-fail">errored</span>}
              {c.replay_verdict && <Badge variant={verdictVariant(c.replay_verdict)}>{c.replay_verdict}</Badge>}
              <span className="ml-auto flex gap-2">
                <a href={`/cases/${caseId}?candidate=${encodeURIComponent(c.trace_id)}`} className="text-[12px] text-fg-muted hover:text-signal">compare</a>
                <button onClick={() => verify(c.trace_id)} disabled={busy !== null} className="btn-primary text-[12px]">
                  {busy === c.trace_id ? "Verifying…" : c.replay_verdict ? "Re-verify" : "Verify"}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <button type="button" onClick={() => setShowManual((v) => !v)} className="text-[12px] text-fg-muted underline-offset-2 hover:underline" aria-expanded={showManual}>
        {showManual ? "Hide" : "Advanced: verify a trace id"}
      </button>
      {showManual && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (manual.trim()) void verify(manual.trim());
          }}
          className="flex gap-2"
        >
          <input aria-label="candidate trace id" value={manual} onChange={(e) => setManual(e.target.value)} placeholder="trace_id of a candidate run" className="input w-[320px] font-mono" />
          <button type="submit" disabled={busy !== null || !manual.trim()} className="btn-ghost">Verify</button>
        </form>
      )}
      {busy && (
        <button type="button" onClick={() => controller?.abort()} className="text-[12px] text-fg-faint underline-offset-2 hover:underline">
          cancel
        </button>
      )}
      {err && <div role="alert" className="text-[12px] text-fail">{err}</div>}
    </div>
  );
}
