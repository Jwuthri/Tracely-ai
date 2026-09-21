"""The nightly sweep that caps ClickHouse's own log tables.

It runs unattended against a live database, so the two things that matter are: it stops doing
work once the cap is in place, and nothing that isn't a plain system log table reaches a DDL
string.
"""

from __future__ import annotations

import pytest

from tracely.infrastructure.clickhouse import maintenance


class FakeClient:
    """The three reads the sweep makes: sizes, engine definitions, which tables have event_date."""

    def __init__(self, parts: list[tuple[str, int]], tables: list[tuple[str, str, int]]):
        self._parts = parts
        self._engines = [(name, engine) for name, engine, _ in tables]
        self._dated = [(name,) for name, _, has_date in tables if has_date]
        self.commands: list[str] = []

    def query(self, sql: str, parameters: dict | None = None):
        rows = (
            self._parts
            if "system.parts" in sql
            else self._dated
            if "system.columns" in sql
            else self._engines
        )
        return type("R", (), {"result_rows": rows})()

    def command(self, sql: str, parameters: dict | None = None):
        self.commands.append(sql)


@pytest.fixture
def client(monkeypatch):
    def install(parts, tables):
        c = FakeClient(parts, tables)
        monkeypatch.setattr(maintenance, "get_client", lambda database=None: c)
        return c

    return install


def test_caps_uncapped_log_tables(client):
    c = client(
        [("query_log", 900), ("trace_log", 100)],
        [("query_log", "MergeTree PARTITION BY event_date", 1), ("trace_log", "MergeTree", 1)],
    )
    out = maintenance.cap_system_logs(3)
    assert out["capped"] == ["query_log", "trace_log"]  # biggest first
    assert c.commands == [
        "ALTER TABLE system.query_log MODIFY TTL event_date + INTERVAL 3 DAY",
        "ALTER TABLE system.trace_log MODIFY TTL event_date + INTERVAL 3 DAY",
    ]
    assert out["bytes"] == 1000


def test_second_night_is_a_no_op(client):
    c = client(
        [("query_log", 900)],
        [("query_log", "MergeTree PARTITION BY event_date TTL event_date + toIntervalDay(3)", 1)],
    )
    assert maintenance.cap_system_logs(3)["capped"] == []
    assert c.commands == []


def test_skips_tables_it_cannot_safely_alter(client):
    c = client(
        [("metric_log", 500), ("weird-name", 400)],
        [("metric_log", "MergeTree", 0), ("weird-name", "MergeTree", 1)],  # no event_date / unsafe
    )
    out = maintenance.cap_system_logs(3)
    assert out["capped"] == [] and set(out["skipped"]) == {"metric_log", "weird-name"}
    assert c.commands == []


def test_disabled_by_zero(client):
    c = client([("query_log", 900)], [("query_log", "MergeTree", 1)])
    assert maintenance.cap_system_logs(0) == {"skipped": "disabled"}
    assert c.commands == []


def test_one_failing_table_does_not_stop_the_rest(client):
    c = client(
        [("query_log", 900), ("trace_log", 100)],
        [("query_log", "MergeTree", 1), ("trace_log", "MergeTree", 1)],
    )

    def boom(sql, parameters=None):
        if "query_log" in sql:
            raise RuntimeError("read-only replica")
        c.commands.append(sql)

    c.command = boom
    out = maintenance.cap_system_logs(3)
    assert out["capped"] == ["trace_log"] and out["skipped"] == ["query_log"]


def test_tightens_a_ttl_that_is_longer_than_ours(client):
    """ClickHouse images ship their own 30-day TTL, so "has a TTL" is not "capped"."""
    c = client(
        [("query_log", 900), ("trace_log", 100)],
        [
            ("query_log", "MergeTree TTL event_date + toIntervalDay(30)", 1),
            ("trace_log", "MergeTree TTL event_date + toIntervalDay(1)", 1),
        ],
    )
    out = maintenance.cap_system_logs(3)
    assert out["capped"] == ["query_log"]  # 30d tightened, 1d left alone
    assert c.commands == ["ALTER TABLE system.query_log MODIFY TTL event_date + INTERVAL 3 DAY"]


def test_leaves_an_unrecognized_ttl_expression_alone(client):
    """Someone else's TTL (a different column, a MOVE rule) is not ours to overwrite."""
    c = client([("query_log", 900)], [("query_log", "MergeTree TTL toDate(event_time) + 7", 1)])
    assert maintenance.cap_system_logs(3)["capped"] == []
    assert c.commands == []
