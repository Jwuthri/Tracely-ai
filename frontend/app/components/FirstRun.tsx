import { Activation } from "@/app/components/Activation";
import { getEvaluators, getGates, getMilestones, getStats } from "@/app/lib/api";
import { getMe } from "@/app/lib/auth";

/** The first-run checklist, mounted on the landing page (/clusters) now that the Overview page it
 *  lived on is gone. Fetches for itself so the page streams around it; renders nothing once the
 *  loop is closed (Activation returns null) and never blocks the page if a roll-up fails. */
export async function FirstRun() {
  try {
    const [stats, evaluators, gates, me, milestones] = await Promise.all([
      getStats(),
      getEvaluators(),
      getGates(1),
      getMe(),
      getMilestones(),
    ]);
    return (
      <Activation
        traces={stats.traces}
        evaluators={evaluators.length}
        failures={stats.auto_failures}
        clusters={stats.open_clusters}
        cases={stats.cases}
        gates={gates.total}
        ingestKey={me?.ingest_keys?.[0] ?? "<your-ingest-key>"}
        endpoint={process.env.NEXT_PUBLIC_TRACELY_PUBLIC_API ?? "http://localhost:8000"}
        milestones={milestones}
      />
    );
  } catch {
    return null;
  }
}
