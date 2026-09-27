"use client";

import { useEffect, useState } from "react";

// ⋯ on a "Rolling summary" column header: the workspace's summary budget. The summary is built
// once per conversation and shared by every column that reads @HISTORY / @ROLLING_SUMMARY, so
// this is a workspace setting, not a per-column one. Changes apply to summaries built from now
// on; a conversation's existing summary is rebuilt with "Generate" on its row.

type Budget = { max_tokens: number; step_max_tokens: number };
type ConfigResponse = { config: Budget; defaults: Budget; custom: boolean };

const PRESETS = [8000, 12000, 16000, 24000, 32000];

function DotsIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" className={className} aria-hidden>
      <circle cx="3" cy="8" r="1.4" /><circle cx="8" cy="8" r="1.4" /><circle cx="13" cy="8" r="1.4" />
    </svg>
  );
}

export function RollingSummarySettings() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ConfigResponse | null>(null);
  const [maxTokens, setMaxTokens] = useState("");
  const [stepTokens, setStepTokens] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!open) return;
    setMsg("");
    void fetch("/api/project/rolling-summary-config", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: ConfigResponse | null) => {
        if (!d?.config) return;
        setData(d);
        setMaxTokens(String(d.config.max_tokens));
        setStepTokens(String(d.config.step_max_tokens));
      })
      .catch(() => {});
  }, [open]);

  async function save(body: Partial<Budget>) {
    setBusy(true);
    setMsg("");
    try {
      const r = await fetch("/api/project/rolling-summary-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        setMsg(typeof d.detail === "string" ? d.detail : "Could not save.");
        return;
      }
      setData(d as ConfigResponse);
      setMaxTokens(String(d.config.max_tokens));
      setStepTokens(String(d.config.step_max_tokens));
      setMsg("Saved. Applies to summaries built from now on — use Generate on a conversation to rebuild it.");
    } finally {
      setBusy(false);
    }
  }

  const input =
    "w-full rounded-md border border-line bg-ink-950/60 px-2 py-1 font-mono text-[11.5px] text-fg focus:border-signal/40 focus:outline-none";
  const label = "mb-1 block text-[10px] font-medium uppercase tracking-wider text-fg-faint";

  return (
    <span className="relative ml-0.5 inline-flex">
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-flex h-5 w-5 items-center justify-center rounded text-fg-faint transition-colors hover:bg-ink-600 hover:text-fg"
        title="Rolling summary settings"
        aria-label="Rolling summary settings"
      >
        <DotsIcon className="h-3 w-3" />
      </button>
      {open && (
        <>
          <span className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <span className="absolute right-0 top-full z-30 mt-1 block w-72 rounded-lg border border-line bg-ink-900 p-3 text-left font-normal normal-case tracking-normal shadow-xl shadow-ink-950/50">
            <span className="mb-2 block text-[12px] font-semibold text-fg">Rolling summary budget</span>
            <span className="mb-3 block text-[10.5px] leading-relaxed text-fg-faint">
              What @HISTORY and @ROLLING_SUMMARY read. Over the budget, older turns are folded into one
              compacted summary; the last two items stay verbatim. Workspace-wide.
            </span>
            <label className={label}>Whole summary (tokens)</label>
            <input className={input} inputMode="numeric" value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)}
              aria-label="Whole summary tokens" />
            <span className="mt-1 mb-3 flex flex-wrap gap-1">
              {PRESETS.map((p) => (
                <button key={p} type="button" onClick={() => setMaxTokens(String(p))}
                  className="rounded border border-line px-1.5 py-0.5 text-[10px] text-fg-muted hover:border-line-bright hover:text-fg">
                  {p / 1000}k
                </button>
              ))}
            </span>
            <span className="mb-3 block text-[10px] leading-relaxed text-fg-faint">
              Keep it well under 32k if a Jev column reads @HISTORY — the question and the rest of the
              template share Jev&apos;s 32k-token budget.
            </span>
            <label className={label}>Keep a step verbatim up to (tokens)</label>
            <input className={input} inputMode="numeric" value={stepTokens} onChange={(e) => setStepTokens(e.target.value)}
              aria-label="Verbatim step tokens" />
            <span className="mt-1 mb-3 block text-[10px] text-fg-faint">Larger steps are summarized to a sentence by the model.</span>
            {msg && <span className="mb-2 block text-[10.5px] text-fg-muted">{msg}</span>}
            <span className="flex items-center justify-between gap-2">
              <button type="button" disabled={busy || !data?.custom} onClick={() => void save({})}
                className="text-[11px] text-fg-faint hover:text-fg disabled:opacity-40"
                title={data ? `Defaults: ${data.defaults.max_tokens} / ${data.defaults.step_max_tokens}` : undefined}>
                Reset to defaults
              </button>
              <button type="button" disabled={busy}
                onClick={() => void save({ max_tokens: Number(maxTokens), step_max_tokens: Number(stepTokens) })}
                className="rounded-md bg-signal/15 px-3 py-1 text-[11.5px] font-medium text-signal hover:bg-signal/25 disabled:opacity-50">
                {busy ? "Saving…" : "Save"}
              </button>
            </span>
          </span>
        </>
      )}
    </span>
  );
}
