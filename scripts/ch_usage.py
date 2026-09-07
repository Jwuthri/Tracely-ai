"""What is in ClickHouse, how much of it, and whose it is.

Usage (from repo root):
    uv run python scripts/ch_usage.py                    # local docker stack
    CH_HOST=clickhouse-production-xxxx.up.railway.app \
    CH_PASS=... PG_URL=postgresql://... \
        uv run python scripts/ch_usage.py                # prod

Connection
----------
ClickHouse over its HTTP interface: `CH_HOST` / `CH_PORT` / `CH_USER` / `CH_PASS` / `CH_DB`.
TLS turns itself on (and the port becomes 443) for any host that isn't localhost — Railway
publishes only the HTTPS edge, and 8123 there is the *container* port, which times out.

`PG_URL` is optional and only buys names: project id -> workspace / org / plan / member emails.
Railway keeps Postgres off the public internet unless a TCP proxy exists, so without it the
report still runs and prints bare ids.

Read-only. Nothing here writes or deletes.
"""

from __future__ import annotations

import os

import clickhouse_connect

CH_HOST = os.environ.get("CH_HOST", "localhost")
_LOCAL = CH_HOST in ("localhost", "127.0.0.1", "clickhouse")
CH_SECURE = os.environ.get("CH_SECURE", "0" if _LOCAL else "1") == "1"
CH_PORT = int(os.environ.get("CH_PORT", 443 if CH_SECURE else 8123))
CH_DB = os.environ.get("CH_DB", "tracely")
PG_URL = os.environ.get("PG_URL", "")


def _h(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _registry() -> dict[str, tuple[str, str, str, str]]:
    """project_id -> (workspace, org, plan, members). Empty when Postgres isn't reachable."""
    if not PG_URL:
        return {}
    try:
        import psycopg

        with psycopg.connect(PG_URL, connect_timeout=15) as c:
            rows = c.execute(
                "SELECT p.id, p.name, coalesce(o.name,'(no org)'), coalesce(o.plan,'-'), "
                "coalesce((SELECT string_agg(u.email, ', ') FROM organization_memberships m "
                "  JOIN users u ON u.id = m.user_id WHERE m.organization_id = o.id), '-') "
                "FROM projects p LEFT JOIN organizations o ON o.id = p.organization_id"
            ).fetchall()
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
    except Exception as exc:  # noqa: BLE001 — names are a nicety, ids are the report
        print(f"  (no Postgres enrichment: {type(exc).__name__}: {str(exc)[:80]})\n")
        return {}


def main() -> None:
    ch = clickhouse_connect.get_client(
        host=CH_HOST,
        port=CH_PORT,
        secure=CH_SECURE,
        username=os.environ.get("CH_USER", "default"),
        password=os.environ.get("CH_PASS", ""),
        database=CH_DB,
    )
    print(f"\n{'=' * 78}\n  {CH_HOST}  ·  ClickHouse {ch.query('SELECT version()').result_rows[0][0]}\n{'=' * 78}\n")

    # ── 1. disk, by database then table ───────────────────────────────────────
    print("DISK BY DATABASE")
    dbs = ch.query(
        "SELECT database, sum(bytes_on_disk) FROM system.parts WHERE active "
        "GROUP BY database ORDER BY 2 DESC"
    ).result_rows
    for db, b in dbs:
        print(f"  {db:<24} {_h(b):>12}")
    sysb = sum(b for d, b in dbs if d == "system")
    ourb = sum(b for d, b in dbs if d != "system")
    if sysb > ourb:
        print(f"\n  !! ClickHouse's own logs are {sysb / max(ourb, 1):.0f}x your data.")
        print("     They have no TTL by default. Top offenders:")
        for t, b, r in ch.query(
            "SELECT table, sum(bytes_on_disk), sum(rows) FROM system.parts "
            "WHERE active AND database = 'system' GROUP BY table ORDER BY 2 DESC LIMIT 5"
        ).result_rows:
            print(f"       system.{t:<28} {_h(b):>10}  {r:>14,} rows")

    # ── 2. our tables: stored vs visible ──────────────────────────────────────
    print(f"\nTABLES IN `{CH_DB}`")
    for t, b, stored in ch.query(
        "SELECT table, sum(bytes_on_disk), sum(rows) FROM system.parts "
        f"WHERE active AND database = '{CH_DB}' GROUP BY table ORDER BY 2 DESC"
    ).result_rows:
        live = ch.query(f"SELECT count() FROM {CH_DB}.{t}").result_rows[0][0]
        # A lightweight DELETE only masks rows; they hold disk until the next merge.
        ghost = f"   ({stored - live:,} deleted, not yet merged away)" if stored > live else ""
        print(f"  {t:<12} {_h(b):>10}  {live:>10,} rows{ghost}")

    # ── 3. whose is it ────────────────────────────────────────────────────────
    reg = _registry()
    print("\nEVENTS BY WORKSPACE")
    rows = ch.query(
        "SELECT project_id, count(), uniqExact(trace_id), "
        "  countIf(internal_kind != ''), sum(length(input) + length(output)), "
        "  min(start_time)::Date, max(start_time)::Date "
        f"FROM {CH_DB}.events GROUP BY project_id ORDER BY 5 DESC"
    ).result_rows
    hdr = f"  {'workspace':<22} {'org':<14} {'plan':<10} {'spans':>7} {'internal':>9} {'payload':>10}  last seen"
    print(hdr if reg else f"  {'project_id':<38} {'spans':>7} {'internal':>9} {'payload':>10}  last seen")
    orphans = []
    for pid, spans, traces, internal, payload, first, last in rows:
        if reg:
            if pid not in reg:
                orphans.append((pid, spans, payload, last))
                continue
            ws, org, plan, _members = reg[pid]
            print(f"  {ws[:22]:<22} {org[:14]:<14} {plan:<10} {spans:>7,} {internal:>9,} {_h(payload):>10}  {last}")
        else:
            print(f"  {pid:<38} {spans:>7,} {internal:>9,} {_h(payload):>10}  {last}")

    if orphans:
        print("\n  ORPHANED — spans whose workspace no longer exists in Postgres:")
        for pid, spans, payload, last in orphans:
            print(f"    {pid}  {spans:>7,} spans  {_h(payload):>10}  last {last}")
        print("    Nothing in the product can reach these; they age out with the table TTL.")

    # ── 4. what retention would reclaim ───────────────────────────────────────
    print("\nAGE OF SPANS")
    for label, days in (("older than 7d", 7), ("older than 30d", 30), ("older than 90d", 90)):
        n, p = ch.query(
            f"SELECT count(), sum(length(input) + length(output)) FROM {CH_DB}.events "
            f"WHERE start_time < now() - INTERVAL {days} DAY"
        ).result_rows[0]
        print(f"  {label:<16} {n:>8,} spans  {_h(p or 0):>10}")
    print()


if __name__ == "__main__":
    main()
