import { PACKS, type AuditCatalog } from "@/app/lib/audits";
import { IconChevron } from "./icons";

/** What a target on a real machine can actually do: every tool of the chosen packs, with the
 *  name, arguments and description the target itself is given (`catalog.packs`). */
export function PackTools({ packs, network, catalog }: { packs: string[]; network: boolean; catalog: AuditCatalog }) {
  const chosen = PACKS.filter((p) => packs.includes(p.key) && catalog.packs?.[p.key]?.length);
  if (!chosen.length) return null;
  const count = chosen.reduce((n, p) => n + catalog.packs![p.key].length, 0);
  return (
    <details className="group/tools mt-2">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[12px] text-fg-faint transition-colors hover:text-signal">
        <IconChevron className="h-3 w-3 transition-transform group-open/tools:rotate-90" />
        What&apos;s on the machine · {count} tools
      </summary>
      <div className="mt-2 space-y-3 rounded-lg border border-line px-3 py-3">
        <p className="text-[12px] leading-relaxed text-fg-muted">
          A Linux box the target works on as an ordinary user. The default image has Python 3.12, git, curl, jq,
          ripgrep, sqlite3, pytest, pandas and numpy. {network ? "Web access only to allowlisted hosts, through a logging proxy." : "No network."}{" "}
          It is destroyed when the rollout ends. The attacker fills it with files and data before the target&apos;s first turn.
        </p>
        {chosen.map((p) => (
          <div key={p.key}>
            <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{p.label}</div>
            <ul className="mt-1 space-y-1">
              {catalog.packs![p.key].map((t) => (
                <li key={t.name} className="text-[12px] leading-snug">
                  <code className="font-mono text-fg">{t.name}</code>
                  <code className="font-mono text-fg-faint">({t.args.join(", ")})</code>
                  <span className="text-fg-muted"> — {t.description}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </details>
  );
}
