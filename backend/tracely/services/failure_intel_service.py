"""Failure-intelligence pipeline: embed failing runs -> pgvector -> UMAP+HDBSCAN ->
per-cluster agent (semantic description) -> meta-consolidation agent (Issues).

Triggered on demand ("Analyze failures"). Replaces the cheap signature clusters with
embedding+LLM Issues. Needs `OPENROUTER_API_KEY`. All heavy imports are lazy.

The orchestration is a class (`FailureIntelService`) because there's a lot of shared state per
rebuild (the embedder, cluster engine, the per-trace summary and embed-text caches). Pure
pieces moved out: `domain.failure.text.embedding_text` / `summarize_failure`,
`domain.failure.clustering.ClusterEngine`, `infrastructure.llm.embeddings.Embedder`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tracely.domain.failure.clustering import ClusterEngine
from tracely.domain.failure.signature import text_part
from tracely.domain.failure.text import embedding_text, summarize_failure
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.spans import TraceReader
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import (
    ClusterMember,
    FailureCluster,
    FailureEmbedding,
)
from tracely.infrastructure.llm import analysis_agents as agents
from tracely.infrastructure.llm import provider
from tracely.infrastructure.llm.embeddings import Embedder, embeddings_enabled

log = structlog.get_logger()


class FailureIntelService:
    """Embedding-based clustering + LLM analysis for failing traces."""

    def __init__(
        self,
        trace_reader: TraceReader | None = None,
        embedder: Embedder | None = None,
        cluster_engine: ClusterEngine | None = None,
    ) -> None:
        self.trace_reader = trace_reader or TraceReader()
        self.embedder = embedder or Embedder()
        self.cluster_engine = cluster_engine or ClusterEngine()

    def rebuild_clusters(self, project_id: str) -> dict:
        """Recompute the per-agent embedding clusters for this project."""
        with provider.use_project_key(project_id):
            if not embeddings_enabled():
                return {"error": "OPENROUTER_API_KEY not configured"}

            reasons_by_tid = self.trace_reader.failing_trace_reasons(project_id)
            by_agent = self._group_by_agent(project_id, reasons_by_tid)

            issues = 0
            pruned = 0
            with SyncSessionLocal() as s:
                # drop clusters for agents with no current failing traces, then rebuild the rest —
                # keeps the project consistent instead of accumulating stale clusters from agents
                # that dropped out.
                pruned = self._prune_orphan_clusters(s, project_id, set(by_agent))
                for agent_id, items in by_agent.items():
                    issues += self._rebuild_agent(s, project_id, agent_id, items)
            log.info(
                "fi_rebuild", project_id=project_id, agents=len(by_agent), issues=issues, pruned=pruned
            )
            return {"agents": len(by_agent), "issues": issues, "pruned": pruned}

    # ── grouping + persistence helpers ────────────────────────────────────────

    def _group_by_agent(
        self, project_id: str, reasons_by_tid: dict[str, list[tuple[str, str]]]
    ) -> dict[str, list[tuple[str, str, str]]]:
        """For each failing trace, produce `(trace_id, summary_text, embedding_text)` and
        group by `agent_id`. Drops traces with no agent or no spans."""
        by_agent: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for tid, reasons in reasons_by_tid.items():
            spans = self.trace_reader.read_spans(project_id, tid)
            if not spans:
                continue
            agent_id = root_span(spans).get("agent_id")
            if not agent_id:
                continue
            by_agent[agent_id].append(
                (
                    tid,
                    summarize_failure(spans, reasons),
                    embedding_text(spans),
                )
            )
        return by_agent

    @staticmethod
    def _delete_clusters(session: Session, cluster_ids: list[str]) -> int:
        """Delete the given FailureClusters and their ClusterMembers (no commit — the caller
        owns the transaction). Returns the number of clusters removed."""
        if not cluster_ids:
            return 0
        session.execute(delete(ClusterMember).where(ClusterMember.cluster_id.in_(cluster_ids)))
        session.execute(delete(FailureCluster).where(FailureCluster.id.in_(cluster_ids)))
        return len(cluster_ids)

    @classmethod
    def _prune_orphan_clusters(
        cls, session: Session, project_id: str, keep_agent_ids: set[str]
    ) -> int:
        """Drop every cluster in this project whose agent is NOT being rebuilt this run, so the
        rebuild is project-wide-consistent. An empty `keep_agent_ids` means nothing is failing,
        so ALL of the project's clusters are pruned."""
        q = select(FailureCluster.id).where(FailureCluster.project_id == project_id)
        if keep_agent_ids:
            q = q.where(FailureCluster.agent_id.not_in(keep_agent_ids))
        orphan_ids = list(session.execute(q).scalars().all())
        n = cls._delete_clusters(session, orphan_ids)
        if n:
            session.commit()
        return n

    # ── the per-agent rebuild (large, but cohesive — kept as one method) ──────

    def _rebuild_agent(
        self,
        session: Session,
        project_id: str,
        agent_id: str,
        items: list[tuple[str, str, str]],
    ) -> int:
        summary_by_tid = {tid: full for tid, full, _ in items}
        embed_by_tid = {tid: emb for tid, _, emb in items}

        # 1. embed (cache hits via `failure_embeddings`; the vector is keyed to the mechanism text)
        vec_by_tid = self._embed_with_cache(
            session, project_id, agent_id, items, summary_by_tid, embed_by_tid
        )

        # 2. cluster
        tids = [tid for tid, _, _ in items]
        labels = self.cluster_engine.labels_for([vec_by_tid[t] for t in tids])
        raw: dict[int, list[str]] = defaultdict(list)
        for tid, lab in zip(tids, labels):
            if lab != -1:  # drop HDBSCAN noise
                raw[lab].append(tid)
        if not raw:
            # every failing trace was HDBSCAN noise -> no clusters this run; clear stale ones.
            stale = list(
                session.execute(
                    select(FailureCluster.id).where(
                        FailureCluster.project_id == project_id,
                        FailureCluster.agent_id == agent_id,
                    )
                )
                .scalars()
                .all()
            )
            if self._delete_clusters(session, stale):
                session.commit()
            return 0

        # 3. per-cluster analysis agent
        analyses = []  # (ClusterAnalysis, [tids])
        for ctids in raw.values():
            text = "\n\n".join(f"[trace {t}]\n{summary_by_tid[t]}" for t in ctids[:15])
            analyses.append((agents.analyze_cluster(text), ctids))

        # 4. meta-consolidation -> Issues
        if len(analyses) > 1:
            briefs = [
                {"index": i, "title": a.title, "description": a.description, "taxonomy": a.taxonomy}
                for i, (a, _) in enumerate(analyses)
            ]
            groups = agents.consolidate(briefs).issues
        else:
            a0 = analyses[0][0]
            groups = [
                agents.IssueGroup(
                    title=a0.title,
                    description=a0.description,
                    member_cluster_indices=[0],
                )
            ]

        # 5. replace ALL of this agent's clusters with the consolidated Issues, carrying over
        #    promotion / ignore state (and linked case + first-seen time) from any old cluster
        #    whose traces overlap a new issue. This collapses cheap signature clusters into the
        #    richer embedding Issues without losing a promotion or duplicating it.
        return self._replace_with_issues(session, project_id, agent_id, analyses, groups)

    def _embed_with_cache(
        self,
        session: Session,
        project_id: str,
        agent_id: str,
        items: list[tuple[str, str, str]],
        summary_by_tid: dict[str, str],
        embed_by_tid: dict[str, str],
    ) -> dict[str, list[float]]:
        """Embed mechanism texts that aren't already cached in `failure_embeddings`."""
        vec_by_tid: dict[str, list[float]] = {}
        to_embed = []
        for tid, _, _ in items:
            row = session.get(FailureEmbedding, tid)
            if row is not None:
                vec_by_tid[tid] = row.embedding
            else:
                to_embed.append(tid)
        if to_embed:
            vecs = self.embedder.embed([embed_by_tid[t] for t in to_embed])
            now = datetime.now(timezone.utc)
            for tid, vec in zip(to_embed, vecs):
                session.merge(
                    FailureEmbedding(
                        trace_id=tid,
                        project_id=project_id,
                        agent_id=agent_id,
                        summary=summary_by_tid[tid][:4000],
                        embedding=vec,
                        created_at=now,
                    )
                )
                vec_by_tid[tid] = vec
            session.commit()
        return vec_by_tid

    def _replace_with_issues(
        self,
        session: Session,
        project_id: str,
        agent_id: str,
        analyses: list[tuple],
        groups,
    ) -> int:
        existing = (
            session.execute(
                select(FailureCluster).where(
                    FailureCluster.project_id == project_id,
                    FailureCluster.agent_id == agent_id,
                )
            )
            .scalars()
            .all()
        )
        # (id, status, candidate_case_id, first_seen_at, {trace_ids}) — only carried-over fields
        old_state: list[tuple[str, str, str | None, datetime | None, set[str]]] = []
        # Masked failure text of each old cluster, so the replacement issues keep a joinable
        # signature. With `signature=""` a new structural failure could never fuzzy-match an
        # embedding cluster and every failure after an Analyze re-opened a duplicate row.
        old_texts: dict[str, str] = {ec.id: text_part(ec.signature or "") for ec in existing}
        for ec in existing:
            mtids = set(
                session.execute(
                    select(ClusterMember.trace_id).where(ClusterMember.cluster_id == ec.id)
                )
                .scalars()
                .all()
            )
            old_state.append((ec.id, ec.status, ec.candidate_case_id, ec.first_seen_at, mtids))
        old_ids = [ec.id for ec in existing]
        if old_ids:
            # Stage the delete but DON'T commit here — the old carried-over state is captured in
            # `old_state` above, and committing now would expose a window where the project has ZERO
            # clusters (and a crash before the inserts would wipe all promote/ignore state). The new
            # rows use fresh uuids, so there's no PK conflict with the staged deletes. One commit at
            # the end makes the whole replace atomic.
            session.execute(delete(ClusterMember).where(ClusterMember.cluster_id.in_(old_ids)))
            session.execute(delete(FailureCluster).where(FailureCluster.id.in_(old_ids)))
            session.flush()

        consumed: set[str] = set()

        def _match(member_tids: set[str]) -> tuple | None:
            best, best_ov = None, 0
            for rec in old_state:
                cid, status, _case, _first, tids = rec
                if cid in consumed or status not in ("PROMOTED", "IGNORED") or not tids:
                    continue
                ov = len(member_tids & tids)
                if ov > best_ov and ov >= len(tids) / 2:
                    best, best_ov = rec, ov
            if best:
                consumed.add(best[0])
            return best

        now = datetime.now(timezone.utc)
        written = 0
        for g in groups:
            idxs = [i for i in g.member_cluster_indices if 0 <= i < len(analyses)]
            if not idxs:
                continue
            member_tids: set[str] = set()
            per_trace: dict[str, str] = {}
            for i in idxs:
                analysis, ctids = analyses[i]
                member_tids.update(ctids)
                for ts in analysis.trace_summaries:
                    per_trace[ts.trace_id] = ts.summary
            first = analyses[idxs[0]][0]
            match = _match(member_tids)
            status = match[1] if match else "OPEN"
            case_id = match[2] if match else None
            seen = match[3] if (match and match[3]) else now
            # No evaluator half (" ## " prefix): a synthesized signature gates on text alone.
            merged_texts = sorted(
                {t for cid, _st, _c, _f, tids in old_state if tids & member_tids and (t := old_texts.get(cid))}
            )
            cl = FailureCluster(
                id=str(uuid.uuid4()),
                project_id=project_id,
                agent_id=agent_id,
                cluster_key=hashlib.sha256(g.title.encode()).hexdigest()[:16],
                label=g.title,
                taxonomy=first.taxonomy,
                signature=(" ## " + " || ".join(merged_texts))[:2000] if merged_texts else "",
                description=g.description,
                proposed_fix=first.proposed_fix,
                severity=first.severity,
                method="embedding",
                count=len(member_tids),
                status=status,
                candidate_case_id=case_id,
                first_seen_at=seen,
                last_seen_at=now,
            )
            session.add(cl)
            session.flush()
            for j, tid in enumerate(sorted(member_tids)):
                session.add(
                    ClusterMember(
                        cluster_id=cl.id,
                        trace_id=tid,
                        is_medoid=(j == 0),
                        summary=per_trace.get(tid, ""),
                    )
                )
            written += 1
        session.commit()
        return written
