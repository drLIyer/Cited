import json
import logging
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

import digest
import events
import fetcher
import healer
import pipeline

AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def srv(server_factory, monkeypatch):
    monkeypatch.setattr(healer, "VERIFY_AFTER", 0)
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    fetcher._PAUSED.clear()
    yield s
    fetcher._PAUSED.clear()
    events.configure(False, events._db_path)


def ctx_for(ip):
    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"), headers={"x-forwarded-for": ip})
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


async def _fail_feed(s, mock_http, n, url="https://feeds.example/feed.xml"):
    mock_http[url] = httpx.Response(503)
    for _ in range(n):
        fetcher._CACHE.clear()
        await s.list_posts(url)


# ── Fix → verify → remember → re-apply ───────────────────────────────────────

async def test_failing_feed_is_paused_verified_and_remembered(srv, mock_http):
    await _fail_feed(srv, mock_http, 3)
    taken = srv._HEALER.run_cycle({"checks": {}})
    assert [t["action"] for t in taken] == ["pause_source"]
    assert "feeds.example" in fetcher.paused_hosts()

    # While paused, requests get a friendly message instead of hammering the feed
    out = await srv.list_posts("https://feeds.example/feed.xml")
    assert "paused" in out

    srv._HEALER.run_cycle({"checks": {}})              # verification pass: no recurrence → worked
    g = events.groups(category="error")[0]
    assert g["state"] == "fixed" and g["resolved_by"] == "auto"
    pb = srv._HEALER.playbook()[0]
    assert pb["action"] == "pause_source" and pb["successes"] == 1 and pb["trusted"]
    assert events.fix_stats()["auto_fixed"] == 1


async def test_remembered_fix_is_applied_immediately_next_time(srv, mock_http):
    await _fail_feed(srv, mock_http, 3)
    srv._HEALER.run_cycle({"checks": {}})
    srv._HEALER.run_cycle({"checks": {}})              # verified + remembered
    fetcher._PAUSED.clear()

    await _fail_feed(srv, mock_http, 1)               # just ONE failure, below the threshold
    taken = srv._HEALER.run_cycle({"checks": {}})
    assert len(taken) == 1 and taken[0]["from_playbook"] is True
    assert "feeds.example" in fetcher.paused_hosts()
    recent = srv._HEALER.recent_actions()
    assert recent[0]["from_playbook"] == 1


async def test_fix_that_does_not_work_is_flagged(srv, mock_http):
    await _fail_feed(srv, mock_http, 3)
    srv._HEALER.run_cycle({"checks": {}})
    srv._HEALER._pending[0]["applied_at"] -= 100       # pretend the fix was a while ago
    logging.getLogger("x")  # noqa
    fetcher._PAUSED.clear()
    await _fail_feed(srv, mock_http, 1)               # it happens again
    srv._HEALER.run_cycle({"checks": {}})
    pb = srv._HEALER.playbook()[0]
    assert pb["failures"] == 1 and pb["successes"] == 0 and not pb["trusted"]
    assert any(g["kind"] == "auto_fix_failed" for g in events.groups(category="autofix"))


def test_broken_search_triggers_model_reload(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "reload_models", lambda: calls.append(1))
    srv._HEALER.run_cycle({"checks": {"models": {"status": "ok"}, "search_selftest": {"status": "error"}}})
    assert calls == [1]
    srv._HEALER.run_cycle({"checks": {"models": {"status": "ok"}, "search_selftest": {"status": "ok"}}})
    assert srv._HEALER.recent_actions()[0]["result"] == "worked"


def test_model_exception_in_log_triggers_reload(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "reload_models", lambda: calls.append(1))
    try:
        raise RuntimeError("CrossEncoder predict failed in pipeline.py")
    except RuntimeError:
        logging.getLogger("cited").exception("search_substack failed (url=%s)", "https://x.example/feed")
    srv._HEALER.run_cycle({"checks": {}})
    assert calls == [1]


def test_memory_pressure_frees_caches(srv):
    pipeline._EMBED_CACHE["x"] = ("fp", None, 0)
    fetcher._CACHE["y"] = ([], 0)
    srv._HEALER.run_cycle({"checks": {"memory": {"status": "warn"}}})
    assert pipeline._EMBED_CACHE == {} and fetcher._CACHE == {}


def test_hourly_safety_cap(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "reload_models", lambda: None)
    bad = {"checks": {"models": {"status": "error"}, "search_selftest": {"status": "starting"}}}
    for _ in range(6):
        srv._HEALER._pending.clear()
        srv._HEALER.run_cycle(bad)
    results = [a["result"] for a in srv._HEALER.recent_actions()]
    assert results.count("applied") == 3 and "skipped" in results


def test_unknown_errors_are_left_for_a_human(srv):
    logging.getLogger("x").error("Something odd happened")
    assert srv._HEALER.run_cycle({"checks": {}}) == []


# ── Abuse → temporary block (IPs never stored) ───────────────────────────────

async def test_repeat_attacker_is_blocked_and_ip_not_stored(srv, tmp_path):
    attacker = ctx_for("203.0.113.66")
    for _ in range(3):
        await srv.search_substack("https://169.254.169.254/latest/feed", "x", ctx=attacker)
    taken = srv._HEALER.run_cycle({"checks": {}})
    assert [t["action"] for t in taken] == ["block_ip"]

    out = json.loads(await srv.search_substack("https://feeds.example/feed.xml", "x", ctx=attacker))
    assert "temporarily blocked" in out["error"]
    out = await srv.list_posts("https://feeds.example/feed.xml", ctx=ctx_for("198.51.100.1"))
    assert "blocked" not in out                       # other users unaffected

    raw = (tmp_path / "history.db").read_bytes()
    assert b"203.0.113.66" not in raw
    assert srv._HEALER.playbook()[0]["params"] == {"ip": "(hidden)"}


def test_agent_can_be_disabled(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret", CITED_AUTOHEAL="0")
    s._HEALER.note_offense("1.2.3.4", "blocked_url")
    assert s._HEALER.run_cycle({"checks": {"memory": {"status": "warn"}}}) == []


async def test_dashboard_shows_agent_and_forget(srv, mock_http):
    await _fail_feed(srv, mock_http, 3)
    srv._HEALER.run_cycle({"checks": {}})
    app = srv.build_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        o = (await c.get("/admin/api/overview", headers=AUTH)).json()
        af = o["autofix"]
        assert af["enabled"] and af["stats_24h"]["applied"] == 1
        assert af["paused_sources"][0]["host"] == "feeds.example"
        fp = af["playbook"][0]["fingerprint"]
        assert (await c.post("/admin/api/forget", json={"fingerprint": fp}, headers=AUTH)).status_code == 200
        assert (await c.get("/admin/api/overview", headers=AUTH)).json()["autofix"]["playbook"] == []


# ── Daily digest ─────────────────────────────────────────────────────────────

@pytest.fixture
def resend(monkeypatch):
    sent = []
    monkeypatch.setenv("CITED_DIGEST_TO", "owner@example.com")
    monkeypatch.setenv("CITED_DIGEST_FROM", "Cited <alerts@example.com>")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("CITED_PUBLIC_URL", "https://cited.example")
    monkeypatch.setattr(digest.httpx, "post",
                        lambda url, **kw: sent.append((url, kw)) or httpx.Response(200, json={"id": "1"}))
    return sent


async def test_digest_content_and_privacy(srv, resend, mock_http):
    srv._history.save("https://a.substack.com", "my private question", "x", [], 0.3,
                      update_memory=False, level="low")
    await _fail_feed(srv, mock_http, 2)
    await srv.search_substack("https://127.0.0.1/feed", "x")
    data = await srv._digest_data()
    subject, text, html_body = digest.build(data, "https://cited.example")
    assert subject.startswith("Cited daily digest")
    for section in ("STATUS", "SEARCHES & CONFIDENCE", "ERRORS (24 H)", "BUGS FIXED",
                    "AUTO-FIX AGENT (24 H)", "THREATS BLOCKED"):
        assert section in text
    assert "Couldn't fetch a feed" in text and "Blocked unsafe URL: 1" in text
    assert "my private question" not in text and "my private question" not in html_body
    assert "https://cited.example/admin" in html_body


async def test_digest_sends_once_per_day(srv, resend, monkeypatch):
    db = srv._history.DB_PATH
    monkeypatch.setenv("CITED_DIGEST_HOUR_UTC", "9")
    early = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    assert not digest.is_due(db, early)
    assert digest.is_due(db, later)
    digest.mark_sent(db, "2026-11-22")
    assert not digest.is_due(db, later)
    assert digest.is_due(db, datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc))


async def test_resend_payload(srv, resend):
    await srv._send_digest(mark=False)
    url, kw = resend[0]
    assert url == "https://api.resend.com/emails"
    assert kw["json"]["to"] == ["owner@example.com"]
    assert kw["headers"]["Authorization"] == "Bearer re_test"
    assert digest.last_sent(srv._history.DB_PATH) is None      # test sends don't count


def test_smtp_sending(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): sent["host"] = (host, port)
        def starttls(self, context): sent["tls"] = True
        def login(self, u, p): sent["login"] = u
        def send_message(self, msg): sent["to"] = msg["To"]; sent["html"] = msg.get_body(("html",)) is not None
        def quit(self): pass

    for k in ("RESEND_API_KEY",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CITED_DIGEST_TO", "owner@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    assert digest.send("s", "t", "<b>h</b>") == "sent via SMTP"
    assert sent == {"host": ("smtp.example.com", 587), "tls": True, "login": "me@example.com",
                    "to": "owner@example.com", "html": True}


async def test_test_digest_endpoint(srv, monkeypatch):
    for k in ("CITED_DIGEST_TO", "RESEND_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    app = srv.build_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/api/digest/test", json={}, headers=AUTH)
        assert r.status_code == 400 and "CITED_DIGEST_TO" in r.json()["error"]
        assert (await c.post("/admin/api/digest/test", json={})).status_code == 401
