import { DocLink } from "@/app/components/DocLink";
import { getAgents, getSessions, getSessionsCount } from "@/app/lib/api";
import { TracesExplorer, queryFromView, viewFromParams } from "@/app/components/TracesExplorer";

// First page is rendered server-side for fast first paint; TracesExplorer pages/filters from there.
// The URL carries the view (range/agent/sort/filter/q), so a shared link renders the same list.
const PAGE = 50;

export default async function TracesPage({ searchParams }: { searchParams: Promise<Record<string, string | undefined>> }) {
  const view = viewFromParams(await searchParams);
  const query = queryFromView(view);
  const [threads, agents, total] = await Promise.all([
    getSessions({ ...query, limit: PAGE }),
    getAgents(),
    getSessionsCount(query),
  ]);
  return (
    <div className="space-y-6">
      <header className="reveal">
        <div className="flex items-center gap-3"><h1 className="font-display text-[26px] font-extrabold tracking-tight">Traces</h1><DocLink path="/product/traces" /></div>
        <p className="mt-1.5 text-[14px] text-fg-muted">
          Agent runs grouped into conversation threads — expand any conversation into its messages and steps.
        </p>
      </header>

      <TracesExplorer initial={threads} initialTotal={total} initialView={view} pageSize={PAGE} hasMore={threads.length === PAGE} agents={agents} />
    </div>
  );
}
