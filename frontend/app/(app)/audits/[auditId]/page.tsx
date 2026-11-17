import Link from "next/link";
import { notFound } from "next/navigation";
import { AuditDetail } from "@/app/components/AuditDetail";
import { DocLink } from "@/app/components/DocLink";
import { IconArrowLeft } from "@/app/components/icons";
import { getAudit } from "@/app/lib/api";

export default async function AuditPage({ params }: { params: Promise<{ auditId: string }> }) {
  const { auditId } = await params;
  const audit = await getAudit(auditId);
  if (!audit) notFound();

  return (
    <div className="space-y-6">
      <header className="reveal">
        <Link href="/audits" className="inline-flex items-center gap-1.5 font-mono text-[11.5px] text-fg-faint transition-colors hover:text-signal">
          <IconArrowLeft className="h-3.5 w-3.5" /> Audits
        </Link>
        <div className="mt-2 flex items-center gap-3">
          <h1 className="font-display text-[26px] font-extrabold tracking-tight">{audit.name}</h1>
          <DocLink path="/product/audits" />
        </div>
        {audit.behavior && <p className="mt-1.5 max-w-3xl text-[14px] text-fg-muted">{audit.behavior}</p>}
      </header>
      <AuditDetail audit={audit} />
    </div>
  );
}
