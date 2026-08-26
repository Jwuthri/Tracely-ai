"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Me } from "@/app/lib/auth/types";
import { AccountMenu } from "./AccountMenu";
import { DOCS_URL } from "@/app/lib/site";
import { IconActivity, IconBell, IconBolt, IconBook, IconCard, IconDatabase, IconGate, IconGrid, IconLayers, IconScale, IconSettings, IconShield, IconTrend, IconUsers } from "./icons";

type NavItem = { href: string; label: string; Icon: typeof IconGrid; exact?: boolean; external?: boolean };

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Observe",
    items: [
      { href: "/dashboard", label: "Dashboard", Icon: IconGrid, exact: true },
      { href: "/traces", label: "Traces", Icon: IconActivity },
      { href: "/trends", label: "Trends", Icon: IconTrend },
    ],
  },
  {
    group: "Triage",
    items: [{ href: "/clusters", label: "Failure clusters", Icon: IconLayers }],
  },
  {
    group: "Test",
    items: [
      { href: "/cases", label: "Regression cases", Icon: IconShield },
      { href: "/scenarios", label: "Scenarios", Icon: IconBolt },
      { href: "/calibration", label: "Judge calibration", Icon: IconScale },
    ],
  },
  {
    group: "Ship",
    items: [{ href: "/gates", label: "CI gates", Icon: IconGate }],
  },
  {
    group: "Configure",
    items: [
      { href: "/settings/api-keys", label: "API keys", Icon: IconSettings },
      { href: "/settings/alerts", label: "Alerts", Icon: IconBell },
      { href: "/settings/data", label: "Data", Icon: IconDatabase },
      { href: "/settings/team", label: "Team", Icon: IconUsers },
      // The ONLY route to billing — deliberately not duplicated in the AccountMenu dropdown,
      // which also never renders in clerk mode (ClerkUserButton replaces it).
      { href: "/settings/billing", label: "Usage & billing", Icon: IconCard },
    ],
  },
  {
    group: "Learn",
    // How every screen works, with screenshots — each page also carries its own "Docs ↗" pill.
    items: [{ href: `${DOCS_URL}/product`, label: "Documentation", Icon: IconBook, external: true }],
  },
];

function Mark() {
  return (
    <div className="relative grid h-9 w-9 place-items-center rounded-[11px] border border-signal/30 bg-signal/10 shadow-[0_0_22px_-6px_rgb(var(--c-signal)/0.7)]">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
        <path d="M12 2 22 12 12 22 2 12Z" stroke="rgb(var(--c-signal))" strokeWidth="1.8" strokeLinejoin="round" />
        <circle cx="12" cy="12" r="2.7" fill="rgb(var(--c-signal))" />
      </svg>
    </div>
  );
}

export function Sidebar({ me }: { me: Me | null }) {
  const path = usePathname();
  return (
    <aside className="sticky top-0 hidden h-screen w-[244px] shrink-0 flex-col border-r border-line bg-ink-900/80 backdrop-blur-md md:flex">
      <a href="/" className="flex items-center gap-3 px-5 pb-6 pt-6 transition-opacity hover:opacity-80">
        <Mark />
        <div className="leading-none">
          <div className="font-display text-[19px] font-extrabold tracking-tight text-fg">Tracely</div>
          <div className="mt-1.5 font-mono text-[9.5px] uppercase tracking-[0.22em] text-fg-faint">
            trace-native ci/cd
          </div>
        </div>
      </a>

      <nav className="flex-1 space-y-7 px-3 py-1">
        {NAV.map((sec) => (
          <div key={sec.group}>
            <div className="px-3 pb-2 font-mono text-[10px] uppercase tracking-[0.2em] text-fg-faint">
              {sec.group}
            </div>
            <div className="space-y-0.5">
              {sec.items.map(({ href, label, Icon, exact, external }) => {
                const active = !external && (exact ? path === href : path === href || path.startsWith(href + "/"));
                // A plain <a> for an in-app route is a full document reload: it throws away the
                // layout, the JS bundle and everything holding client state — including a live
                // assistant voice call. `Link` navigates in-place; external links keep the <a>.
                const Tag = external ? "a" : Link;
                return (
                  <Tag
                    key={href}
                    href={href}
                    target={external ? "_blank" : undefined}
                    rel={external ? "noreferrer" : undefined}
                    className={clsx(
                      "group relative flex items-center gap-3 rounded-lg px-3 py-2 text-[13.5px] transition-colors",
                      active ? "bg-signal/10 text-fg" : "text-fg-muted hover:bg-hilite/[0.03] hover:text-fg",
                    )}
                  >
                    {active && (
                      <span className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-signal shadow-[0_0_8px_rgb(var(--c-signal)/0.7)]" />
                    )}
                    <Icon
                      className={clsx(
                        "h-[17px] w-[17px] transition-colors",
                        active ? "text-signal" : "text-fg-faint group-hover:text-fg-muted",
                      )}
                    />
                    {label}
                    {external && <span className="ml-auto text-fg-faint" aria-hidden>↗</span>}
                  </Tag>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="border-t border-line px-4 py-4">
        <AccountMenu me={me} />
        <div className="mt-3 px-1 font-mono text-[10px] text-fg-faint">v0.1.0 · MVP</div>
      </div>
    </aside>
  );
}
