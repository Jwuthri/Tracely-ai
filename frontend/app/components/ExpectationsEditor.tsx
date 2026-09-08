"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import type { Assertions, ToolArgPredicate } from "@/app/lib/expectations";

/**
 * Editable expected behaviour (W5 §2). The common expectations are first-class fields; the
 * rest (argument predicates, call bounds) sit behind "Advanced". Saving bumps the case version
 * and re-checks that the original still fails — the page re-renders with the new verdict.
 */
export function ExpectationsEditor({ caseId, initial }: { caseId: string; initial: Assertions }) {
  const [open, setOpen] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [required, setRequired] = useState((initial.required_tools ?? []).join(", "));
  const [forbidden, setForbidden] = useState((initial.forbidden_tools ?? []).join(", "));
  const [mode, setMode] = useState(initial.match_mode ?? "superset");
  const [noError, setNoError] = useState(initial.no_error !== false);
  const [allowToolErrors, setAllowToolErrors] = useState(Boolean(initial.allow_tool_errors));
  const [limits, setLimits] = useState(
    Object.entries(initial.max_tool_calls ?? {}).map(([t, n]) => `${t}=${n}`).join(", "),
  );
  const [args, setArgs] = useState<ToolArgPredicate[]>(initial.tool_args ?? []);
  const router = useRouter();
  const id = useId();

  const list = (s: string) => s.split(",").map((t) => t.trim()).filter(Boolean);

  async function save() {
    setBusy(true);
    setErr(null);
    const max_tool_calls: Record<string, number> = {};
    for (const part of list(limits)) {
      const [t, n] = part.split("=").map((x) => x.trim());
      const num = Number(n);
      if (!t || !Number.isInteger(num) || num < 0) {
        setErr(`Call bound "${part}" must look like tool=2`);
        setBusy(false);
        return;
      }
      max_tool_calls[t] = num;
    }
    const body = {
      required_tools: list(required),
      forbidden_tools: list(forbidden),
      match_mode: mode,
      no_error: noError,
      allow_tool_errors: allowToolErrors,
      max_tool_calls,
      tool_args: args.filter((p) => p.tool && p.key).map((p) => ({ ...p, equals: parseLiteral(String(p.equals ?? "")) })),
    };
    try {
      const r = await fetch(`/api/cases/${caseId}/expectations`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) {
        setErr(typeof data?.detail === "string" ? data.detail : `Save failed (HTTP ${r.status})`);
        return;
      }
      setOpen(false);
      router.refresh();
    } catch {
      setErr("Save failed: could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className="btn-ghost text-[12.5px]" aria-expanded={false}>
        Edit expectations
      </button>
    );
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
      className="space-y-3 rounded-lg border border-line bg-ink-900/60 p-3 text-[12.5px]"
      aria-label="Edit expectations"
    >
      <Field id={`${id}-req`} label="Must call (comma-separated tool names)">
        <input id={`${id}-req`} value={required} onChange={(e) => setRequired(e.target.value)} className="input" autoFocus />
      </Field>
      <Field id={`${id}-mode`} label="How the required tools are matched">
        <select id={`${id}-mode`} value={mode} onChange={(e) => setMode(e.target.value)} className="input">
          <option value="superset">superset — any order, other tools allowed</option>
          <option value="unordered">unordered — exactly these, any order</option>
          <option value="strict">strict — exactly these, in this order</option>
          <option value="subset">subset — only these tools</option>
        </select>
      </Field>
      <Field id={`${id}-forb`} label="Must never call">
        <input id={`${id}-forb`} value={forbidden} onChange={(e) => setForbidden(e.target.value)} className="input" placeholder="e.g. delete_account" />
      </Field>
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={noError} onChange={(e) => setNoError(e.target.checked)} />
        The run must complete without error
      </label>
      {noError && (
        <label className="ml-5 flex items-center gap-2 text-fg-muted">
          <input type="checkbox" checked={allowToolErrors} onChange={(e) => setAllowToolErrors(e.target.checked)} />
          …but a tool may fail (the agent must handle it)
        </label>
      )}
      <button type="button" onClick={() => setAdvanced((v) => !v)} className="text-[12px] text-fg-muted underline-offset-2 hover:underline" aria-expanded={advanced}>
        {advanced ? "Hide advanced" : "Advanced: call bounds, argument predicates"}
      </button>
      {advanced && (
        <div className="space-y-3 border-l border-line pl-3">
          <Field id={`${id}-lim`} label="Call bounds (tool=max, comma-separated)">
            <input id={`${id}-lim`} value={limits} onChange={(e) => setLimits(e.target.value)} className="input" placeholder="lookup=1" />
          </Field>
          <div>
            <div className="mb-1 text-fg-faint">Argument predicates — every call to the tool must carry key = value</div>
            {args.map((p, i) => (
              <div key={i} className="mb-1.5 grid grid-cols-[1fr_1fr_1fr_auto] gap-1.5">
                <input aria-label="tool" value={p.tool} onChange={(e) => setArgs(args.map((q, j) => (j === i ? { ...q, tool: e.target.value } : q)))} className="input" placeholder="tool" />
                <input aria-label="key" value={p.key} onChange={(e) => setArgs(args.map((q, j) => (j === i ? { ...q, key: e.target.value } : q)))} className="input" placeholder="key (a.b allowed)" />
                <input aria-label="value" value={String(p.equals ?? "")} onChange={(e) => setArgs(args.map((q, j) => (j === i ? { ...q, equals: e.target.value } : q)))} className="input" placeholder="value (JSON or text)" />
                <button type="button" onClick={() => setArgs(args.filter((_, j) => j !== i))} className="btn-ghost" aria-label="remove predicate">✕</button>
              </div>
            ))}
            <button type="button" onClick={() => setArgs([...args, { tool: "", key: "", equals: "" }])} className="btn-ghost text-[12px]">
              + predicate
            </button>
          </div>
        </div>
      )}
      <p className="text-[11.5px] text-fg-faint">
        A failure trace is evidence of the bug, not the expected behaviour: the recorded arguments are never copied into the
        contract for you. Saving creates case version {"→"} next and re-checks that the original still fails it.
      </p>
      {err && <div role="alert" className="text-[12px] text-fail">{err}</div>}
      <div className="flex gap-2">
        <button type="submit" disabled={busy} className="btn-primary">{busy ? "Saving…" : "Save expectations"}</button>
        <button type="button" onClick={() => setOpen(false)} disabled={busy} className="btn-ghost">Cancel</button>
      </div>
    </form>
  );
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-fg-faint">{label}</label>
      {children}
    </div>
  );
}

function parseLiteral(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}
