import { DocLink } from "@/app/components/DocLink";
import { getClusters, getLlmKeyConfigured, PAGE_SIZE } from "@/app/lib/api";
import { Badge } from "@/app/components/ui";
import { ClusterList } from "@/app/components/ClusterList";
import { Pager, pageParam } from "@/app/components/Pager";
import { RebuildButton } from "@/app/components/RebuildButton";

export default async function ClustersPage({
  searchParams,
}: {
  searchParams: Promise<{ min_size?: string; page?: string }>;
}) {
  const sp = await searchParams;
  const raw = Number(sp.min_size);
  // ponytail: plain GET form -> URL -> server re-fetch. No client state, back button works.
  const minSize = Number.isFinite(raw) && raw >= 2 ? Math.floor(raw) : undefined;
  const page = pageParam(sp.page);
  // `open` comes from the server as a project-wide count — deriving it from the page would make
  // the badge mean "open on this page".
  const [{ items: clusters, total, open }, hasLlmKey] = await Promise.all([
    getClusters(minSize, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    getLlmKeyConfigured(),
  ]);
  const qs = (p: number) =>
    `/clusters?${new URLSearchParams({
      ...(minSize ? { min_size: String(minSize) } : {}),
      page: String(p),
    }).toString()}`;
  return (
    <div className="space-y-6">
      <header className="reveal flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-3"><h1 className="font-display text-[26px] font-extrabold tracking-tight">Failure clusters</h1><DocLink path="/product/clusters" /></div>
          <p className="mt-1.5 max-w-2xl text-[14px] text-fg-muted">
            Auto-detected failures grouped into issues — run <span className="text-fg">Analyze</span> to
            cluster with embeddings + LLM agents, then promote an issue into a regression test.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <form className="flex items-center gap-2 text-[13px] text-fg-muted">
            <label htmlFor="min_size">Min occurrences</label>
            <input
              id="min_size"
              name="min_size"
              type="number"
              min={2}
              step={1}
              defaultValue={minSize ?? 5}
              className="w-16 rounded-md border border-line bg-ink-900 px-2 py-1 text-[13px] text-fg"
            />
            <button type="submit" className="btn-ghost">
              Apply
            </button>
          </form>
          <Badge variant="warn">{open} open</Badge>
          <RebuildButton hasLlmKey={hasLlmKey} />
        </div>
      </header>

      {/* rows + multi-select delete (client) */}
      <ClusterList clusters={clusters} />

      <Pager page={page} pageSize={PAGE_SIZE} total={total} label="clusters" href={qs} />
    </div>
  );
}
