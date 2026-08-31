"use client";

import { useEffect, useMemo, useState } from "react";
import type { AgentRow } from "@/app/lib/api";

/** The project's agent registry as a native searchable combobox.
 *
 *  A `<select>` over a registry with dozens of agents is a wall to scroll, so this is
 *  `<input list>` + `<datalist>`: type-to-filter for free, in every browser, no dependency.
 *  Typing only filters — a selection commits on an exact slug (or, where `allLabel` says an
 *  empty box means "all agents", on clearing it), so half-typed text never re-queries the
 *  server or drops a required pick. Anything left half-typed snaps back on blur. */
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
  allLabel?: string; // placeholder for the empty box; omitted = a pick is required
  hint?: (a: AgentRow) => string; // secondary text (a count); Chrome/Firefox render it, Safari doesn't
  sort?: boolean; // off where the caller curates the order (e.g. agents with cases first)
  id?: string;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
}) {
  // Natural order, so agent_2 sorts before agent_10 instead of after it.
  const options = useMemo(
    () =>
      sort
        ? [...agents].sort((a, b) => a.slug.localeCompare(b.slug, undefined, { numeric: true, sensitivity: "base" }))
        : agents,
    [agents, sort],
  );

  const selected = agents.find((a) => a[by] === value);
  const [text, setText] = useState(selected?.slug ?? "");
  // Follow the value when it moves from outside — a filter reset, a workspace switch.
  useEffect(() => setText(selected?.slug ?? ""), [selected?.slug]);

  function type(next: string) {
    setText(next);
    const hit = agents.find((a) => a.slug === next);
    if (hit) {
      if (hit[by] !== value) onChange(hit[by]);
    } else if (!next.trim() && allLabel && value) {
      onChange("");
    }
  }

  return (
    <>
      <input
        id={id}
        type="search"
        list={`${id ?? "agent-picker"}-options`}
        value={text}
        onChange={(e) => type(e.target.value)}
        onBlur={() => setText(selected?.slug ?? "")}
        disabled={disabled}
        placeholder={allLabel ?? "Search agents…"}
        aria-label={ariaLabel ?? "Agent"}
        className={className}
      />
      <datalist id={`${id ?? "agent-picker"}-options`}>
        {options.map((a) => (
          <option key={a.id} value={a.slug} label={hint?.(a)} />
        ))}
      </datalist>
    </>
  );
}
