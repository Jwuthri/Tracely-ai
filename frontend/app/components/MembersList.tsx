"use client";

import { useEffect, useState } from "react";

import type { Member } from "@/app/lib/auth/types";

/** Who is in the organization. Everyone here can reach every workspace in the account, which is
 *  exactly why it's worth showing — an invite is not scoped to one workspace, and neither is
 *  removing someone: the seat IS the access.
 *
 *  Leaving and removing are the same button pointed at a different row, because they are the same
 *  backend call. The refusals (last owner, personal account) are the server's — it holds the
 *  counts, so we surface its message rather than second-guessing it here. */
export function MembersList({
  meId,
  local,
  canManage,
}: {
  meId: string | null;
  /** Clerk owns membership there and re-creates the row from the JWT, so the endpoint that
   *  backs these buttons is only mounted in local mode. */
  local: boolean;
  canManage: boolean;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/auth/members")
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => setMembers(Array.isArray(d) ? d : []))
      .catch(() => setMembers([]))
      .finally(() => setLoaded(true));
  }, []);

  async function remove(m: Member) {
    const self = m.user_id === meId;
    const what = self
      ? "Leave this organization? You'll lose access to all of its workspaces. If it's the only one you belong to, you'll land in a fresh personal workspace."
      : `Remove ${m.display_name || m.email}? They lose access to every workspace in this organization.`;
    if (!window.confirm(what)) return;
    setBusy(m.user_id);
    setErr(null);
    try {
      const r = await fetch(`/api/auth/members/${m.user_id}`, { method: "DELETE" });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        setErr(d?.detail ?? `Failed (HTTP ${r.status})`);
        return;
      }
      // Leaving: the proxy moved the active-workspace cookie to one that still resolves, and a
      // full navigation re-reads it everywhere instead of leaving stale server components behind.
      if (self) window.location.href = "/dashboard";
      else setMembers((prev) => prev.filter((x) => x.user_id !== m.user_id));
    } catch {
      setErr("Failed: could not reach the server.");
    } finally {
      setBusy(null);
    }
  }

  if (!loaded) return null;

  return (
    <section className="card overflow-hidden">
      <div className="flex items-baseline justify-between gap-3 border-b border-line px-5 py-3">
        <span className="text-[13px] font-semibold text-fg">Members</span>
        <span className="font-mono text-[10.5px] uppercase tracking-wider text-fg-faint">
          {members.length} {members.length === 1 ? "seat" : "seats"} used
        </span>
      </div>
      <ul>
        {members.map((m) => {
          const self = m.user_id === meId;
          const canRemove = local && (self || canManage);
          return (
            <li
              key={m.user_id}
              className="flex items-center justify-between gap-3 border-b border-line/50 px-5 py-2.5 last:border-b-0"
            >
              <span className="min-w-0">
                <span className="block truncate text-[13px] text-fg">
                  {m.display_name || m.email}
                </span>
                {m.display_name && (
                  <span className="block truncate text-[11.5px] text-fg-faint">{m.email}</span>
                )}
              </span>
              <span className="flex shrink-0 items-center gap-3">
                <span className="font-mono text-[10.5px] uppercase tracking-wider text-fg-muted">
                  {m.role}
                </span>
                {canRemove && (
                  <button
                    onClick={() => remove(m)}
                    disabled={busy !== null}
                    className="rounded-md border border-line px-2 py-1 text-[11.5px] text-fg-muted transition-colors hover:border-fail/40 hover:bg-fail/10 hover:text-fail disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {busy === m.user_id ? "…" : self ? "Leave" : "Remove"}
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      {err && (
        <p role="alert" className="border-t border-line px-5 py-2.5 text-[12.5px] text-fail">
          {err}
        </p>
      )}
    </section>
  );
}
