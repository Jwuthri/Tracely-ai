"""Ingestion service: the OTLP → blob → enqueue producer side AND the worker-side
"read blob, map, persist" pipeline.

The class wraps the worker-side stages (`process_blob`) so each step is named and testable;
`ingest_otlp` stays as a module-level function for the FastAPI router (it has nothing to do
with a long-lived service object — it's one fire-and-forget action).
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict

import structlog

from tracely.config import settings
from tracely.domain.cost import cost_details
from tracely.infrastructure.blob import s3 as blobstore
from tracely.infrastructure.clickhouse.client import get_client, insert_rows
from tracely.infrastructure.clickhouse.events_schema import EVENT_COLUMNS, to_rows
from tracely.infrastructure.db import repositories
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.llm.provider import resolve_rate
from tracely.infrastructure.registry import agents as registry
from tracely.otel import parse_otlp_traces, parse_otlp_traces_json

log = structlog.get_logger()


def ingest_otlp(project_id: str, content_type: str, raw: bytes) -> str:
    """Blob-first ingestion entry: upload the raw OTLP body to S3, then enqueue the worker.

    Mirrors Langfuse `processEventBatch`: nothing is queued unless the blob is durable.
    """
    # Local import: avoids a circular path through `workers.tasks -> celery_app -> include`.
    from tracely.workers.tasks import ingest_otlp_blob

    from tracely.services.selfcheck_service import LAST_ACCEPT_KEY, stamp

    batch_id = uuid.uuid4().hex
    key = blobstore.event_blob_key(project_id, batch_id, content_type)
    blobstore.put_blob(key, raw, content_type or "application/x-protobuf")
    ingest_otlp_blob.delay(project_id, key, content_type or "")
    # "Spans were accepted" is half of the deployment's most important signal: accepted spans +
    # a stale ClickHouse = the worker is dead while every client still sees 202s.
    stamp(LAST_ACCEPT_KEY)
    return batch_id


class IngestionService:
    """Worker-side pipeline: blob → events → registry resolve → ClickHouse insert → schedule
    online eval. Called from the `ingest_otlp_blob` Celery task."""

    def process_blob(self, project_id: str, key: str, content_type: str) -> dict:
        raw = blobstore.get_blob(key)
        is_json = "json" in (content_type or "")
        events = (parse_otlp_traces_json if is_json else parse_otlp_traces)(raw, project_id)
        if not events:
            return {"events": 0, "trace_ids": []}

        # agent-less traces -> fallback agent (inherits within a trace)
        self._attribute_default_agent(events)
        self._attach_costs(events)
        self._resolve_registry_ids(project_id, events)
        # user-declared agent catalog (SDK `tracely.agents`) -> Postgres, stripped from ClickHouse
        self._extract_agent_definitions(project_id, events)

        client = get_client()
        insert_rows(client, "events", EVENT_COLUMNS, to_rows(events))

        trace_ids = sorted({ev.get("trace_id") for ev in events if ev.get("trace_id")})
        # Recordings of Tracely's own work (an evaluation, a scenario run) are reported separately
        # so the caller never schedules evaluation for them: grading an eval run would record
        # another eval run, and so on without end. See `domain/introspection.py`.
        internal = sorted({
            ev.get("trace_id") for ev in events
            if ev.get("trace_id") and ev.get("internal_kind")
        })
        log.info("ingested", project_id=project_id, key=key, events=len(events))
        real = [ev for ev in events if ev.get("trace_id") and not ev.get("internal_kind")]
        if real:
            self._milestone_first_trace(project_id, real)
        return {
            "events": len(events),
            "trace_ids": list(trace_ids),
            "internal_trace_ids": list(internal),
        }

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _milestone_first_trace(project_id: str, events: list[dict]) -> None:
        """`first_trace_received` — from a confirmed ClickHouse insert, not from a request."""
        try:
            from tracely.infrastructure.db.engine import SyncSessionLocal
            from tracely.services import milestones

            with SyncSessionLocal() as s:
                milestones.record(
                    s, project_id, "first_trace_received",
                    sample=milestones.is_sample(events), integration=milestones.integration_of(events),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("milestone_first_trace_failed", error=str(exc))

    @staticmethod
    def _attach_costs(events: list[dict]) -> None:
        """Derive each span's `cost_details` from its model + token counts.

        Providers don't put a price on the wire, so this is the only way the money column exists.
        Doing it at ingest (rather than in the reader) is what lets ClickHouse aggregate spend per
        day, per agent and per gate — those all read `arraySum(mapValues(cost_details))`, which
        was summing an always-empty map to a hard zero.

        Rates come from OpenRouter's published catalog (refreshed hourly, warmed at startup) —
        we don't maintain a price list. Unknown models get no cost rather than 0.0, so "not
        priced" stays distinguishable from "free".

        Lives here rather than in `otel/span_mapper` on purpose: the mapper is a serialization
        rule over the wire format, and pricing is a policy that changes on its own schedule.
        """
        for ev in events:
            if ev.get("cost_details") or not ev.get("usage_details"):
                continue
            model = str(ev.get("model_id") or "")
            if model:
                ev["cost_details"] = cost_details(resolve_rate(model), ev.get("usage_details"))

    @staticmethod
    def _attribute_default_agent(events: list[dict]) -> None:
        """Decide which registry agent each trace belongs to — the dimension gates, clusters,
        regression cases and scenarios are scoped by.

        A trace that declares a TENANT (`tracely.tenant.id` — one codebase serving many
        customers / workspaces / bots) belongs to that tenant, and so does EVERY span in it: the
        tenant is what gets registered and what every agent-scoped read sees. The spans' own
        `agent=` labels ("supervisor", "billing") stay in metadata untouched, so the per-span Agent
        column still shows who did each step; they just never register agents of their own.

        Without a tenant, the older rule: an agent-less span inherits the trace's agent (its
        app-root's, else any sibling's) and only a trace with no agent anywhere gets the configured
        default. Either way an inherited slug is mirrored into metadata under
        `tracely.agent.id.inherited`, NOT `tracely.agent.id`: the Agent column must be able to tell
        a slug the span declared from a trace-wide back-fill."""
        default_slug = settings.default_agent_slug
        if not default_slug:
            return
        by_trace: dict[str, list[dict]] = defaultdict(list)
        for ev in events:
            by_trace[ev.get("trace_id")].append(ev)
        for evs in by_trace.values():
            # A recording of Tracely's own work was produced by no agent, so there is nothing to
            # fall back to: attributing it to `default` both lies in the Agent column and files
            # the product's own runs under a customer agent.
            if any(e.get("internal_kind") for e in evs):
                continue
            root = next((e for e in evs if e.get("is_app_root")), None)
            tenant = (root.get("tenant_slug") if root else "") or next(
                (e.get("tenant_slug") for e in evs if e.get("tenant_slug")), ""
            )
            if tenant:
                for e in evs:
                    if not e.get("agent_slug"):
                        e.setdefault("metadata", {})["tracely.agent.id.inherited"] = tenant
                    e["agent_slug"] = tenant
                continue
            trace_agent = (
                (root.get("agent_slug") if root else "")
                or next((e.get("agent_slug") for e in evs if e.get("agent_slug")), "")
                or default_slug
            )
            for e in evs:
                if not e.get("agent_slug"):
                    e["agent_slug"] = trace_agent
                    e.setdefault("metadata", {})["tracely.agent.id.inherited"] = trace_agent

    @staticmethod
    def _extract_agent_definitions(project_id: str, events: list[dict]) -> None:
        """Pull the user-declared agent catalog (`tracely.agents`, JSON) off the spans and upsert it
        per conversation into Postgres `conversation_agents`. The attribute is STRIPPED from every
        span's metadata so the (potentially large) catalog isn't duplicated into ClickHouse. Latest
        write wins per thread; malformed JSON is ignored. Best-effort — never blocks ingest."""
        by_thread: dict[str, list] = {}
        for ev in events:
            meta = ev.get("metadata")
            if not meta:
                continue
            raw = meta.pop("tracely.agents", None)  # strip even if we can't parse it
            if not raw:
                continue
            try:
                agents = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                continue
            if isinstance(agents, list) and agents:
                thread = ev.get("conversation_id") or ev.get("trace_id") or ""
                if thread:
                    by_thread[thread] = agents  # last span wins (all carry the same catalog)
        if not by_thread:
            return
        try:
            with SyncSessionLocal() as session:
                for thread, agents in by_thread.items():
                    repositories.conversation_agents_upsert(
                        session, project_id, thread_id=thread, agents=agents
                    )
        except Exception as exc:  # never fail ingest on the optional catalog
            log.warning("conversation_agents_upsert_failed", project_id=project_id, error=str(exc))

    @staticmethod
    def _resolve_registry_ids(project_id: str, events: list[dict]) -> None:
        """Resolve `agent_slug` → registry UUID (and `agent_version_ref` → UUID), then strip
        helper keys so they don't get written to ClickHouse."""
        with SyncSessionLocal() as session:
            slug_to_id: dict[str, str] = {}
            for ev in events:
                slug = ev.pop("agent_slug", "")
                ver = ev.pop("agent_version_ref", "")
                ev.pop("tenant_slug", "")  # folded into agent_slug above; never a column
                if slug:
                    if slug not in slug_to_id:
                        slug_to_id[slug] = registry.upsert_agent(session, project_id, slug)
                    ev["agent_id"] = slug_to_id[slug]
                    if ver:
                        ev["agent_version_id"] = registry.upsert_agent_version(
                            session, slug_to_id[slug], ver
                        )
