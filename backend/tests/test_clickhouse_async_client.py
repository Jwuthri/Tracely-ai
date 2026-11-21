"""The shared async ClickHouse client is bound to the event loop that created it."""

import asyncio

from tracely.infrastructure.clickhouse import client as ch


class _Fake:
    closed = False

    async def close(self):
        self.closed = True


def test_a_new_event_loop_gets_a_new_client(monkeypatch):
    made: list[_Fake] = []

    async def new(_database):
        made.append(_Fake())
        return made[-1]

    monkeypatch.setattr(ch, "_new_async_client", new)
    monkeypatch.setattr(ch, "_async_client", None)
    monkeypatch.setattr(ch, "_async_client_loop", None)

    async def twice():
        return await ch.get_async_client(), await ch.get_async_client()

    # one loop (the API): one client, reused
    a, b = asyncio.run(twice())
    assert a is b and len(made) == 1

    # a second `asyncio.run` (the next worker task): a fresh client, the stale one closed
    c, _ = asyncio.run(twice())
    assert c is not a and len(made) == 2 and a.closed and not c.closed
