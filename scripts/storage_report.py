"""What is actually stored, where, and whose it is — ClickHouse + object storage + the registry.

    railway ssh -s api "python scripts/storage_report.py"   # everything (PG/MinIO are internal)
    uv run python scripts/storage_report.py                 # locally, or against a public host

Reads the app's own `Settings`, so environment variables point it anywhere:

    CLICKHOUSE_HOST=clickhouse-production-xxxx.up.railway.app CLICKHOUSE_PORT=443 \
    CLICKHOUSE_USER=… CLICKHOUSE_PASSWORD=… uv run python scripts/storage_report.py

TLS is on automatically for any host that is not localhost (`CLICKHOUSE_SECURE=0|1` to force it).
Each store is optional: whatever this process cannot reach is reported as unreachable and the
rest still prints — so the same script is useful from a laptop and from inside the deployment.

Read-only. It runs `SELECT`s and `list_objects_v2`, and deletes nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from tracely.config import settings  # noqa: E402

# Windows a plan sweep or the table TTL would act on — the columns of the age table.
AGE_BUCKETS = (7, 30, 90)


def human(n: float | None) -> str:
    n = n or 0  # a sumIf over Nullable columns matching no rows comes back as NULL
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def table(rows: list[tuple], headers: tuple[str, ...]) -> str:
    """Left-align text, right-align everything else, one space of padding either side."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)
    ]
    numeric = (
        [
            all(
                r[i].replace(",", "").replace(".", "").replace("-", "").isdigit()
                or r[i].endswith(("B", "KB", "MB", "GB", "TB"))
                for r in cells
            )
            for i in range(len(headers))
        ]
        if cells
        else [False] * len(headers)
    )

    def line(vals: list[str]) -> str:
        return "  ".join(
            v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i]) for i, v in enumerate(vals)
        )

    out = [line(list(headers)), "  ".join("─" * w for w in widths)]
    out += [line(r) for r in cells]
    return "\n".join(out)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ── ClickHouse ────────────────────────────────────────────────────────────────


def clickhouse():
    """`(client, per-project rows)`, or `(None, [])` when ClickHouse is unreachable."""
    import clickhouse_connect

    host = settings.clickhouse_host
    secure = os.getenv("CLICKHOUSE_SECURE")
    secure = (
        secure not in ("0", "false", "")
        if secure is not None
        else host not in ("localhost", "127.0.0.1", "clickhouse", "clickhouse.railway.internal")
    )
    port = settings.clickhouse_port
    if secure and port == 8123:  # the default is the plaintext port; TLS never listens there
        port = 443
    try:
        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
            secure=secure,
        )
        client.query("SELECT 1")
    except Exception as exc:
        print(f"\n  ClickHouse unreachable at {host}:{port} — {type(exc).__name__}: {exc}")
        return None, []
    print(
        f"  ClickHouse {client.query('SELECT version()').result_rows[0][0]} "
        f"at {host}:{port} (db {settings.clickhouse_database})"
    )

    section("Disk by database")
    db_rows = client.query(
        "SELECT database, sum(bytes_on_disk), sum(rows), count() FROM system.parts "
        "WHERE active GROUP BY database ORDER BY sum(bytes_on_disk) DESC"
    ).result_rows
    print(
        table(
            [(d, human(b), f"{r:,}", f"{p:,}") for d, b, r, p in db_rows],
            ("database", "on disk", "rows", "parts"),
        )
    )

    # The trap this report exists to make visible: ClickHouse's own telemetry tables are
    # unbounded by default and routinely dwarf the product's data on a small deployment.
    sizes = {d: b for d, b, _, _ in db_rows}
    sys_b, app_b = sizes.get("system", 0), sizes.get(settings.clickhouse_database, 0)
    if sys_b > app_b:
        print(
            f"\n  ⚠ system.* logs hold {human(sys_b)} vs {human(app_b)} of Tracely data — "
            "ClickHouse's own telemetry, not customer traces. See the tail of this report."
        )

    section("Biggest tables")
    print(
        table(
            [
                (d, t, human(b), f"{r:,}")
                for d, t, b, r in client.query(
                    "SELECT database, table, sum(bytes_on_disk), sum(rows) FROM system.parts "
                    "WHERE active GROUP BY database, table ORDER BY sum(bytes_on_disk) DESC LIMIT 12"
                ).result_rows
            ],
            ("database", "table", "on disk", "rows"),
        )
    )

    section("Tracely tables — stored vs visible")
    rows = []
    for t, b, stored in client.query(
        "SELECT table, sum(bytes_on_disk), sum(rows) FROM system.parts WHERE active "
        "AND database = {db:String} GROUP BY table ORDER BY sum(bytes_on_disk) DESC",
        parameters={"db": settings.clickhouse_database},
    ).result_rows:
        visible = int(client.query(f"SELECT count() FROM {t}").result_rows[0][0])
        rows.append((t, human(b), f"{stored:,}", f"{visible:,}", f"{stored - visible:,}"))
    print(table(rows, ("table", "on disk", "stored rows", "visible", "masked")))
    print(
        "\n  `masked` = rows a lightweight DELETE has hidden but no merge has dropped yet.\n"
        "  They still cost disk. `OPTIMIZE TABLE events FINAL` reclaims it immediately."
    )

    section("Spans by project")
    per_project = client.query(
        "SELECT project_id, count(), uniqExact(trace_id), "
        "  countIf(internal_kind != ''), sum(length(input) + length(output)), "
        "  min(start_time)::Date, max(start_time)::Date "
        "FROM events GROUP BY project_id ORDER BY count() DESC"
    ).result_rows
    print(
        table(
            [
                (p, f"{s:,}", f"{t:,}", f"{i:,}", human(b), str(first), str(last))
                for p, s, t, i, b, first, last in per_project
            ],
            ("project_id", "spans", "traces", "internal", "payload", "first", "last"),
        )
    )
    print(
        "  `internal` = Tracely's own recordings (evals, scenarios, assistant turns).\n"
        "  `payload` = input+output bytes uncompressed — the share of the table a project owns."
    )

    section("What a retention sweep would delete today")
    cuts = client.query(
        "SELECT "
        + ", ".join(
            f"countIf(start_time < now() - INTERVAL {d} DAY), "
            f"sumIf(length(input) + length(output), start_time < now() - INTERVAL {d} DAY)"
            for d in AGE_BUCKETS
        )
        + " FROM events"
    ).result_rows[0]
    print(
        table(
            [
                (f"older than {d}d", f"{cuts[i * 2]:,}", human(cuts[i * 2 + 1]))
                for i, d in enumerate(AGE_BUCKETS)
            ],
            ("window", "spans", "payload"),
        )
    )
    return client, [(r[0], r[1]) for r in per_project]


# ── object storage ────────────────────────────────────────────────────────────


def blobs() -> dict[str, dict[str, tuple[int, int]]]:
    """`{project_id: {kind: (objects, bytes)}}` — empty when the bucket is unreachable.

    Keys are `events/<project>/otlp/…`, `events/<project>/assistant/…` and
    `events/fixtures/<project>/…`, so the project id sits at a different depth for fixtures.
    """
    from tracely.infrastructure.blob import s3

    section("Object storage (raw OTLP bodies — the source of truth)")
    per: dict[str, dict[str, tuple[int, int]]] = {}
    total_n = total_b = 0
    try:
        client = s3._s3()
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
            for obj in page.get("Contents", []):
                parts = obj["Key"].split("/")
                if len(parts) > 3 and parts[1] == "fixtures":
                    project, kind = parts[2], "fixtures"
                elif len(parts) > 3:
                    project, kind = parts[1], parts[2]
                else:
                    project, kind = "(unkeyed)", "?"
                n, b = per.setdefault(project, {}).get(kind, (0, 0))
                per[project][kind] = (n + 1, b + obj["Size"])
                total_n, total_b = total_n + 1, total_b + obj["Size"]
    except Exception as exc:
        print(f"  unreachable at {settings.s3_endpoint_url} — {type(exc).__name__}: {exc}")
        return {}

    print(f"  bucket {settings.s3_bucket}: {total_n:,} objects, {human(total_b)}\n")
    kinds = sorted({k for v in per.values() for k in v})
    rows = []
    for project, by_kind in sorted(per.items(), key=lambda kv: -sum(b for _, b in kv[1].values())):
        rows.append(
            (
                project,
                *(
                    f"{by_kind[k][0]:,} / {human(by_kind[k][1])}" if k in by_kind else "-"
                    for k in kinds
                ),
                human(sum(b for _, b in by_kind.values())),
            )
        )
    print(table(rows, ("project_id", *kinds, "total")))
    return per


# ── the registry: whose data is this ──────────────────────────────────────────


def owners(project_ids: set[str]) -> dict[str, str]:
    """`{project_id: "workspace · org (plan) · owner emails"}`, empty when Postgres is unreachable."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from tracely.infrastructure.db.models import (
        Organization,
        OrgMembership,
        Project,
        User,
    )

    try:
        engine = create_engine(settings.alembic_database_url, connect_args={"connect_timeout": 8})
        with Session(engine) as s:
            rows = s.execute(
                select(
                    Project.id,
                    Project.name,
                    Organization.name,
                    Organization.plan,
                    Organization.kind,
                    Organization.id,
                ).join(Organization, Organization.id == Project.organization_id, isouter=True)
            ).all()
            people: dict[str, list[str]] = {}
            for org_id, email, role in s.execute(
                select(OrgMembership.organization_id, User.email, OrgMembership.role)
                .join(User, User.id == OrgMembership.user_id)
                .order_by(OrgMembership.role)
            ).all():
                people.setdefault(org_id, []).append(f"{email}({role[0]})")
    except Exception as exc:
        print(
            f"\n  Postgres unreachable — {type(exc).__name__}: {exc}\n"
            "  (it is internal-only on Railway: run this with `railway ssh -s api` for names)"
        )
        return {}

    known = {}
    for pid, pname, oname, plan, kind, org_id in rows:
        who = ", ".join(people.get(org_id, [])) or "—"
        org = f"{oname} ({kind}/{plan})" if oname else "no organization"
        known[pid] = f"{pname} · {org} · {who}"

    section("Whose workspace is it")
    seen = sorted(project_ids)
    print(
        table(
            [(p, known.get(p, "⚠ NOT IN THE REGISTRY — orphaned data")) for p in seen],
            ("project_id", "workspace · organization (kind/plan) · members"),
        )
    )
    orphans = [p for p in seen if p not in known]
    if orphans:
        print(
            f"\n  ⚠ {len(orphans)} project id(s) hold storage but no longer exist in Postgres.\n"
            "  Deleted workspaces whose bytes outlived them — nothing in the product can reach\n"
            "  this data, and nothing will ever clean it up."
        )
    return known


def main() -> None:
    print("\033[1mTracely storage report\033[0m")
    client, per_project = clickhouse()
    per_blob = blobs()
    ids = {p for p, _ in per_project} | set(per_blob) - {"(unkeyed)"}
    owners(ids)

    if client:
        section("ClickHouse system logs")
        logs = client.query(
            "SELECT table, sum(bytes_on_disk) FROM system.parts WHERE active AND database = 'system' "
            "GROUP BY table HAVING sum(bytes_on_disk) > 0 ORDER BY 2 DESC"
        ).result_rows
        total = sum(b for _, b in logs)
        print(
            f"  {human(total)} of ClickHouse's own telemetry. It has no TTL by default and is\n"
            "  never read by Tracely. To cap it (persists across restarts, reclaims on merge):\n"
        )
        for t, _ in logs[:4]:
            print(f"    ALTER TABLE system.{t} MODIFY TTL event_date + INTERVAL 3 DAY;")
        print("    -- then TRUNCATE TABLE system.<name>; to reclaim the space immediately")


if __name__ == "__main__":
    main()
