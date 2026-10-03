"""Making "deleted" mean deleted — the background half of every workspace-level delete.

The routes (Data → wipe, workspace delete, organization delete) do the fast part inline: mask the
ClickHouse rows and drop the registry rows, so the UI is empty on the next fetch. What they must
NOT do inline is the rest, because it is slow and was silently not happening:

- **Object storage.** The raw OTLP bodies are the customer's payloads verbatim. MinIO took 197 s
  just to *list* one workspace's 108k bodies; inside the request that outlived the browser, a
  deploy killed it, and 2 GB stayed in the bucket while the UI said "deleted".
- **ClickHouse disk.** A lightweight DELETE only masks; every query keeps reading masked rows
  until their part is rewritten, which for a big part is never.

So `schedule_purge` hands both to `tracely.purge_project` — acks-late, retried with backoff, so a
deploy or a MinIO hiccup delays it instead of losing it. And `purge_orphans` (nightly) is the
backstop for anything that still slips through: data in ClickHouse or the bucket whose workspace
no longer exists in Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from tracely.infrastructure.blob import s3
from tracely.infrastructure.clickhouse import deletes, maintenance
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.repositories import project_plans
from tracely.infrastructure.llm import checkpointer
from tracely.infrastructure.queue.celery_app import celery_app

log = structlog.get_logger()

# A deleted workspace cannot write (its keys are gone). Recent writes from a "deleted" project
# therefore mean the registry read is wrong — the sweep refuses rather than act on it.
ORPHAN_QUIET_DAYS = 7


def schedule_purge(project_id: str, before: datetime | None = None) -> None:
    """Queue the physical purge of one workspace's deleted data.

    `before`: only objects written before it go — a wipe keeps whatever arrives after the
    button was pressed. None (workspace / organization delete) takes everything, attachments
    included. By task name, so the API never imports the worker module."""
    celery_app.send_task(
        "tracely.purge_project", args=[project_id, before.isoformat() if before else None]
    )


def purge_project(project_id: str, before: str | None) -> dict:
    """Delete the workspace's blobs, then rewrite ClickHouse so the masked rows leave the disk.
    Idempotent — a retry re-lists what is left and finishes the job."""
    cutoff = datetime.fromisoformat(before) if before else None
    blobs = s3.delete_project_blobs(project_id, traces_only=cutoff is not None, before=cutoff)
    maintenance.compact_tables()
    log.info("project_purged", project_id=project_id, blobs=blobs, before=before)
    return {"project_id": project_id, "blobs": blobs}


def purge_orphans(now: datetime | None = None) -> dict:
    """Remove ClickHouse rows and objects whose workspace no longer exists (beat, nightly).

    Order matters: storage and ClickHouse are read FIRST, the registry LAST. A workspace has to
    exist in Postgres before it can write anything, so anything still missing from the registry
    after we have seen its data was deleted — never "just created". Read the other way round, a
    workspace created between the two reads would look orphaned and lose its first traces.

    Refuses, and deletes nothing, when the registry comes back empty or a would-be orphan wrote
    within ORPHAN_QUIET_DAYS: both mean we are looking at the wrong database, and this is the
    one job that deletes across every workspace at once."""
    now = now or datetime.now(timezone.utc)
    in_storage = s3.project_ids_in_storage()
    last_write = deletes.last_write_by_project()
    with SyncSessionLocal() as s:
        live = {pid for pid, _ in project_plans(s)}

    if not live:
        log.error("orphan_sweep_refused", reason="registry returned no workspaces")
        return {"refused": "empty registry"}

    orphans = sorted((in_storage | set(last_write)) - live)
    quiet_since = now - timedelta(days=ORPHAN_QUIET_DAYS)
    noisy = [p for p in orphans if p in last_write and _aware(last_write[p]) > quiet_since]
    if noisy:
        log.error("orphan_sweep_refused", reason="recent writes", projects=noisy)
        return {"refused": "recent writes", "projects": noisy}

    blobs = 0
    for pid in orphans:
        if pid in last_write:
            deletes.delete_project_rows(pid)
        blobs += s3.delete_project_blobs(pid)
        checkpointer.delete_project_chats(pid)
    if orphans:
        maintenance.compact_tables()
        log.info("orphans_purged", projects=orphans, blobs=blobs)
    return {"purged": orphans, "blobs": blobs}


def _aware(ts: datetime) -> datetime:
    """ClickHouse hands back naive UTC datetimes."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
