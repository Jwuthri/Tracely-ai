import { AuditsManager } from "@/app/components/AuditsManager";
import { DocLink } from "@/app/components/DocLink";
import { getAgents, getAuditCatalog, getAudits } from "@/app/lib/api";

export default async function AuditsPage() {
  const [audits, catalog, agents] = await Promise.all([getAudits(), getAuditCatalog(), getAgents()]);

  return (
    <div className="space-y-6">
      <header className="reveal">
        <div className="flex items-center gap-3">
          <h1 className="font-display text-[26px] font-extrabold tracking-tight">Audits</h1>
          <DocLink path="/product/audits" />
        </div>
        <p className="mt-1.5 max-w-3xl text-[14px] text-fg-muted">
          Go looking for a behaviour instead of waiting for it: an attacker model tries to provoke
          it, and judges score how often it appears.
        </p>
      </header>

      <AuditsManager initial={audits} catalog={catalog} agents={agents} />
    </div>
  );
}
