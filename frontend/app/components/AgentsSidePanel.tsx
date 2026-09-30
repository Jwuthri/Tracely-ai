"use client";

// Right-side drawer listing a conversation's agents — exactly what a judge reads as `@AGENTS`.
// The backend merges three sources per tool, best first, and marks each tool with where it came from:
//   • DECLARED — the catalog the user sent via the SDK (tracely.trace(agents=[...])).
//   • OFFERED — in the tool list an instrumented model call was given, but not in the catalog.
//   • CALLED — only seen being called; nothing describes it.
// A declared agent's other keys (system_prompt, model, guardrails, config, …) render as expandable
// rows — the catalog is free-form, so the panel must not assume a fixed shape. An undeclared agent
// shows the system prompt and models recovered from its own spans instead.
// Everything here is click-to-expand: prompts, tool schemas, and arbitrary config blobs are all
// too big to render inline. Rendered via a portal so it escapes the table/timeline overflow.

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { HighlightedJson, prettyJson } from "./JsonView";
import { notInCatalog, observedDetail, type AgentsData, type MergedTool } from "./agentsPanel";

// Keys the card renders itself — everything else becomes a generic expandable row.
const DECLARED_OWN = new Set(["name", "description", "tools"]);
const TOOL_OWN = new Set(["name", "description", "parameters", "count"]);

const EMPTY: AgentsData = { agents: [], declared: [], observed: [] };

export function AgentsSidePanel({ threadId, onClose }: { threadId: string; onClose: () => void }) {
  const [data, setData] = useState<AgentsData | null>(null);

  useEffect(() => {
    let live = true;
    fetch(`/api/sessions/${encodeURIComponent(threadId)}/agents`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!live) return;
        setData({
          agents: Array.isArray(d?.agents) ? d.agents : [],
          declared: Array.isArray(d?.declared) ? d.declared : [],
          observed: Array.isArray(d?.observed) ? d.observed : [],
        });
      })
      .catch(() => live && setData(EMPTY));
    return () => {
      live = false;
    };
  }, [threadId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const missing = data ? notInCatalog(data) : 0;

  return createPortal(
    <>
      <div className="fixed inset-0 z-[80] bg-black/50 backdrop-blur-[1px]" onClick={onClose} />
      <aside className="fixed inset-y-0 right-0 z-[81] flex w-full max-w-md flex-col border-l border-line bg-ink-900 shadow-2xl">
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-[14px] font-semibold text-fg">
              <BotIcon /> Conversation Agents
            </h2>
            <p className="mt-0.5 font-mono text-[11px] text-fg-faint">{threadId}</p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-fg-faint transition-colors hover:bg-hilite/5 hover:text-fg"
            aria-label="Close"
          >
            <CloseIcon />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {data === null ? (
            <div className="space-y-3">
              <div className="h-24 animate-pulse rounded-lg bg-hilite/[0.03]" />
              <div className="h-24 animate-pulse rounded-lg bg-hilite/[0.03]" />
            </div>
          ) : data.agents.length === 0 ? (
            <p className="mt-8 text-center text-[13px] text-fg-faint">
              No agents found for this conversation.
              <br />
              <span className="text-[11.5px]">
                Declare them via <code className="font-mono">tracely.trace(agents=[…])</code> in the SDK.
              </span>
            </p>
          ) : (
            <div className="space-y-3">
              <p className="text-[11.5px] text-fg-faint">
                What a judge reads as <code className="font-mono text-fg-muted">@AGENTS</code>.
              </p>
              {missing > 0 && (
                <p className="rounded-md border border-warn/30 bg-warn/[0.06] px-3 py-2 text-[12px] text-fg-muted">
                  {missing} tool{missing === 1 ? " isn't" : "s aren't"} in your agent definition — the traces
                  show {missing === 1 ? "it" : "them"} offered to the model or being called.
                </p>
              )}
              {data.agents.map((a, i) => {
                const declared = data.declared.find((d) => d.name.toLowerCase() === a.name.toLowerCase());
                const observed = declared ? undefined : observedDetail(a, data);
                return (
                  <div key={`${a.name}-${i}`} className="rounded-lg border border-line bg-hilite/[0.02] p-4">
                    <div className="text-[13.5px] font-semibold text-fg">{a.name}</div>
                    {a.description && <div className="mt-0.5 text-[12px] text-fg-muted">{a.description}</div>}

                    {/* every key the card doesn't render itself — system_prompt, model, guardrails, … */}
                    {declared &&
                      Object.entries(declared)
                        .filter(([k, v]) => !DECLARED_OWN.has(k) && v != null && v !== "")
                        .map(([k, v]) => <ConfigRow key={k} label={k} value={v} />)}
                    {/* recovered from the agent's own spans, not declared by anyone */}
                    {observed?.system_prompt && <ConfigRow label="system_prompt" value={observed.system_prompt} derived />}
                    {observed?.models && observed.models.length > 0 && (
                      <ConfigRow label="models" value={observed.models} derived />
                    )}

                    <div className="mt-3 space-y-1.5">
                      <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-faint">
                        Tools{a.tools.length > 0 && <span className="text-fg-muted"> · {a.tools.length}</span>}
                      </div>
                      {a.tools.length === 0 ? (
                        <p className="text-[12px] text-fg-faint">No tools.</p>
                      ) : (
                        a.tools.map((t) => (
                          <ToolRow
                            key={t.name}
                            tool={t}
                            extras={declared?.tools.find((d) => d.name === t.name)}
                          />
                        ))
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </aside>
    </>,
    document.body,
  );
}

// One click-to-expand config row. Strings render as wrapped text (prompts), everything else as
// highlighted JSON. `derived` marks values Tracely recovered from the trace rather than was told.
function ConfigRow({ label, value, derived }: { label: string; value: unknown; derived?: boolean }) {
  const [open, setOpen] = useState(false);
  const isText = typeof value === "string";
  const body = isText ? (value as string) : prettyJson(value) ?? "";
  const preview = isText
    ? (value as string).replace(/\s+/g, " ").slice(0, 60)
    : Array.isArray(value)
      ? `${value.length} item${value.length === 1 ? "" : "s"}`
      : `${Object.keys(value as object).length} keys`;

  return (
    <div className="mt-2 overflow-hidden rounded-md border border-line/70 bg-ink-950/60">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-hilite/[0.04]"
      >
        <Chevron open={open} />
        <span className="font-mono text-[11px] text-fg">{label}</span>
        {derived && (
          <span
            title="recovered from the trace, not declared"
            className="rounded border border-line bg-hilite/[0.04] px-1 py-px font-mono text-[9px] uppercase tracking-wide text-fg-faint"
          >
            derived
          </span>
        )}
        {!open && <span className="truncate text-[11px] text-fg-faint">{preview}</span>}
      </button>
      {open && (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words border-t border-line/70 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-fg-muted">
          {isText ? body : <HighlightedJson text={body} />}
        </pre>
      )}
    </div>
  );
}

const SOURCE_BADGE: Record<MergedTool["source"], { label: string; title: string }> = {
  declared: { label: "declared", title: "in the agent definition the SDK sent" },
  offered: { label: "offered", title: "not in the agent definition — the model was offered it" },
  called: { label: "called only", title: "not in the agent definition, and nothing describes it — only seen being called" },
};

// A tool: name, source and call count always visible; the parameter schema and any extra keys the
// agent definition gave it on click.
function ToolRow({ tool, extras: declared }: { tool: MergedTool; extras?: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const extras = Object.fromEntries(Object.entries(declared ?? {}).filter(([k]) => !TOOL_OWN.has(k)));
  const params = tool.parameters || {};
  const hasDetail = Object.keys(params).length > 0 || Object.keys(extras).length > 0;
  const badge = SOURCE_BADGE[tool.source] ?? SOURCE_BADGE.called;

  return (
    <div className="overflow-hidden rounded-md border border-line/70 bg-ink-950/60">
      <button
        onClick={() => hasDetail && setOpen((o) => !o)}
        aria-expanded={hasDetail ? open : undefined}
        className={`w-full px-2.5 py-1.5 text-left transition-colors ${hasDetail ? "hover:bg-hilite/[0.04]" : "cursor-default"}`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1.5 font-mono text-[11.5px] text-fg">
            {hasDetail ? <Chevron open={open} /> : <span className="h-1.5 w-1.5 rounded-[3px] bg-t_tool" />}
            <span className="truncate">{tool.name}</span>
          </span>
          <span className="flex shrink-0 items-center gap-1.5 font-mono text-[10px] text-fg-faint">
            <span
              title={badge.title}
              className={`rounded border px-1 py-px uppercase tracking-wide ${
                tool.source === "declared" ? "border-line" : "border-warn/40 text-warn"
              }`}
            >
              {badge.label}
            </span>
            <span title={tool.calls ? `called ${tool.calls}×` : "not called in this conversation"}>
              {tool.calls > 0 ? `×${tool.calls}` : "unused"}
            </span>
          </span>
        </div>
        {tool.description && <div className="mt-0.5 text-[11.5px] text-fg-muted">{tool.description}</div>}
      </button>
      {open && (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words border-t border-line/70 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-fg-muted">
          <HighlightedJson text={prettyJson({ ...extras, parameters: params }) ?? ""} />
        </pre>
      )}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      className={`shrink-0 text-fg-faint transition-transform ${open ? "rotate-90" : ""}`}
    >
      <path d="m9 18 6-6-6-6" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-faint">{children}</div>
  );
}

function BotIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" className="text-signal">
      <rect x="4" y="8" width="16" height="11" rx="2.5" stroke="currentColor" strokeWidth="1.7" />
      <path d="M12 4v4M9 13h.01M15 13h.01" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <circle cx="12" cy="3.5" r="1.2" fill="currentColor" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
