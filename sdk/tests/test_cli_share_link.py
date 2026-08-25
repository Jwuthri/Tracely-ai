"""The PR comment's link must survive a backend that can't mint one.

A public share link is a growth feature; the gate is a merge blocker. If minting ever takes the
check down with it, the trade is backwards — so these pin the fallback, not the happy path.
"""

from __future__ import annotations

import urllib.error

import pytest

from tracely_sdk import cli


def test_public_link_replaces_the_authed_one(monkeypatch):
    monkeypatch.setattr(cli, "_post_json", lambda url, key, body: {"token": "tok-123"})
    url = cli.mint_share_url("http://api", "k", "https://app.tracely.dev", "gate-1")
    assert url == "https://app.tracely.dev/share/tok-123"

    data = {"id": "gate-1", "share_url": url}
    assert cli.gate_link(data, "https://app.tracely.dev") == url


def test_an_older_backend_falls_back_to_the_gate_page(monkeypatch):
    def boom(url, key, body):
        raise urllib.error.HTTPError(url, 422, "unknown kind", {}, None)

    monkeypatch.setattr(cli, "_post_json", boom)
    assert cli.mint_share_url("http://api", "k", "https://app.tracely.dev", "gate-1") == ""
    assert (
        cli.gate_link({"id": "gate-1"}, "https://app.tracely.dev")
        == "https://app.tracely.dev/gates/gate-1"
    )


def test_an_unreachable_api_never_raises(monkeypatch):
    def boom(url, key, body):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli, "_post_json", boom)
    assert cli.mint_share_url("http://api", "k", "https://app.tracely.dev", "gate-1") == ""


@pytest.mark.parametrize(
    "api,key,web,gate", [("", "k", "w", "g"), ("a", "", "w", "g"), ("a", "k", "", "g"), ("a", "k", "w", "")]
)
def test_nothing_to_mint_with(api, key, web, gate, monkeypatch):
    """No web url (self-hosters often have none) means no page to point at — don't call the API."""
    monkeypatch.setattr(cli, "_post_json", lambda *a, **k: pytest.fail("should not be called"))
    assert cli.mint_share_url(api, key, web, gate) == ""


def test_markdown_links_to_the_public_page():
    md = cli.render_markdown(
        {"id": "g1", "status": "FAIL", "agent": "planner", "env": "ci",
         "passed": 1, "failed": 1, "skipped": 0, "cases": [],
         "share_url": "https://app.tracely.dev/share/tok"},
        "https://app.tracely.dev",
        "abc1234",
    )
    assert "https://app.tracely.dev/share/tok" in md
    assert "/gates/g1" not in md
