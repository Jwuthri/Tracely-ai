"""Making "deleted" mean deleted: the background purge behind every workspace-level delete, and
the nightly sweep for data whose workspace is gone.

Both delete customer data with nobody watching, so the tests that matter most are the refusals:
the sweep must never act on a registry read that looks wrong, and a wipe's purge must never take
data that arrived after the button was pressed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tracely.infrastructure.blob import s3
from tracely.services import purge_service

# Bound at collection, before conftest's autouse `purges` stubs it for every test.
REAL_SCHEDULE_PURGE = purge_service.schedule_purge

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


# ── object storage ────────────────────────────────────────────────────────────


class FakeBucket:
    """list_objects_v2 (flat or delimited) + delete_objects over an in-memory key -> mtime map."""

    def __init__(self, objects: dict[str, datetime], errors: list | None = None):
        self.objects = dict(objects)
        self.errors = errors

    def get_paginator(self, _name):
        bucket = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = sorted(k for k in bucket.objects if k.startswith(Prefix))
                if Delimiter:
                    tops = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                                   for k in keys if Delimiter in k[len(Prefix):]})
                    yield {"CommonPrefixes": [{"Prefix": t} for t in tops]}
                else:
                    yield {"Contents": [{"Key": k, "LastModified": bucket.objects[k]} for k in keys]}

        return P()

    def delete_objects(self, Bucket, Delete):
        if self.errors:
            return {"Errors": self.errors}
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"])
        return {"Deleted": Delete["Objects"]}


@pytest.fixture
def bucket(monkeypatch):
    def install(objects, errors=None):
        b = FakeBucket(objects, errors)
        monkeypatch.setattr(s3, "_s3", lambda: b)
        return b

    return install


def test_a_wipe_only_takes_what_existed_when_the_button_was_pressed(bucket):
    """The purge is queued, so a busy workspace keeps ingesting while it waits. Those new bodies
    are new data — taking them would silently lose the first traces after a wipe."""
    old, new = NOW - timedelta(hours=1), NOW + timedelta(seconds=5)
    b = bucket({
        "events/p1/otlp/a.json": old,
        "events/p1/otlp/b.json": new,
        "events/fixtures/p1/f.json": old,
        "events/p1/assistant/chat.png": old,  # not a trace: a wipe keeps attachments
        "events/p2/otlp/z.json": old,  # someone else's
    })
    assert s3.delete_project_blobs("p1", traces_only=True, before=NOW) == 2
    assert set(b.objects) == {
        "events/p1/otlp/b.json", "events/p1/assistant/chat.png", "events/p2/otlp/z.json"
    }


def test_a_workspace_delete_takes_everything_of_that_workspace_only(bucket):
    b = bucket({
        "events/p1/otlp/a.json": NOW,
        "events/p1/assistant/chat.png": NOW,
        "events/cases/p1/c.json": NOW,
        "events/p2/otlp/z.json": NOW,
    })
    assert s3.delete_project_blobs("p1") == 3
    assert set(b.objects) == {"events/p2/otlp/z.json"}


def test_a_failed_delete_is_loud_so_the_task_retries(bucket):
    """`delete_objects` reports per-key failures in its response without raising. The old purge
    returned 0 and moved on — that is how 2 GB stayed in the bucket with nothing logged."""
    bucket({"events/p1/otlp/a.json": NOW}, errors=[{"Key": "events/p1/otlp/a.json", "Code": "X"}])
    with pytest.raises(RuntimeError, match="delete_objects"):
        s3.delete_project_blobs("p1")


def test_storage_lists_project_ids_at_all_three_depths(bucket):
    bucket({
        "events/p1/otlp/a.json": NOW,
        "events/fixtures/p2/f.json": NOW,
        "events/cases/p3/c.json": NOW,
    })
    # `fixtures` and `cases` are directories of projects, not projects
    assert s3.project_ids_in_storage() == {"p1", "p2", "p3"}


# ── the queued purge ──────────────────────────────────────────────────────────


def test_purge_project_takes_the_blobs_then_the_masked_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(s3, "delete_project_blobs",
                        lambda pid, traces_only, before: calls.append(("blobs", pid, traces_only, before)) or 7)
    monkeypatch.setattr(purge_service.maintenance, "compact_tables", lambda: calls.append(("compact",)))

    assert purge_service.purge_project("p1", NOW.isoformat()) == {"project_id": "p1", "blobs": 7}
    assert calls == [("blobs", "p1", True, NOW), ("compact",)]

    calls.clear()
    purge_service.purge_project("p1", None)  # workspace delete: everything
    assert calls[0] == ("blobs", "p1", False, None)


def test_schedule_purge_hands_the_cutoff_to_the_worker_by_name(monkeypatch):
    """By task name and as an ISO string: the API never imports the worker module, and a
    datetime does not survive the JSON serializer."""
    sent = []
    monkeypatch.setattr(purge_service.celery_app, "send_task", lambda name, args: sent.append((name, args)))
    REAL_SCHEDULE_PURGE("p1", NOW)
    REAL_SCHEDULE_PURGE("p2")
    assert sent == [("tracely.purge_project", ["p1", NOW.isoformat()]),
                    ("tracely.purge_project", ["p2", None])]


def test_both_sweeps_are_scheduled_nightly():
    from tracely.infrastructure.queue.celery_app import celery_app

    beat = celery_app.conf.beat_schedule
    assert beat["tracely.purge_orphans-nightly"]["task"] == "tracely.purge_orphans"
    assert beat["tracely.compact_tables-nightly"]["task"] == "tracely.compact_tables"


# ── the nightly orphan sweep ──────────────────────────────────────────────────


@pytest.fixture
def world(monkeypatch):
    """Storage, ClickHouse and the registry as plain data, plus a log of what the sweep did —
    in order, because reading the registry LAST is part of the contract."""
    log: list = []

    def install(*, storage, last_write, registry):
        def read_storage():
            log.append("read storage")
            return set(storage)

        def read_ch():
            log.append("read clickhouse")
            return dict(last_write)

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def plans(_s):
            log.append("read registry")
            return [(pid, "free") for pid in registry]

        monkeypatch.setattr(s3, "project_ids_in_storage", read_storage)
        monkeypatch.setattr(purge_service.deletes, "last_write_by_project", read_ch)
        monkeypatch.setattr(purge_service, "SyncSessionLocal", Session)
        monkeypatch.setattr(purge_service, "project_plans", plans)
        monkeypatch.setattr(purge_service.deletes, "delete_project_rows", lambda p: log.append(("rows", p)))
        monkeypatch.setattr(s3, "delete_project_blobs", lambda p: log.append(("blobs", p)) or 1)
        monkeypatch.setattr(purge_service.checkpointer, "delete_project_chats", lambda p: 0)
        monkeypatch.setattr(purge_service.maintenance, "compact_tables", lambda: log.append("compact"))
        return log

    return install


def test_sweep_purges_only_workspaces_missing_from_the_registry(world):
    old = NOW - timedelta(days=30)
    log = world(
        storage={"live", "gone_both", "gone_bucket_only"},
        last_write={"live": NOW, "gone_both": old},
        registry={"live"},
    )
    out = purge_service.purge_orphans(now=NOW)
    assert out["purged"] == ["gone_both", "gone_bucket_only"]
    assert ("rows", "gone_both") in log and ("blobs", "gone_both") in log
    assert ("blobs", "gone_bucket_only") in log and ("rows", "gone_bucket_only") not in log
    assert not any(isinstance(e, tuple) and e[1] == "live" for e in log)
    assert log[-1] == "compact"


def test_sweep_reads_the_registry_last(world):
    """A workspace must exist in Postgres before it can write. Read the data first and anything
    still missing from the registry afterwards really was deleted; read the registry first and a
    workspace created in between loses its first traces."""
    log = world(storage={"a"}, last_write={}, registry={"a"})
    purge_service.purge_orphans(now=NOW)
    reads = [e for e in log if isinstance(e, str) and e.startswith("read")]
    assert reads[-1] == "read registry"


def test_sweep_refuses_on_an_empty_registry(world):
    """Pointed at the wrong / an empty database, every workspace looks deleted."""
    log = world(storage={"a", "b"}, last_write={"a": NOW - timedelta(days=30)}, registry=set())
    assert purge_service.purge_orphans(now=NOW) == {"refused": "empty registry"}
    assert not any(isinstance(e, tuple) for e in log)


def test_sweep_refuses_when_a_would_be_orphan_wrote_recently(world):
    """A deleted workspace cannot write. If one did, the registry read is wrong — and the sweep
    deletes NOTHING, not just that one: the other "orphans" came from the same bad read."""
    log = world(
        storage={"live", "ancient"},
        last_write={"live": NOW - timedelta(hours=2), "ancient": NOW - timedelta(days=60)},
        registry={"someone_else"},
    )
    out = purge_service.purge_orphans(now=NOW)
    assert out == {"refused": "recent writes", "projects": ["live"]}
    assert not any(isinstance(e, tuple) for e in log)


def test_sweep_handles_naive_clickhouse_timestamps(world):
    """ClickHouse hands back naive UTC; comparing one with an aware `now` raises."""
    world(storage=set(), last_write={"gone": (NOW - timedelta(days=30)).replace(tzinfo=None)}, registry={"x"})
    assert purge_service.purge_orphans(now=NOW)["purged"] == ["gone"]


# ── 90-day expiry of raw OTLP bodies ──────────────────────────────────────────


def test_expiry_takes_only_old_otlp_bodies(bucket):
    """Past the 90-day horizon the tables already enforce, a raw body is bytes nothing can reach.
    But case fixtures and artifacts must outlive their source trace — a promoted regression case
    keeps running after the trace it came from has expired — and chat attachments aren't traces."""
    ancient = NOW - timedelta(days=91)
    recent = NOW - timedelta(days=89)
    b = bucket({
        "events/p1/otlp/old.json": ancient,
        "events/p1/otlp/new.json": recent,
        "events/p2/otlp/old.pb": ancient,
        "events/fixtures/p1/bundle.json": ancient,
        "events/cases/p1/artifact.json": ancient,
        "events/p1/assistant/chat.png": ancient,
    })
    out = purge_service.expire_otlp_blobs(now=NOW)
    assert out["expired"] == 2
    assert set(b.objects) == {
        "events/p1/otlp/new.json",
        "events/fixtures/p1/bundle.json",
        "events/cases/p1/artifact.json",
        "events/p1/assistant/chat.png",
    }


def test_expiry_is_scheduled_nightly():
    from tracely.infrastructure.queue.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["tracely.expire_otlp_blobs-nightly"]
    assert entry["task"] == "tracely.expire_otlp_blobs"
