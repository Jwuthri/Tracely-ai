"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/** "Try the sample": seeds the demo dataset (same path as `make demo`) — its traces are stamped
 *  as sample data and never count as this workspace's activation. Queued, not awaited. */
export function SampleButton() {
  const [state, setState] = useState<"idle" | "busy" | "queued">("idle");
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  async function go() {
    setState("busy");
    setErr(null);
    try {
      const r = await fetch("/api/project/seed", { method: "POST" });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        setErr(typeof d?.detail === "string" ? d.detail : `Seeding failed (HTTP ${r.status})`);
        setState("idle");
        return;
      }
      setState("queued");
    } catch {
      setErr("Seeding failed: could not reach the server.");
      setState("idle");
    }
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button onClick={go} disabled={state === "busy"} className="btn-ghost" title="Seeds a sample agent with a real reproducible bug and its fix. Labelled as sample data everywhere.">
        {state === "busy" ? "Queueing…" : "Try the sample"}
      </button>
      {state === "queued" && (
        <span className="text-[12px] text-fg-muted">
          Queued (a minute or two).{" "}
          <button onClick={() => router.refresh()} className="text-signal hover:underline">Refresh</button>
        </span>
      )}
      {err && <span role="alert" className="text-[12px] text-fail">{err}</span>}
    </span>
  );
}
