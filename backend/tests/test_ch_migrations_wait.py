"""The migration runner waits for a not-ready-yet ClickHouse instead of failing the deploy.

Railway never retries a failed pre-deploy command, and its private network needs a moment to come up
inside a fresh container — so one DNS blip used to kill the whole deploy.
"""

from __future__ import annotations

import pytest
from tracely.infrastructure.clickhouse import migrations as m


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)


def test_retries_until_clickhouse_answers(monkeypatch):
    attempts = []

    def flaky(database=None):
        attempts.append(database)
        if len(attempts) < 3:
            raise OSError("Name or service not known")
        return "client"

    monkeypatch.setattr(m, "get_client", flaky)
    assert m._connect_when_ready(timeout=60.0) == "client"
    assert len(attempts) == 3


def test_gives_up_with_the_host_it_tried(monkeypatch):
    monkeypatch.setattr(
        m, "get_client", lambda database=None: (_ for _ in ()).throw(OSError("nope"))
    )
    monkeypatch.setattr(m.settings, "clickhouse_host", "clickhouse.railway.internal")
    # timeout=0 -> one attempt, then the actionable error (not the driver's DNS traceback)
    with pytest.raises(RuntimeError, match="clickhouse.railway.internal:8123"):
        m._connect_when_ready(timeout=0.0)


def test_postgres_store_skips_clickhouse_entirely(monkeypatch, capsys):
    """Pre-deploy runs this before `alembic upgrade head`; once ClickHouse is deleted it must
    return at once instead of waiting for a service that no longer exists."""
    monkeypatch.setattr(m.settings, "span_store", "postgres")
    monkeypatch.setattr(m, "get_client", lambda *a, **k: pytest.fail("ClickHouse contacted"))
    m.main()
    assert "no ClickHouse migrations" in capsys.readouterr().out
