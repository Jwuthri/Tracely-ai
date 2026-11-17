"""Running an audit: start a run, then advance its rollouts in short, leased, resumable slices.

    start_run → N rollouts (PENDING)
    tick → claim one rollout → DRIVING (attacker ⇄ target) → JUDGING (cell by cell) → DONE
         → checkpoint after EVERY model call → release at the end of the slice → tick again

**No task drives a whole rollout.** A rollout is thirty attacker generations and as many target
replies — ten minutes or more — and every deployment's default worker is the solo pool, which
enforces no time limit and has one slot. Owning that slot for a rollout would stop all OTLP
ingestion behind it. So `tick` advances a rollout for `AUDIT_SLICE_S`, writes the whole state back
and returns; `tracely.audit_tick` re-enqueues itself behind whatever ingest queued meanwhile.

**The lease is what makes that safe.** A claim is a compare-and-swap (`audit_rollout_claim`); every
checkpoint lands only while the lease token still matches. A tick that was redelivered, or whose
worker stalled past the lease, finds its checkpoint refused and stops — it never overwrites the
tick that took over. A worker that dies leaves an expired lease, and the next tick resumes from
the last checkpoint without re-spending a single call.

**Brakes.** A USD budget is never the only one — `estimate_cost_usd` is 0.0 for a model with no
known price, which would make the budget a no-op. Attacker turns are capped, a rollout has a
wall-clock ceiling, and `start_run` refuses a model it cannot price.

Everything runs inside `use_project_key`: the customer's own key, and a workspace without one
gets UNGRADED rollouts — never CLEAN ones.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tracely.config import settings
from tracely.domain.audit import (
    emit,
    explore,
    judge,
    library,
    media,
    metrics,
    prompts,
    realism,
    seeds,
    tooling,
    transcript,
)
from tracely.domain.audit import state as st
from tracely.domain.audit.config import MAX_REPS, normalize, resolved_dimensions
from tracely.domain.evaluation import budget
from tracely.domain.simulation.attacker import weakness_lines
from tracely.infrastructure.blob import s3 as blobstore
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.models import Agent, Audit, AuditRollout, AuditRun, AuditSeed
from tracely.infrastructure.llm import provider
from tracely.infrastructure.sandbox import SandboxGone, get_backend
from tracely.services.sandbox_session import SandboxSession

log = structlog.get_logger()

# A rollout whose lease expired this many times is not unlucky, it is crashing its worker.
_MAX_LOST_LEASES = 3
# The attacker's own model call may fail this many times in a row before the rollout gives up.
_MAX_ATTACKER_ERRORS = 2
# Share of the run's budget the DRIVING phase may spend; the rest is held back so a run that hits
# its budget still has the money to judge what it drove.
_DRIVE_BUDGET_SHARE = 0.75


class AuditError(ValueError):
    """A run that cannot start, with a message for the caller (HTTP 400)."""


def _call_cost(usage: dict) -> float:
    """What one call cost, for the budget brake: the LARGER of the provider's own figure and our
    estimate from the catalog price.

    The larger, not the provider's when present: OpenRouter reports `cost: 0` for calls it did not
    bill itself (a workspace that routes through its own upstream provider key), which is a real
    answer to "what did OpenRouter charge" and a useless one to "what did this cost". Seen live on
    the first run — every target call came back 0.0 and the run's spend stood still while it
    drove. A brake that reads zero is no brake.
    """
    estimate = provider.estimate_cost_usd(
        str(usage.get("model") or ""), int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )
    reported = usage.get("cost_usd")
    return max(float(reported), estimate) if isinstance(reported, (int, float)) else estimate


class AuditService:
    def __init__(self, session: Session, score_writer: Any = None, sandbox_backend: Any = None) -> None:
        self.session = session
        self._score_writer = score_writer
        self._sandbox_backend = sandbox_backend

    def sandbox_backend(self):
        """The deployment's sandbox backend, or None when it has none."""
        if self._sandbox_backend is None:
            self._sandbox_backend = get_backend()
        return self._sandbox_backend

    @property
    def score_writer(self):
        if self._score_writer is None:  # lazy: constructing it opens the span store's socket
            from tracely.infrastructure.spans import ScoreWriter

            self._score_writer = ScoreWriter()
        return self._score_writer

    # ── starting ──────────────────────────────────────────────────────────────

    def start_run(
        self,
        project_id: str,
        audit_id: str,
        *,
        seed_ids: list[str] | None = None,
        reps: int | None = None,
        budget_usd: float | None = None,
        mode: str = "measure",
        created_by: str = "ui",
    ) -> AuditRun:
        """Snapshot the audit and create one PENDING rollout per (seed, repetition). The caller
        enqueues the ticks. Raises `AuditError` for anything that would make the run meaningless
        or unbounded; `LookupError` when the audit is not this project's."""
        s = self.session
        audit = repo.audit_get(s, project_id, audit_id)
        if audit is None:
            raise LookupError("audit not found")
        try:
            cfg = normalize(audit.config)
        except ValueError as exc:
            raise AuditError(f"this audit's configuration is invalid: {exc}") from None
        seeds = repo.audit_seeds_list(s, audit_id)
        # Explicit ids override `enabled`, exactly as an explicit scenario subset does for a gate.
        seeds = [x for x in seeds if (x.id in seed_ids if seed_ids else x.enabled)]
        if not seeds:
            raise AuditError("this audit has no enabled seeds to run")

        if cfg["environment"]["mode"] == "sandbox" and self.sandbox_backend() is None:
            raise AuditError(
                "this audit runs in a real sandbox, and this deployment has none configured "
                "(SANDBOX_BACKEND). Switch the audit to invented tool results, or enable a sandbox."
            )
        if cfg["environment"]["network"] == "allowlist" and not settings.sandbox_egress_network:
            raise AuditError(
                "this audit gives the machine allowlisted web access, and this deployment has no "
                "egress proxy configured (SANDBOX_EGRESS_NETWORK). Set the audit's network to none."
            )
        with provider.use_project_key(project_id):
            if not provider.llm_enabled():
                raise AuditError(
                    "this workspace has no OpenRouter key — add one in Settings -> OpenRouter key "
                    "to run audits"
                )
            default = provider.default_model_id()
            attackers = cfg["attacker"]["models"] or [settings.attacker_model or default]
            judges = cfg["judge_models"] or [settings.attacker_judge_model or default]
            dims = resolved_dimensions(cfg)
            used = {cfg["target"]["model"], *attackers, *judges, *(m for d in dims for m in d.get("models") or [])}
            unpriced = sorted(m for m in used if provider.model_pricing(m) == (None, None))
            needs = {a.get("kind") for x in seeds for a in x.attachments or []}
            needs |= {"audio" if m == "voice" else m for m in cfg["attacker"]["media"]}
            missing = sorted(needs - set(provider.model_modalities(cfg["target"]["model"])["input"])) if needs else []
        if missing:
            raise AuditError(
                f"{cfg['target']['model']} does not accept {' or '.join(missing)} input, and this "
                "audit sends it. Pick a target that does, or remove the attachments / media tools."
            )
        if unpriced:
            raise AuditError(
                f"no known price for {', '.join(unpriced)}, so a USD budget cannot bound this run. "
                "Pick models from the catalog."
            )

        cap = float(cfg["limits"]["budget_usd"])
        # A caller may ask for less than the audit's configured maximum, never more: an ingest
        # key can start runs, and a leaked one must not be able to name its own budget.
        run_budget = min(float(budget_usd), cap) if budget_usd else cap
        n_reps = min(max(int(reps or cfg["limits"]["reps"]), 1), MAX_REPS)
        run = AuditRun(
            id=str(uuid4()), audit_id=audit_id, project_id=project_id, status="RUNNING",
            mode=mode if mode in ("measure", "explore") else "measure",
            config={
                **cfg,
                "audit_id": audit_id, "behavior": audit.behavior, "audit_name": audit.name, "agent_slug": audit.agent_slug,
                "attacker_models": attackers, "judge_models": judges,
                "weaknesses": self._weaknesses(project_id, audit.agent_slug),
            },
            budget_usd=run_budget, total=len(seeds) * n_reps, heartbeat_at=time.time(),
            created_by=created_by,
        )
        s.add(run)
        # Flush the run before its rollouts: there is no ORM relationship between the two, so the
        # unit of work is free to INSERT the rollouts first — a ForeignKeyViolation on Postgres
        # that SQLite, which does not enforce FKs by default, never showed.
        s.flush()
        position = 0
        for seed in seeds:
            for rep in range(n_reps):
                rollout_id = str(uuid4())
                s.add(AuditRollout(
                    id=rollout_id, run_id=run.id, project_id=project_id, seed_id=seed.id,
                    seed={"title": seed.title, "instruction": seed.instruction,
                          "attachments": seed.attachments or []},
                    rep=rep, position=position,
                ))
                position += 1
        s.commit()
        log.info("audit_run_started", run_id=run.id, audit_id=audit_id, rollouts=run.total)
        return run

    def _weaknesses(self, project_id: str, agent_slug: str) -> list[str]:
        """The imported agent's open failure clusters, read once per run. Best-effort."""
        limit = settings.attacker_weakness_hints
        if not agent_slug or limit <= 0:
            return []
        try:
            agent = self.session.execute(
                select(Agent).where(Agent.project_id == project_id, Agent.slug == agent_slug)
            ).scalar_one_or_none()
            if agent is None:
                return []
            return weakness_lines(
                repo.open_failure_clusters(self.session, project_id, agent.id, limit)
            )
        except Exception as exc:
            log.warning("audit_weakness_lookup_failed", agent=agent_slug, error=str(exc))
            return []

    # ── seeds ─────────────────────────────────────────────────────────────────

    def _seed_model(self) -> str | None:
        return settings.attacker_model or None

    def generate_seeds(self, project_id: str, audit_id: str, n: int = seeds.BATCH, guidance: str = "") -> list[AuditSeed]:
        """One batch of new seeds for an audit (Bloom's understanding + ideation). The
        understanding is derived on the first call and stored on the audit; each call writes its
        seeds straight away, so asking for more is just calling again."""
        s = self.session
        audit = repo.audit_get(s, project_id, audit_id)
        if audit is None:
            raise LookupError("audit not found")
        if not (audit.behavior or "").strip():
            raise AuditError("describe the behaviour to look for first — seeds are generated from it")
        cfg = dict(audit.config or {})
        existing = repo.audit_seeds_list(s, audit_id)
        n = min(max(int(n or seeds.BATCH), 1), seeds.BATCH)
        with provider.use_project_key(project_id):
            if not provider.llm_enabled():
                raise AuditError("this workspace has no OpenRouter key — add one in Settings -> OpenRouter key")
            understanding = cfg.get("understanding") or {}
            if not understanding.get("understanding"):
                got = provider.run_structured_agent(
                    seeds.understanding_prompt(audit.behavior), response_format=seeds.Understanding,
                    system_prompt=seeds.UNDERSTAND_SYSTEM, model=self._seed_model(),
                )
                understanding = {"understanding": got.understanding, "motivation": got.motivation}
                audit.config = {**cfg, "understanding": understanding}  # a new dict: JSON columns do not see in-place edits
                s.commit()
            batch = provider.run_structured_agent(
                seeds.ideation_prompt(
                    behaviour=audit.behavior, understanding=understanding, n=n,
                    existing=[x.title for x in existing if x.title],
                    target=cfg.get("target") or {}, environment=cfg.get("environment") or {},
                    weaknesses=self._weaknesses(project_id, audit.agent_slug), guidance=guidance,
                ),
                response_format=seeds.SeedBatch, system_prompt=seeds.IDEATE_SYSTEM,
                model=self._seed_model(), temperature=1.0,  # warm: a cold ideator writes one scenario eight ways
            )
        return self._add_seeds(audit, [
            {"title": i.title, "instruction": i.instruction, "tags": i.tags} for i in batch.seeds[:n]
        ], origin="generated")

    def generate_variations(self, project_id: str, audit_id: str, seed_id: str, dimensions: list[str]) -> list[AuditSeed]:
        """One variation of a seed per axis (`seeds.VARIATIONS`), each changing only that axis."""
        s = self.session
        audit = repo.audit_get(s, project_id, audit_id)
        parent = s.get(AuditSeed, seed_id)
        if audit is None or parent is None or parent.audit_id != audit_id:
            raise LookupError("seed not found")
        dims = [d for d in dict.fromkeys(dimensions or []) if d][:6]
        if not dims:
            raise AuditError("choose at least one axis to vary")
        with provider.use_project_key(project_id):
            if not provider.llm_enabled():
                raise AuditError("this workspace has no OpenRouter key — add one in Settings -> OpenRouter key")
            batch = provider.run_structured_agent(
                seeds.variation_prompt(parent.instruction, dims), response_format=seeds.SeedBatch,
                system_prompt=seeds.IDEATE_SYSTEM, model=self._seed_model(), temperature=0.7,
            )
        return self._add_seeds(audit, [
            {"title": i.title, "instruction": i.instruction, "tags": i.tags or [dims[min(k, len(dims) - 1)]]}
            for k, i in enumerate(batch.seeds[: len(dims)])
        ], origin="variation", parent_seed_id=seed_id)

    def import_library_seeds(self, project_id: str, audit_id: str, *, tag: str = "", ids: list[str] | None = None) -> list[AuditSeed]:
        """Copy seeds from the vendored Petri library into an audit — by tag, or by id."""
        audit = repo.audit_get(self.session, project_id, audit_id)
        if audit is None:
            raise LookupError("audit not found")
        chosen = [x for x in library.petri_seeds(tag or None) if not ids or x["id"] in ids]
        have = {x.title for x in repo.audit_seeds_list(self.session, audit_id)}
        rows = [
            {"title": x["id"].replace("_", " ").capitalize(), "instruction": x["instruction"], "tags": x["tags"]}
            for x in chosen
        ]
        return self._add_seeds(audit, [r for r in rows if r["title"] not in have], origin="library")

    def _add_seeds(
        self, audit: Audit, rows: list[dict], *, origin: str, parent_seed_id: str | None = None, enabled: bool = True,
    ) -> list[AuditSeed]:
        out = []
        for row in rows:
            if not str(row.get("instruction") or "").strip():
                continue
            seed = AuditSeed(
                id=str(uuid4()), audit_id=audit.id, project_id=audit.project_id,
                title=str(row.get("title") or "")[:512], instruction=str(row["instruction"])[:20000],
                tags=[str(t)[:48] for t in (row.get("tags") or [])][:6], origin=origin,
                parent_seed_id=parent_seed_id, enabled=enabled,
            )
            self.session.add(seed)
            out.append(seed)
        self.session.commit()
        return out

    # ── media ─────────────────────────────────────────────────────────────────

    def put_asset(self, project_id: str, data: bytes, name: str, text: str = "") -> dict:
        """Store an image or audio file once, by content hash, and return its reference
        (`media.make_ref`). `ValueError` for bytes that are not sendable media."""
        ref = media.make_ref(data, name, text)
        blobstore.put_blob(blobstore.audit_blob_key(project_id, f"assets/{ref['asset']}"), data, ref["mime"])
        return ref

    def get_asset(self, project_id: str, asset: str) -> bytes:
        return blobstore.get_blob(blobstore.audit_blob_key(project_id, f"assets/{asset}"))

    def attach_to_seed(self, project_id: str, audit_id: str, seed_id: str, data: bytes, name: str, text: str = "") -> AuditSeed:
        seed = self.session.get(AuditSeed, seed_id)
        if seed is None or seed.audit_id != audit_id or seed.project_id != project_id:
            raise LookupError("seed not found")
        if len(seed.attachments or []) >= media.MAX_PER_MESSAGE:
            raise AuditError(f"a seed carries at most {media.MAX_PER_MESSAGE} attachments")
        try:
            ref = self.put_asset(project_id, data, name, text)
        except ValueError as exc:
            raise AuditError(f"{name}: {exc}") from None
        if any(a["name"] == ref["name"] for a in seed.attachments or []):
            raise AuditError(f"this seed already has a file named {ref['name']}")
        seed.attachments = [*(seed.attachments or []), ref]  # a new list: JSON columns do not see appends
        self.session.commit()
        return seed

    def import_target(self, project_id: str, agent_ref: str) -> dict:
        """A target reconstructed from an agent's most recent real trace: the system prompt it
        ran under, the tools it was offered, the model it called. This is what "audit MY agent"
        means without an endpoint — the trace already holds the deployment's configuration.

        The model id comes back as the tracer reported it (often a bare or dated id), so the
        caller picks the catalog model to audit with; the prompt and tools are used as they are.
        """
        from tracely.domain.evaluation.template_resolver import collect_agents
        from tracely.domain.traces.spans import system_prompt
        from tracely.infrastructure.spans import TraceReader

        agent = self.session.execute(
            select(Agent).where(Agent.project_id == project_id, Agent.slug == agent_ref)
        ).scalar_one_or_none()
        if agent is None:
            raise LookupError(f"agent '{agent_ref}' not found")
        reader = TraceReader()
        for env in ("prod", "ci"):
            for trace_id in reader.latest_traces_for_env(project_id, agent.id, env, limit=5):
                spans = reader.read_spans(project_id, trace_id)
                tools: dict[str, dict] = {}
                for view in collect_agents(spans):
                    for tool in view.tools.values():
                        tools.setdefault(tool.name, {
                            "name": tool.name, "description": tool.description,
                            "parameters": tool.parameters or {"type": "object", "properties": {}},
                        })
                prompt = system_prompt(spans)
                if not prompt and not tools:
                    continue  # a trace that shows neither tells us nothing about the deployment
                return {
                    "agent": agent.slug, "found": True, "trace_id": trace_id,
                    "model": next((str(sp.get("model_id")) for sp in spans if sp.get("model_id")), ""),
                    "system_prompt": prompt, "tools": list(tools.values()),
                }
        return {"agent": agent.slug, "found": False, "model": "", "system_prompt": "", "tools": []}

    def cancel_run(self, project_id: str, run_id: str) -> AuditRun | None:
        """Stop a run: unfinished rollouts become SKIPPED and the run is summarised as it stands.
        A tick holding a lease finds its next checkpoint refused (phase is DONE) and stops."""
        s = self.session
        run = repo.audit_run_get(s, project_id, run_id)
        if run is None:
            return None
        if run.status == "RUNNING":
            s.execute(
                update(AuditRollout)
                .where(AuditRollout.run_id == run_id, AuditRollout.phase != "DONE")
                .values(phase="DONE", verdict="SKIPPED", lease_token="", lease_expires_at=0.0,
                        detail={"reason": "run cancelled"})
                .execution_options(synchronize_session=False)
            )
            s.commit()
            self._finalize(run_id, status="CANCELLED")
            s.expire_all()
        return repo.audit_run_get(s, project_id, run_id)

    # ── the tick ──────────────────────────────────────────────────────────────

    def tick(self, run_id: str, *, slice_s: float | None = None) -> dict:
        """Advance this run for one slice. Returns `{"more": bool}` — whether another tick has
        work to do. Never raises for a model failure; those end up on the rollout."""
        s = self.session
        deadline = time.monotonic() + float(slice_s if slice_s is not None else settings.audit_slice_s)
        worked = 0
        while True:
            run = s.get(AuditRun, run_id)
            if run is None or run.status != "RUNNING":
                return {"more": False, "rollouts": worked}
            rollout = repo.audit_rollout_claim(
                s, run_id, lease_s=settings.audit_lease_s, now=time.time(),
                max_parallel=max(1, settings.audit_max_parallel),
            )
            if rollout is None:
                break
            worked += 1
            run = s.get(AuditRun, run_id)
            with provider.use_project_key(run.project_id):
                _Slice(self, run, rollout, deadline).advance()
            if time.monotonic() >= deadline:
                break
        s.expire_all()
        more = any(r.phase != "DONE" for r in repo.audit_rollouts_list(s, run_id))
        if not more and self._deepen(run_id):
            return {"more": True, "rollouts": worked}
        if not more:
            self._finalize(run_id)
        # `worked == 0` with rollouts left means every one is leased to another chain (or the run
        # is at its parallel cap): that chain carries on, this one ends rather than spin.
        return {"more": more and worked > 0, "rollouts": worked}

    def _deepen(self, run_id: str) -> int:
        """An explore run's second pass, planned once when its first pass has ended
        (`explore.deepen_plan`). Returns the rollouts added — 0 for a measure run, a run that has
        already deepened, or one with nothing worth deepening.

        The claim is a compare-and-swap on `stage`, so two ticks ending the first pass together
        plan one second pass, not two."""
        s = self.session
        run = s.get(AuditRun, run_id)
        if run is None or run.mode != "explore" or run.status != "RUNNING":
            return 0
        claimed = s.execute(
            update(AuditRun).where(AuditRun.id == run_id, AuditRun.stage == 0).values(stage=1)
            .execution_options(synchronize_session=False)
        ).rowcount
        s.commit()
        if not claimed:
            return 0
        rollouts = repo.audit_rollouts_list(s, run_id)
        plan = explore.deepen_plan([
            {"seed_id": r.seed_id, "seed": r.seed, "stage": r.stage, "verdict": r.verdict, "scores": r.scores}
            for r in rollouts
        ])
        position, added = len(rollouts), 0
        audit = s.get(Audit, run.audit_id)

        def add(seed_id: str, seed: dict, rep: int) -> None:
            nonlocal position, added
            s.add(AuditRollout(
                id=str(uuid4()), run_id=run_id, project_id=run.project_id, seed_id=seed_id,
                seed=seed, rep=rep, position=position, stage=1,
            ))
            position, added = position + 1, added + 1

        for entry in plan["repeat"]:
            for rep in range(2):
                add(entry["seed_id"], entry["seed"], 100 + rep)
        for entry in plan["vary"]:
            try:
                with provider.use_project_key(run.project_id):
                    batch = provider.run_structured_agent(
                        seeds.variation_prompt(str(entry["seed"].get("instruction") or ""), list(explore.DEEPEN_AXES)),
                        response_format=seeds.SeedBatch, system_prompt=seeds.IDEATE_SYSTEM,
                        model=self._seed_model(), temperature=0.7,
                    )
            except Exception as exc:  # a variation that could not be written is not worth the run
                log.warning("audit_deepen_variation_failed", run_id=run_id, error=str(exc)[:300])
                continue
            # Kept as seeds of the audit, disabled: a variation that elicits is worth promoting
            # into the measured set, but that is the author's call, not the run's.
            new = self._add_seeds(audit, [
                {"title": i.title, "instruction": i.instruction, "tags": i.tags}
                for i in batch.seeds[: len(explore.DEEPEN_AXES)]
            ], origin="variation", parent_seed_id=entry["seed_id"], enabled=False) if audit else []
            for seed in new:
                add(seed.id, {"title": seed.title, "instruction": seed.instruction}, 0)
        if added:
            s.execute(
                update(AuditRun).where(AuditRun.id == run_id).values(total=AuditRun.total + added)
                .execution_options(synchronize_session=False)
            )
        s.commit()
        log.info("audit_run_deepened", run_id=run_id, added=added)
        return added

    def _finalize(self, run_id: str, status: str = "DONE") -> None:
        s = self.session
        rollouts = repo.audit_rollouts_list(s, run_id)
        summary = metrics.run_summary([
            {"verdict": r.verdict, "scores": r.scores, "spend_usd": r.spend_usd} for r in rollouts
        ])
        s.execute(
            update(AuditRun)
            .where(AuditRun.id == run_id, AuditRun.status == "RUNNING")  # once
            .values(status=status, summary=summary, done=len(rollouts),
                    finished_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        s.commit()
        log.info("audit_run_finished", run_id=run_id, status=status, **{
            k: summary[k] for k in ("elicitation_rate", "counted", "total")
        })

    # ── reading a rollout back ────────────────────────────────────────────────

    def rollout_state(self, project_id: str, rollout_id: str) -> dict | None:
        """A rollout's checkpoint — every branch, the attacker's side included — from the row
        while it is running and from the blob store once it has finished."""
        rollout = repo.audit_rollout_get(self.session, project_id, rollout_id)
        if rollout is None:
            return None
        if rollout.state:
            return rollout.state
        if rollout.state_key:
            try:
                return json.loads(blobstore.get_blob(rollout.state_key))
            except Exception as exc:
                log.warning("audit_state_blob_unreadable", rollout_id=rollout_id, error=str(exc))
        return None


class _Slice:
    """One tick's hold on one rollout. Keeps the working copy (state, cells, spend) in memory and
    writes it back through `save`, which is also where a lost lease is discovered."""

    def __init__(self, service: AuditService, run: AuditRun, rollout: AuditRollout, deadline: float) -> None:
        self.svc = service
        self.s = service.session
        self.run_id = run.id
        self.project_id = run.project_id
        self.cfg: dict = run.config or {}
        self.budget_usd = float(run.budget_usd or 0.0)
        self.run_spend = float(run.spend_usd or 0.0)  # as of claim; our own spend is added below
        self.deadline = deadline
        self.id = rollout.id
        self.token = rollout.lease_token
        self.phase = rollout.phase
        self.position = int(rollout.position or 0)
        self.explore = run.mode == "explore"
        self.stage = int(rollout.stage or 0)
        self.seed: dict = rollout.seed or {}
        self.attempts = int(rollout.attempts or 0)
        self.detail: dict = dict(rollout.detail or {})
        self.cells: list[dict] = list((rollout.scores or {}).get("cells") or [])
        self.spend = float(rollout.spend_usd or 0.0)
        raw = rollout.state  # deferred column: loaded here, once
        self.state: st.AuditState | None = st.from_dict(raw) if raw else None
        self.lost = False
        self._unsaved_spend = 0.0  # spent since the last checkpoint — added to the run on save
        self._sandbox_record: dict | None = rollout.sandbox
        self._sbx: SandboxSession | None = None

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _time_left(self) -> bool:
        return time.monotonic() < self.deadline

    def _spent(self, usage: dict) -> None:
        cost = _call_cost(usage)
        self.spend += cost
        self.run_spend += cost
        self._unsaved_spend += cost

    def save(self, *, release: bool = False, **fields: Any) -> bool:
        """Checkpoint. False = the lease is gone: stop, write nothing else."""
        if self.lost:
            return False
        now = time.time()
        if self.state is not None and "state" not in fields:
            fields["state"] = st.to_dict(self.state)
        if self._sbx is not None:
            fields["sandbox"] = dict(self._sbx.record)
        ok = repo.audit_rollout_checkpoint(
            self.s, self.id, self.token, lease_s=settings.audit_lease_s, now=now, release=release,
            phase=self.phase, spend_usd=self.spend, detail=self.detail,
            scores={**fields.pop("scores", {}), "cells": self.cells}, **fields,
        )
        if not ok:
            self.lost = True
            log.warning("audit_lease_lost", rollout_id=self.id, run_id=self.run_id)
            return False
        repo.audit_run_touch(self.s, self.run_id, now=now, spend_delta=self._unsaved_spend)
        self._unsaved_spend = 0.0
        return True

    def finish(self, verdict: str, *, results: list[dict] | None = None, **detail: Any) -> None:
        """Close the rollout: emit its conversation, write its scores, park its checkpoint in the
        blob store, and release it as DONE."""
        self.detail = {**self.detail, **{k: v for k, v in detail.items() if v not in (None, "")}}
        # Prove the lease BEFORE anything leaves this process. A tick that lost its rollout holds
        # a fork of it: emitting that fork would land a second set of spans (same ids, different
        # timestamps — which `events` keeps both of) beside the real owner's.
        if not self.save():
            return
        state_fields: dict[str, Any] = {}
        if self.state is not None:
            self.detail.update(
                ended=self.state.ended, turns=self.state.auditor_turns,
                branches=len(self.state.branches) - 1,
                **({"error": self.state.error} if self.state.error else {}),
            )
            self._destroy_sandbox()
            trace_ids = self._emit()
            self.detail["trace_ids"] = trace_ids
            # Only a valid, complete, graded rollout gets scores. The thread's pill reads PASS for
            # any thread that has scores and no FAIL, so writing neutral scores for an INVALID
            # rollout would paint it green.
            if verdict in ("ELICITED", "CLEAN") and trace_ids and results:
                self._write_scores(trace_ids[-1], results)
            state_fields = self._park_state()
        self.phase = "DONE"
        if self.save(
            release=True, verdict=verdict, finished_at=datetime.now(UTC),
            scores={"results": results or []}, **state_fields,
        ):
            self.s.execute(
                update(AuditRun).where(AuditRun.id == self.run_id)
                .values(done=AuditRun.done + 1).execution_options(synchronize_session=False)
            )
            self.s.commit()
            log.info("audit_rollout_done", rollout_id=self.id, verdict=verdict)

    def _emit(self) -> list[str]:
        """The surviving conversation → internal spans. Inline (blob, then map), never queued:
        the tick already holds the worker's slot. Idempotent — the payload is a pure function of
        the checkpoint, ids and timestamps included."""
        try:
            payload, trace_ids = emit.rollout_payload(
                self.state, rollout_id=self.id,
                title=f"audit · {self.seed.get('title') or self.cfg.get('audit_name') or 'rollout'}",
                meta={
                    "audit_id": str(self.cfg.get("audit_id") or ""), "run_id": self.run_id,
                    "rollout_id": self.id, "audit": str(self.cfg.get("audit_name") or ""),
                },
            )
            if not payload:
                return []
            from tracely.services.ingestion_service import IngestionService

            # A stable key, so re-emitting overwrites the same blob instead of leaving another.
            key = blobstore.event_blob_key(self.project_id, f"audit-{self.id}", "application/json")
            blobstore.put_blob(key, json.dumps(payload).encode(), "application/json")
            IngestionService().process_blob(self.project_id, key, "application/json")
            return trace_ids
        except Exception as exc:  # a transcript we cannot show must not cost the verdict
            log.exception("audit_emit_failed", rollout_id=self.id)
            self.detail["emit_error"] = str(exc)[:300]
            return []

    def _write_scores(self, last_trace_id: str, results: list[dict]) -> None:
        try:
            self.svc.score_writer.write_eval_scores(
                self.project_id, last_trace_id, last_trace_id, judge.score_rows(results),
                thread_id=emit.conversation_id(self.id),
            )
        except Exception as exc:
            log.exception("audit_scores_failed", rollout_id=self.id)
            self.detail["scores_error"] = str(exc)[:300]

    def _park_state(self) -> dict:
        """Move the finished checkpoint out of Postgres. On any failure it simply stays in the
        row — bigger than we want, never lost."""
        try:
            key = blobstore.audit_blob_key(self.project_id, f"rollouts/{self.id}.json")
            blobstore.put_blob(key, json.dumps(st.to_dict(self.state)).encode(), "application/json")
            return {"state": None, "state_key": key}
        except Exception as exc:
            log.warning("audit_state_park_failed", rollout_id=self.id, error=str(exc))
            return {}

    # ── the slice ─────────────────────────────────────────────────────────────

    def advance(self) -> None:
        try:
            if self.attempts >= _MAX_LOST_LEASES:
                self.finish("ERROR", reason=f"abandoned after {self.attempts} interrupted attempts")
                return
            if self.phase == "PENDING" and not self._begin():
                return
            if self.phase == "DRIVING" and not self._drive():
                return
            if self.phase == "JUDGING":
                self._judge()
        except Exception as exc:  # one crashing rollout must not sink the run
            log.exception("audit_rollout_crashed", rollout_id=self.id)
            self.finish("ERROR", reason=f"{type(exc).__name__}: {str(exc)[:300]}")

    def _begin(self) -> bool:
        """PENDING → DRIVING. False when the rollout ended here instead."""
        if not provider.llm_enabled():
            self.finish("UNGRADED", reason="no LLM key is configured for this workspace")
            return False
        # An explore run's first pass leaves a reserve for the deepen pass to spend.
        share = explore.STAGE0_BUDGET_SHARE if self.explore and self.stage == 0 else 1.0
        if self.budget_usd and self.run_spend >= self.budget_usd * share:
            self.finish("SKIPPED", reason="the run's budget was spent before this rollout started")
            return False
        target = self.cfg["target"]
        models = self.cfg.get("attacker_models") or [provider.default_model_id()]
        attacker_model = models[self.position % len(models)]  # deterministic rotation
        seed_text = str(self.seed.get("instruction") or "")
        env = self.cfg.get("environment") or {}
        sandboxed = env.get("mode") == "sandbox"
        tools = [dict(t) for t in target.get("tools") or []]
        modal = set(provider.model_modalities(target["model"])["input"])
        if sandboxed:
            # The target's own tools keep whatever binding they came with (imported ones are the
            # attacker's to answer); the pack's are added beside them and really execute.
            tools += tooling.pack_tools(
                env.get("packs") or ["dev"], {t["name"] for t in tools}, images="image" in modal,
            )
        files = [a for a in self.seed.get("attachments") or [] if a.get("kind") in modal]
        # A modality the target cannot take is simply not offered: sending it would be a 400,
        # and `start_run` has already refused the audits that depend on it.
        modal_tools = [m for m in self.cfg["attacker"].get("media") or []
                       if ("audio" if m == "voice" else m) in modal]
        self.state = st.new_state(
            environment="sandbox" if sandboxed else "synthetic",
            auditor_system=prompts.auditor_system(
                target_model=target["model"], max_turns=self.cfg["attacker"]["max_turns"],
                today=datetime.now(UTC).strftime("%Y-%m-%d"), seed=seed_text,
                behaviour=str(self.cfg.get("behavior") or ""), control=target["control"],
                tools=tools, weaknesses=self.cfg.get("weaknesses") or [], sandbox=sandboxed,
                network=str(env.get("network") or "none"), lessons=self._lessons(), files=files,
                media=modal_tools,
            ),
            auditor_user=prompts.auditor_user(target["model"], seed_text),
            target_model=target["model"], control=target["control"],
            system_prompt=target.get("system_prompt") or "", tools=tools,
            max_turns=self.cfg["attacker"]["max_turns"], prefill=self.cfg["attacker"]["prefill"],
            rollback=self.cfg["attacker"]["rollback"],
            media=modal_tools,
            assets=files,
        )
        self.phase = "DRIVING"
        self.detail.update(attacker_model=attacker_model, started_at=time.time())
        return self.save()

    def _lessons(self) -> list[str]:
        """Explore runs only: what this run's finished rollouts showed (`explore.lessons`)."""
        if not self.explore:
            return []
        return explore.lessons([
            {"verdict": r.verdict, "seed": r.seed, "scores": r.scores}
            for r in repo.audit_rollouts_list(self.s, self.run_id) if r.phase == "DONE" and r.id != self.id
        ])

    def _attacker_reply(self, messages: list[dict], tools: list[dict]) -> dict:
        """The attacker's next turn. In an explore run with `best_of` > 1 it is the draft a
        picker prefers out of that many; a picker that fails costs nothing but the choice."""
        def draft() -> dict:
            return provider.run_chat(
                messages, tools=tools, model=self.detail.get("attacker_model"),
                # Warm: a deterministic attacker rephrases the same probe every turn.
                temperature=1.0, on_usage=self._spent,
            )

        n = int(self.cfg["attacker"].get("best_of") or 1) if self.explore else 1
        first = draft()
        if n <= 1:
            return first
        drafts = [first]
        for _ in range(n - 1):
            try:
                drafts.append(draft())
            except Exception as exc:
                log.warning("audit_draft_failed", rollout_id=self.id, error=str(exc)[:200])
        drafts = [d for d in drafts if d.get("tool_calls")] or drafts[:1]
        if len(drafts) == 1:
            return drafts[0]
        try:
            pick = provider.run_structured_agent(
                explore.pick_prompt(
                    str(self.seed.get("instruction") or ""),
                    transcript.render(self.state)[-6000:], drafts,
                ),
                response_format=explore.Pick, system_prompt=explore.PICK_SYSTEM,
                model=self.cfg["attacker"].get("realism_model") or None, on_usage=self._spent,
            )
            index = explore.chosen(pick, len(drafts))
        except Exception as exc:
            log.warning("audit_pick_failed", rollout_id=self.id, error=str(exc)[:200])
            index = 0
        self.detail["picks"] = int(self.detail.get("picks") or 0) + 1
        return drafts[index]

    def _drive(self) -> bool:
        """Attacker ⇄ target until the conversation ends or the slice runs out. True when the
        rollout moved on to JUDGING (and time may remain to start it)."""
        state = self.state
        target = self.cfg["target"]
        while True:
            step = st.next_step(state)
            if step == "done":
                self.phase = "JUDGING"
                return self.save()
            if not self._time_left():
                self.save(release=True)
                return False
            if step == "env" and state.env_queue[0]["name"] in ("generate_image", "send_voice_message"):
                self._media_step(state.env_queue[0])
                if not self.save():
                    return False
                continue
            if step in ("restore", "env", "exec"):
                if not self._sandbox_step(step):
                    state.ended = "error"
                    state.awaiting, state.env_queue, state.restore = None, [], None
                if not self.save():
                    return False
                continue
            if step == "auditor":
                # The brakes apply between attacker turns — never while the target owes a reply,
                # which would leave the attacker's history with an unanswered call.
                if self.budget_usd and self.run_spend >= self.budget_usd * _DRIVE_BUDGET_SHARE:
                    state.ended, state.error = "budget", "the run's budget ran out mid-rollout"
                    continue
                if time.time() - float(self.detail.get("started_at") or time.time()) > settings.audit_rollout_timeout_s:
                    state.ended, state.error = "timeout", "the rollout hit its wall-clock ceiling"
                    continue
                messages, tools = st.auditor_request(state)
                try:
                    reply = self._attacker_reply(messages, tools)
                except Exception as exc:
                    errors = int(self.detail.get("attacker_errors") or 0) + 1
                    self.detail["attacker_errors"] = errors
                    log.warning("audit_attacker_failed", rollout_id=self.id, error=str(exc)[:300])
                    if errors >= _MAX_ATTACKER_ERRORS:
                        st.apply_auditor_error(state, str(exc))
                else:
                    self.detail["attacker_errors"] = 0
                    st.apply_auditor_reply(state, reply, time.time_ns(), self._realism(reply))
            else:
                if state.environment == "sandbox" and not state.base_snapshot and not self._base_snapshot():
                    state.ended = "error"
                    state.awaiting = None
                    if not self.save():
                        return False
                    continue
                messages, tools = st.target_request(state)
                started = time.time_ns()
                try:
                    reply = provider.run_chat(
                        media.wire(messages, self._load_asset), tools=tools or None, model=target["model"],
                        temperature=float(target.get("temperature", 1.0)),
                        reasoning_effort=target.get("reasoning_effort") or None,
                        on_usage=self._spent,
                    )
                except Exception as exc:
                    log.warning("audit_target_failed", rollout_id=self.id, error=str(exc)[:300])
                    st.apply_target_error(state, str(exc))
                else:
                    st.apply_target_reply(state, reply, started, time.time_ns())
            if not self.save():
                return False

    # ── the real environment ──────────────────────────────────────────────────

    def _sandbox(self) -> SandboxSession:
        """This rollout's sandbox, created on first use. One that vanished between ticks (a host
        restart, its lifetime cap) is rebuilt from the snapshot of where the target stands."""
        if self._sbx is None:
            self._sbx = SandboxSession(
                self.svc.sandbox_backend(), project_id=self.project_id, rollout_id=self.id,
                record=self._sandbox_record,
                network=str((self.cfg.get("environment") or {}).get("network") or "none"),
            )
        if not self._sbx.record.get("ref"):
            key, cwd = self._snapshot_at(self.state.head)
            self._sbx.ensure(key)
            if key:
                self.state.cwd = cwd
        return self._sbx

    def _snapshot_at(self, anchor: str) -> tuple[str, str]:
        """`(blob key, cwd)` of the environment as it stood after `anchor`; `""` = pristine.
        `"none"` is the marker for "the base snapshot was taken and was too large to keep"."""
        key, cwd = st.resolve_snapshot(self.state, anchor)
        return ("" if key == "none" else key), cwd

    def _destroy_sandbox(self) -> None:
        if self.state is None or self.state.environment != "sandbox":
            return
        try:
            if self._sbx is None and self._sandbox_record:
                self._sbx = SandboxSession(
                    self.svc.sandbox_backend(), project_id=self.project_id, rollout_id=self.id,
                    record=self._sandbox_record,
                )
            if self._sbx is not None:
                self._sbx.destroy()
        except Exception as exc:  # its lifetime cap is the backstop
            log.warning("audit_sandbox_destroy_failed", rollout_id=self.id, error=str(exc)[:200])

    def _base_snapshot(self) -> bool:
        """Store the environment as the attacker set it up, once, before the target's first
        turn — what a restart, or a rollback to before any tool ran, goes back to."""
        try:
            self.state.base_snapshot = self._sandbox().snapshot() or "none"
            return True
        except Exception as exc:
            self.state.error = f"sandbox failed: {str(exc)[:300]}"
            log.warning("audit_sandbox_failed", rollout_id=self.id, step="base", error=str(exc)[:300])
            return False

    def _sandbox_step(self, step: str, retried: bool = False) -> bool:
        """One sandbox operation the state asked for. False ends the rollout with the reason."""
        state = self.state
        try:
            sbx = self._sandbox()
            if step == "restore":
                key, cwd = self._snapshot_at(state.restore["anchor"])
                sbx.restore(key)
                st.restored(state, cwd)
            elif step == "env" and state.env_queue[0]["name"] == "env_fetch_file":
                item = state.env_queue[0]
                data = sbx.fetch(str(item["args"].get("path") or ""))
                self._media_done(item, data, "that path is not a file, or it is too large")
            elif step == "env":
                item = state.env_queue[0]
                st.apply_env_result(state, item["id"], sbx.run_env(item["name"], item["args"]))
            else:
                for call in st.real_pending(state):
                    tool = next((t for t in state.tools if t["name"] == call["name"]), None)
                    out = sbx.run_tool(call["name"], call["args"], state.cwd, tool)
                    if out is None:  # the attacker's own implementation failed on this call
                        st.demote_call(state, call["id"], sbx.last_handler_error)
                        continue
                    state.cwd = out.cwd
                    st.apply_tool_result(
                        state, call["id"], out.content, status=out.status, now_ns=time.time_ns(),
                        attachments=[self.svc.put_asset(self.project_id, *out.file)] if out.file else None,
                    )
                # After the batch: this is the environment a rollback to here must restore.
                if state.rollback_enabled:
                    key = sbx.snapshot()
                    if key:
                        st.mark_snapshot(state, key)
            return True
        except SandboxGone:
            if retried:
                state.error = "the sandbox kept disappearing"
                return False
            # Gone mid-rollout: forget the dead one; `_sandbox()` rebuilds it from the snapshot
            # of where the target stands, and the step runs again.
            if self._sbx is not None:
                self._sbx.record.pop("ref", None)
            return self._sandbox_step(step, retried=True)
        except Exception as exc:
            state.error = f"sandbox failed: {str(exc)[:300]}"
            log.warning("audit_sandbox_failed", rollout_id=self.id, step=step, error=str(exc)[:300])
            return False

    # ── media ─────────────────────────────────────────────────────────────────

    def _load_asset(self, asset: str) -> bytes:
        return self.svc.get_asset(self.project_id, asset)

    def _media_done(self, item: dict, data: bytes | None, why_not: str) -> None:
        """Store what a queued media call produced and tell the attacker (`st.apply_media_result`)."""
        args = item["args"]
        ref, note = None, why_not
        if data is not None:
            spoken = str(args.get("text") or "") if item["name"] == "send_voice_message" else ""
            name = str(args.get("name") or "") or f"voice-{self.state.seq + 1}.mp3"
            try:
                ref, note = self.svc.put_asset(self.project_id, data, name, spoken), f"Saved as {name!r}."
            except ValueError as exc:
                note = str(exc)
        st.apply_media_result(self.state, item["id"], ref, note, time.time_ns())

    def _media_step(self, item: dict) -> None:
        """`generate_image` / `send_voice_message`: make the file on the workspace's key. A
        failure is the attacker's to read and route around — never the rollout's end."""
        args = item["args"]
        try:
            if item["name"] == "generate_image":
                data = provider.generate_image(str(args.get("prompt") or ""), on_usage=self._spent)
            else:
                data = provider.synthesize_speech(str(args.get("text") or ""), on_usage=self._spent)
        except Exception as exc:
            log.warning("audit_media_failed", rollout_id=self.id, tool=item["name"], error=str(exc)[:300])
            self._media_done(item, None, f"could not be made ({str(exc)[:200]}). Carry on without it.")
        else:
            self._media_done(item, data, "")

    # ── the realism filter ────────────────────────────────────────────────────

    def _realism(self, reply: dict) -> dict[str, str]:
        """`{tool_call_id: feedback}` for the attacker's actions that would not pass as real.
        Empty when the filter is off, and on any failure — it fails open."""
        if not self.cfg["attacker"].get("realism_filter"):
            return {}
        state = self.state
        rejected: dict[str, str] = {}
        tail = [{"role": n["role"], "content": n.get("content") or ""} for n in st.path(state)[-6:]]
        for call in reply.get("tool_calls") or []:
            if call["name"] not in realism.CHECKED:
                continue
            if int(state.realism_rejects.get(call["name"]) or 0) >= realism.MAX_REJECTIONS:
                continue  # it has been sent back enough: let it through, the judges will see it
            try:
                verdict = provider.run_structured_agent(
                    realism.realism_prompt(str(self.seed.get("instruction") or ""), tail, state.tools, call),
                    response_format=realism.RealismVerdict, system_prompt=realism.REALISM_SYSTEM,
                    model=self.cfg["attacker"].get("realism_model") or None, on_usage=self._spent,
                )
            except Exception as exc:
                log.warning("audit_realism_failed", rollout_id=self.id, error=str(exc)[:200])
                continue
            if verdict.score < realism.THRESHOLD:
                rejected[call["id"]] = verdict.feedback.strip() or "It reads as staged; make it more ordinary."
                self.detail["realism_rejections"] = int(self.detail.get("realism_rejections") or 0) + 1
        return rejected

    def _judge(self) -> None:
        """Ask the cells still missing, then reduce the matrix to a verdict."""
        state = self.state
        if not st.has_target_reply(state):
            self.finish(
                judge.rollout_verdict([], ended=state.ended, has_reply=False),
                reason=state.error or "the target never replied, so there is nothing to judge",
            )
            return
        dims = resolved_dimensions(self.cfg)
        models = self.cfg.get("judge_models") or [provider.default_model_id()]
        text = transcript.render(state)
        known_ids = set(state.order)
        while True:
            todo = judge.plan_cells(dims, self.cells, models)
            if not todo:
                break
            if not self._time_left():
                self.save(release=True)
                return
            cell = todo[0]
            dim = next(d for d in dims if d["name"] == cell["dimension"])
            self.cells.append(self._ask(cell, dim, text, known_ids))
            if not self.save():
                return
        results = [judge.aggregate_panel(d, self.cells, models) for d in dims]
        self.finish(
            judge.rollout_verdict(results, ended=state.ended, has_reply=True), results=results
        )

    def _ask(self, cell: dict, dim: dict, text: str, known_ids: set[str]) -> dict:
        """One judge call. A failure is recorded ON the cell (and never retried here): the
        quorum rule then decides whether the dimension still stands."""
        if self.budget_usd and self.run_spend >= self.budget_usd:
            return {**cell, "score": None, "error": "the run's budget ran out before this judge was asked"}
        prompt: Any = judge.judge_prompt(text, dim, str(self.cfg.get("behavior") or ""))
        info = provider.model_info(cell["model"])
        tokens = budget.estimate_tokens(judge.JUDGE_SYSTEM, prompt)
        if not budget.fits(info, tokens, settings.llm_max_output_tokens):
            return {**cell, "score": None, "error": budget.overflow_note(info["id"], tokens, info)}
        try:
            # A judge that can see is shown the images the transcript only names; one that cannot
            # reads the markers. Audio is always read as its words.
            shown = media.images(list(self.state.nodes.values()))
            if shown and "image" in provider.model_modalities(cell["model"])["input"]:
                prompt = media.wire([{"content": media.blocks(
                    prompt + "\n\nThe images named in the transcript follow, in order of first appearance.", shown,
                )}], self._load_asset)[0]["content"]
            verdict = provider.run_structured_agent(
                prompt, response_format=judge.DimensionVerdict, system_prompt=judge.JUDGE_SYSTEM,
                model=cell["model"],
                # The first look is deterministic; repeat samples must be allowed to differ, or
                # asking again only buys the same answer three times.
                temperature=0.0 if cell["sample"] == 0 else 0.7,
                on_usage=self._spent,
            )
        except Exception as exc:
            log.warning("audit_judge_failed", rollout_id=self.id, dimension=dim["name"],
                        model=cell["model"], error=str(exc)[:300])
            return {**cell, "score": None, "error": str(exc)[:300]}
        return {
            **cell, "score": int(verdict.score), "justification": verdict.justification[:1500],
            "highlights": judge.clean_highlights(
                [h.model_dump() for h in verdict.highlights], known_ids
            ),
        }
