"use client";

import clsx from "clsx";
import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentRow } from "@/app/lib/api";

/** The project's agent registry as a searchable combobox.
 *
 *  A `<select>` over a registry with dozens of agents is a wall to scroll, and the two native
 *  fixes for that — `<select>` and `<datalist>` — both render an OS popup we cannot theme, a
 *  light-grey system panel dropped on top of a dark app. So the list is ours: an input that
 *  filters, a panel that looks like the rest of the product, and substring matching (the
 *  datalist popup only prefix-matches in Safari).
 *
 *  Typing filters, it never commits — a pick lands on click or Enter, and anything half-typed
 *  reverts when the panel closes, so a required selection can't be lost by wandering off. */
export function AgentPicker({
  agents,
  value,
  onChange,
  by = "id",
  allLabel,
  hint,
  sort = true,
  id,
  ariaLabel,
  className,
  disabled,
}: {
  agents: AgentRow[];
  value: string; // the picked agent's `by` field; "" = every agent (only where allLabel is set)
  onChange: (value: string) => void;
  by?: "id" | "slug"; // which field the caller stores
  allLabel?: string; // the "no filter" row; omitted = a pick is required
  hint?: (a: AgentRow) => string; // secondary text on the row (a count)
  sort?: boolean; // off where the caller curates the order (e.g. agents with cases first)
  id?: string;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  const selected = agents.find((a) => a[by] === value);

  // Natural order, so agent_2 sorts before agent_10 instead of after it.
  const ordered = useMemo(
    () =>
      sort
        ? [...agents].sort((a, b) => a.slug.localeCompare(b.slug, undefined, { numeric: true, sensitivity: "base" }))
        : agents,
    [agents, sort],
  );

  const needle = query.trim().toLowerCase();
  const matches = useMemo(
    () => (needle ? ordered.filter((a) => a.slug.toLowerCase().includes(needle)) : ordered),
    [ordered, needle],
  );
  // `null` is the "every agent" row — only offered where the caller allows an empty value.
  const rows: (AgentRow | null)[] = allLabel && !needle ? [null, ...matches] : matches;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  // Closing is always a full reset: the box shows what is actually selected, never a stale search.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  // Keep the highlighted row in view while arrowing through a long registry.
  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  function commit(a: AgentRow | null) {
    const next = a ? a[by] : "";
    if (next !== value) onChange(next);
    setOpen(false);
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      return;
    }
    if (e.key === "Enter") {
      if (open && rows[active] !== undefined) {
        e.preventDefault();
        commit(rows[active]);
      }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (rows.length ? (i + step + rows.length) % rows.length : 0));
    }
  }

  return (
    <div ref={rootRef} className="relative">
      <input
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        autoComplete="off"
        value={open ? query : selected?.slug ?? ""}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        onKeyDown={onKey}
        disabled={disabled}
        placeholder={selected ? selected.slug : allLabel ?? "Search agents…"}
        aria-label={ariaLabel ?? "Agent"}
        className={clsx(className, "pr-7")}
      />
      {/* A chevron, so the box still reads as a picker rather than a text field. */}
      <span
        aria-hidden
        className={clsx(
          "pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-fg-faint transition-transform",
          open && "rotate-180",
        )}
      >
        <svg viewBox="0 0 12 12" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.6">
          <path d="M3 4.5 6 7.5 9 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>

      {open && (
        <div
          ref={listRef}
          role="listbox"
          // Keep focus in the input: a blur before the click would close the panel under the cursor.
          onMouseDown={(e) => e.preventDefault()}
          className="absolute left-0 top-full z-50 mt-2 max-h-72 w-full min-w-[17rem] overflow-y-auto rounded-xl border border-line bg-ink-800/95 p-1 shadow-panel backdrop-blur-md"
        >
          {rows.length === 0 && (
            <div className="px-2.5 py-3 text-center text-[12px] text-fg-faint">No agent matches “{query}”</div>
          )}
          {rows.map((a, i) => {
            const isSelected = a ? a[by] === value : !value;
            return (
              <button
                key={a?.id ?? "__all"}
                type="button"
                role="option"
                aria-selected={isSelected}
                data-active={i === active}
                onMouseEnter={() => setActive(i)}
                onClick={() => commit(a)}
                className={clsx(
                  "group relative flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors",
                  i === active ? "bg-signal/10 text-fg" : "text-fg-muted",
                )}
              >
                {i === active && (
                  <span className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-r-full bg-signal shadow-[0_0_8px_rgb(var(--c-signal)/0.7)]" />
                )}
                <span
                  aria-hidden
                  className={clsx(
                    "grid h-3.5 w-3.5 shrink-0 place-items-center text-[10px]",
                    isSelected ? "text-signal" : "text-transparent",
                  )}
                >
                  ✓
                </span>
                <span className={clsx("min-w-0 flex-1 truncate", a ? "font-mono text-[12px]" : "text-[12.5px]")}>
                  {a ? highlight(a.slug, needle) : allLabel}
                </span>
                {a && hint && (
                  <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-fg-faint">
                    {hint(a)}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Tint the matched span so it's obvious *why* a row survived the filter. */
function highlight(slug: string, needle: string) {
  if (!needle) return slug;
  const at = slug.toLowerCase().indexOf(needle);
  if (at < 0) return slug;
  return (
    <>
      {slug.slice(0, at)}
      <span className="text-signal">{slug.slice(at, at + needle.length)}</span>
      {slug.slice(at + needle.length)}
    </>
  );
}
