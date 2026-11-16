"use client";

import { Fragment } from "react";
import { usePathname } from "next/navigation";
import { IconChevron, IconSearch } from "./icons";
import { OnboardingQuest } from "./OnboardingQuest";
import { ThemeToggle } from "./ThemeToggle";

const LABELS: Record<string, string> = {
  "": "Dashboard",
  traces: "Traces",
  cases: "Regression cases",
  gates: "CI gates",
  clusters: "Failure clusters",
};

function crumbs(path: string) {
  const segs = path.split("/").filter(Boolean);
  if (segs.length === 0) return [{ href: "/dashboard", label: "Dashboard" }];
  const out: { href: string; label: string }[] = [];
  let acc = "";
  segs.forEach((seg, i) => {
    acc += "/" + seg;
    const isId = i > 0;
    out.push({
      href: acc,
      label: isId ? seg.slice(0, 12) + (seg.length > 12 ? "…" : "") : LABELS[seg] ?? seg,
    });
  });
  return out;
}

export function Topbar() {
  const path = usePathname();
  const items = crumbs(path);
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-line bg-ink/70 px-8 backdrop-blur-md">
      <nav className="flex items-center gap-1.5 text-[13px]">
        {items.map((c, i) => (
          <Fragment key={c.href}>
            {i > 0 && <IconChevron className="h-3.5 w-3.5 text-fg-faint" />}
            <a
              href={c.href}
              className={
                i === items.length - 1
                  ? "font-medium text-fg" + (i > 0 ? " font-mono text-[12px]" : "")
                  : "text-fg-muted transition-colors hover:text-fg"
              }
            >
              {c.label}
            </a>
          </Fragment>
        ))}
      </nav>
      <div className="flex items-center gap-3">
        <button
          onClick={() => window.dispatchEvent(new Event("tracely:cmdk"))}
          className="hidden items-center gap-2 rounded-lg border border-line bg-ink-800 px-3 py-1.5 text-[12px] text-fg-faint transition-colors hover:border-signal/40 hover:text-fg-muted sm:flex"
        >
          <IconSearch className="h-3.5 w-3.5" />
          <span>Search…</span>
          <kbd className="ml-1 rounded border border-line bg-ink-700 px-1.5 py-0.5 font-mono text-[10px] text-fg-muted">
            ⌘K
          </kbd>
        </button>
        <OnboardingQuest />
        <ThemeToggle />
      </div>
    </header>
  );
}
