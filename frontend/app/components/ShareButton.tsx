"use client";

import { useState } from "react";
import { IconCheck, IconCopy } from "./icons";

type Props = {
  /** Legacy call site (ConversationChrome). Same thing as `kind="conversation"` + that id. */
  threadId?: string;
  kind?: "conversation" | "gate";
  id?: string;
  /** What the link exposes, for the warning line. */
  label?: string;
};

/** Mint a public link for one object and copy it. The link is unlisted and read-only; it expires
 *  on its own after 30 days, and "Stop sharing" kills every link minted for this object — including
 *  ones nobody has a copy of any more. */
export function ShareButton({ threadId, kind, id, label }: Props) {
  const subjectKind = kind ?? "conversation";
  const subjectId = id ?? threadId ?? "";
  const what = label ?? (subjectKind === "gate" ? "this gate result" : "the full conversation");

  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const [copied, setCopied] = useState(false);
  const [revoked, setRevoked] = useState(false);

  async function mint() {
    if (busy) return;
    setBusy(true);
    setErr(false);
    try {
      const r = await fetch("/api/share", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: subjectKind, id: subjectId }),
      });
      const j = await r.json();
      if (!r.ok || !j.token) throw new Error(j.detail ?? "failed");
      const link = `${window.location.origin}/share/${j.token}`;
      setUrl(link);
      setRevoked(false);
      await copy(link);
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (busy) return;
    setBusy(true);
    setErr(false);
    try {
      const q = `kind=${encodeURIComponent(subjectKind)}&id=${encodeURIComponent(subjectId)}`;
      const r = await fetch(`/api/share?${q}`, { method: "DELETE" });
      if (!r.ok) throw new Error("failed");
      setUrl(null);
      setRevoked(true);
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard blocked (insecure origin / permissions) — the input below is selectable */
    }
  }

  return (
    <div className="flex flex-col items-start gap-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => (url ? copy(url) : mint())}
          disabled={busy}
          className="btn-ghost"
        >
          {copied ? <IconCheck className="h-3.5 w-3.5 text-ok" /> : <IconCopy className="h-3.5 w-3.5" />}
          {busy ? "Working…" : copied ? "Link copied" : url ? "Copy link" : "Share"}
        </button>
        <button
          type="button"
          onClick={revoke}
          disabled={busy}
          className="btn-ghost text-fg-faint hover:text-fail"
          title="Kill every public link ever created for this — including ones you no longer have"
        >
          Stop sharing
        </button>
      </div>

      {url && (
        <div className="flex flex-col gap-1">
          <input
            readOnly
            value={url}
            onFocus={(e) => e.currentTarget.select()}
            className="w-[min(28rem,80vw)] rounded-md border border-line bg-ink-900 px-2 py-1 font-mono text-[11px] text-fg-muted"
          />
          <span className="text-[11px] text-fg-faint">
            Anyone with this link can read {what}. Expires in 30 days, or when you press Stop
            sharing.
          </span>
        </div>
      )}
      {revoked && (
        <span className="text-[11px] text-fg-faint">
          Public links for this are off. Press Share to create a new one.
        </span>
      )}
      {err && <span className="text-[11px] text-fail">Something went wrong — try again.</span>}
    </div>
  );
}
