"use client";

import { useCallback, useEffect, useState } from "react";

// Single source of truth for the "hidden span types" preference — shared between the trace
// table, the timeline, and the fleet/replay views so one filter applies everywhere.
//
// Two layers:
//   · WORKSPACE default — stored server-side (`projects.ui_prefs.hiddenTypes`), fetched once
//     per mount. What every member's views start from.
//   · LOCAL override — this browser's localStorage. It exists only once the user actually
//     touches the filter; its PRESENCE is what makes it win (an explicit empty set is a real
//     choice: "show me everything, whatever the workspace hides").
// "Use workspace default" deletes the local key; "save as default" pushes the current set to
// the workspace and drops the local override so this browser tracks it again.
const PREFS_KEY = "tracely.traceTable.prefs";
const EVENT = "tracely:prefs";

function readLocal(): Set<string> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as { hiddenTypes?: unknown };
    return Array.isArray(p.hiddenTypes) ? new Set(p.hiddenTypes as string[]) : null;
  } catch {
    return null;
  }
}

function writeLocal(next: Set<string> | null) {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    const cur = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    if (next === null) delete cur.hiddenTypes;
    else cur.hiddenTypes = [...next];
    localStorage.setItem(PREFS_KEY, JSON.stringify(cur));
    window.dispatchEvent(new Event(EVENT));
  } catch {
    /* ignore */
  }
}

export function useHiddenTypes(): {
  hidden: Set<string>;
  /** Where the current set comes from — drives the "save/use default" affordances. */
  source: "local" | "workspace";
  toggle: (type: string) => void;
  /** Explicit local "show everything" (still an override of the workspace default). */
  reset: () => void;
  /** Drop the local override — this browser follows the workspace default again. */
  useWorkspaceDefault: () => void;
  /** Push the current set as the workspace default (and follow it). */
  saveAsWorkspaceDefault: () => void;
} {
  const [local, setLocal] = useState<Set<string> | null>(null);
  const [workspace, setWorkspace] = useState<Set<string>>(new Set());

  useEffect(() => {
    setLocal(readLocal());
    const sync = () => setLocal(readLocal());
    window.addEventListener(EVENT, sync);
    window.addEventListener("storage", sync); // cross-tab sync
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  useEffect(() => {
    // The public share page renders these views without a session — a 401 here simply means
    // "no workspace default", never an error state.
    let alive = true;
    fetch("/api/project/ui-prefs", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { prefs: {} }))
      .then((d) => {
        const t = d?.prefs?.hiddenTypes;
        if (alive && Array.isArray(t)) setWorkspace(new Set(t as string[]));
      })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  const hidden = local ?? workspace;
  const source: "local" | "workspace" = local ? "local" : "workspace";

  const toggle = useCallback((type: string) => {
    // first touch materializes the local override from whatever is currently in effect
    const cur = readLocal() ?? new Set(hidden);
    if (cur.has(type)) cur.delete(type);
    else cur.add(type);
    writeLocal(cur);
    setLocal(cur);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hidden]);

  const reset = useCallback(() => {
    const empty = new Set<string>();
    writeLocal(empty);
    setLocal(empty);
  }, []);

  const useWorkspaceDefault = useCallback(() => {
    writeLocal(null);
    setLocal(null);
  }, []);

  const saveAsWorkspaceDefault = useCallback(() => {
    const current = [...(readLocal() ?? workspace)];
    fetch("/api/project/ui-prefs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prefs: { hiddenTypes: current } }),
    })
      .then((r) => { if (r.ok) setWorkspace(new Set(current)); })
      .catch(() => {});
    writeLocal(null);
    setLocal(null);
  }, [workspace]);

  return { hidden, source, toggle, reset, useWorkspaceDefault, saveAsWorkspaceDefault };
}
