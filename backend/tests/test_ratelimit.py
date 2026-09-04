"""The unauthenticated auth routes are rate-limited per IP; Redis down means no limit, not no login."""

from __future__ import annotations

from tracely.api import ratelimit


class FakeRedis:
    def __init__(self):
        self.n: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    def incr(self, key):
        self.n[key] = self.n.get(key, 0) + 1
        return self.n[key]

    def expire(self, key, secs):
        self.ttl[key] = secs


class DeadRedis:
    def incr(self, key):
        raise ConnectionError("redis is down")


async def test_login_is_limited_per_ip(client, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(ratelimit, "sync_redis", lambda: fake)
    bad = {"email": "nobody@x.test", "password": "wrong-wrong"}
    for _ in range(20):
        assert (await client.post("/auth/login", json=bad)).status_code == 401
    r = await client.post("/auth/login", json=bad)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    # another IP (first X-Forwarded-For hop) has its own budget
    r = await client.post("/auth/login", json=bad, headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert r.status_code == 401
    assert all(ttl == ratelimit.WINDOW_SECONDS for ttl in fake.ttl.values())


async def test_redis_outage_fails_open(client, monkeypatch):
    monkeypatch.setattr(ratelimit, "sync_redis", lambda: DeadRedis())
    bad = {"email": "nobody@x.test", "password": "wrong-wrong"}
    for _ in range(25):
        assert (await client.post("/auth/login", json=bad)).status_code == 401
