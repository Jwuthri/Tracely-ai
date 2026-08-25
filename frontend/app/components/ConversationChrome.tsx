"use client";

import { DocLink } from "@/app/components/DocLink";
import clsx from "clsx";
import Link from "next/link";
import type { ReactNode } from "react";
import { fmtUsd } from "../lib/usage";
import { CopyId } from "./CopyId";
import { SaveAsScenarioButton } from "./SaveAsScenarioButton";
import { ShareButton } from "./ShareButton";
import { IconArrowLeft } from "./icons";
import { Badge, verdictVariant } from "./ui";

/* The chrome every lens of a conversation wears. Table and Timeline are client tabs on the
   conversation page; Replay and Fleet are their own routes — but all four keep the same header
   and the same strip, so switching lens never looks like landing in a different app. */

export type ConvView = "table" | "timeline" | "replay" | "fleet";

export function ConversationHeader({
  threadId, turns, usage, agentRef, firstInput,
}: {
  threadId: string; turns: number; usage: Record<string, number>;
  agentRef: string; firstInput: string;
}) {
  return (
    <header className="reveal">
      <Link href="/traces" className="inline-flex items-center gap-1.5 text-[13px] text-fg-muted transition-colors hover:text-signal">
        <IconArrowLeft className="h-4 w-4" /> Traces
      </Link>
      <div className="mt-4 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="font-display text-[22px] font-extrabold tracking-tight">Conversation</h1>
            <DocLink path="/product/traces#one-conversation-five-lenses" />
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-3 font-mono text-[11.5px] text-fg-faint">
            <CopyId value={threadId} label="thread id" />
            <span>{turns} turns</span>
            {usage.input_tokens ? <span>{usage.input_tokens.toLocaleString("en-US")} in</span> : null}
            {usage.cached_tokens ? <span>{usage.cached_tokens.toLocaleString("en-US")} cached</span> : null}
            {usage.output_tokens ? <span>{usage.output_tokens.toLocaleString("en-US")} out</span> : null}
            {usage.total_tokens ? <span>{usage.total_tokens.toLocaleString("en-US")} tokens</span> : null}
            {usage.cost ? <span className="text-syn-bool/90">{fmtUsd(usage.cost)}</span> : null}
          </div>
        </div>
        {/* Only ACTIONS live up here — the lenses are tabs, and the grading hangs off the
            verdict pill. A header of eight equal ghost buttons said nothing was important. */}
        <div className="flex items-center gap-3">
          {turns > 0 && agentRef && (
            <SaveAsScenarioButton
              threadId={threadId}
              agent={agentRef}
              defaultTitle={firstInput ? `Prod · ${firstInput.slice(0, 70)}` : undefined}
            />
          )}
          {turns > 0 && <ShareButton threadId={threadId} />}
        </div>
      </div>
    </header>
  );
}

const TAB = "relative flex items-center gap-2 px-4 py-2.5 text-[13px] font-medium transition-colors";
const underline = <span className="absolute inset-x-3 -bottom-px h-0.5 rounded bg-signal" />;

/**
 * The strip. Table/Timeline are BUTTONS when the conversation page owns that state and LINKS
 * from Replay/Fleet (where switching back is a navigation); Replay/Fleet are always links.
 */
export function ConversationTabs({
  threadId, active, spans, onSelect, right,
}: {
  threadId: string;
  active: ConvView;
  spans: number;
  /** Provided by the conversation page, which toggles Table/Timeline client-side. */
  onSelect?: (view: "table" | "timeline") => void;
  right?: ReactNode;
}) {
  const base = `/sessions/${encodeURIComponent(threadId)}`;
  const cls = (v: ConvView) => clsx(TAB, active === v ? "text-fg" : "text-fg-faint hover:text-fg-muted");
  const local = (v: "table" | "timeline", label: ReactNode) =>
    onSelect ? (
      <button key={v} onClick={() => onSelect(v)} className={cls(v)}>{label}{active === v && underline}</button>
    ) : (
      <Link key={v} href={v === "table" ? base : `${base}?view=timeline`} className={cls(v)}>{label}</Link>
    );
  return (
    <div className="flex items-center justify-between gap-1 border-b border-line">
      <div className="flex items-center gap-1">
        {local("table", "Table")}
        {local("timeline", <>Timeline <span className="font-mono text-[11px] text-fg-faint">{spans}</span></>)}
        <span className="mx-1 h-4 w-px bg-line" />
        <Link href={`${base}/replay`} className={cls("replay")}>▶ Replay{active === "replay" && underline}</Link>
        <Link href={`${base}/fleet`} className={cls("fleet")}>⌂ Fleet{active === "fleet" && underline}</Link>
      </div>
      <div className="flex items-center gap-2">{right}</div>
    </div>
  );
}

/** Tracely's own grading of this conversation — status AND the way into it. */
export function EvalsPill({ threadId, verdict, link = true }: {
  threadId: string; verdict: "PASS" | "FAIL" | null; link?: boolean;
}) {
  if (!verdict) return null;
  const badge = (
    <Badge variant={verdictVariant(verdict)} dot>
      evals {verdict}{link ? " →" : ""}
    </Badge>
  );
  if (!link) return badge;
  return (
    <Link href={`/sessions/${encodeURIComponent(threadId)}/evals`}
      title="Open Tracely's evaluation of this conversation"
      className="transition-opacity hover:opacity-80">
      {badge}
    </Link>
  );
}
