"""Error log, threat tracking, fix tracking, admin dashboard API, and new health checks."""
import asyncio
import json
import logging
import threading
import time

import httpx
import pytest

import events
import pipeline

LONG = "Pricing strategy for SaaS: charge for value, not cost, and test tiers. " * 10
FEED = (f"<rss><channel><item><title>Pricing</title><link>https://x/p</link>"
        f"<description>{LONG}</description></item></channel></rss>").encode()
AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def srv(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    yield s
    events.configure(False, events._db_path)


def client(s):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=s.build_http_app()), base_url="http://t")


# ── Event capture & grouping ──────────────────────────────────────────────────

def test_any_logger_error_is_captured_with_traceback(srv):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("some.library").exception("Library failed on %s", "item-1")
    g = events.groups(category="error")
    assert len(g) == 1
    assert g[0]["kind"] == "exception" and g[0]["source"] == "some.library"
    assert "ValueError: boom" in g[0]["detail"]


def test_same_problem_is_grouped_not_duplicated(srv):
    for i in range(5):
        logging.getLogger("x").error("Timeout talking to %s", f"host-{i}")
    g = events.groups(category="error")
    assert len(g) == 1 and g[0]["count"] == 5


def test_info_logs_are_not_captured(srv):
    logging.getLogger("x").info("just info")
    assert events.groups() == []


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_crash_is_captured(srv):
    def bad():
        raise RuntimeError("thread exploded")
    t = threading.Thread(target=bad, name="worker-1")
    t.start(); t.join()
    g = events.groups(category="error")
    assert any(e["kind"] == "uncaught_exception" and "thread exploded" in e["message"] for e in g)


def test_disabled_without_admin_token(server_factory):
    s = server_factory(CITED_TRANSPORT="http")
    logging.getLogger("x").error("nope")
    assert events.enabled() is False and events.groups() == []


def test_events_never_store_questions_or_ips(srv, mock_http):
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(500)
    asyncio.run(srv.search_newsletter("https://feeds.example/feed.xml", "my secret question"))
    dump = json.dumps(events.query(limit=100))
    assert "my secret question" not in dump


# ── Threats ──────────────────────────────────────────────────────────────────

async def test_ssrf_attempt_counts_as_threat(srv):
    await srv.search_newsletter("https://169.254.169.254/latest/meta-data/feed", "x")
    await srv.list_posts("http://127.0.0.1/feed")
    sec = events.security_summary()
    blocked = next(t for t in sec["types"] if t["kind"] == "blocked_url")
    assert blocked["24h"] == 2 and sec["total_24h"] == 2


async def test_xml_bomb_counts_as_blocked_content(srv, mock_http):
    bomb = b"""<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aa"><!ENTITY b "&a;&a;">]>
<rss><channel><item><title>&b;</title></item></channel></rss>"""
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=bomb)
    out = json.loads(await srv.search_newsletter("https://feeds.example/feed.xml", "x"))
    assert "unsafe XML" in out["error"]
    kinds = {g["kind"] for g in events.groups(category="security")}
    assert kinds == {"blocked_content"}


async def test_failed_admin_logins_and_lockout_are_threats(srv):
    async with client(srv) as c:
        for _ in range(5):
            await c.get("/admin/api/overview", headers={"Authorization": "Bearer tok-ZQX9871"})
    sec = {t["kind"]: t["24h"] for t in events.security_summary()["types"]}
    assert sec["admin_auth_failed"] == 5 and sec["admin_lockout"] == 1
    assert "tok-ZQX9871" not in json.dumps(events.query(limit=100))   # token never stored


async def test_fetch_failure_is_an_error_not_a_threat(srv, mock_http):
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(503)
    await srv.list_posts("https://feeds.example/feed.xml")
    assert [g["kind"] for g in events.groups(category="error")] == ["fetch_failed"]
    assert events.security_summary()["total_24h"] == 0


# ── Fix tracking ─────────────────────────────────────────────────────────────

def _one_error():
    logging.getLogger("x").error("Database locked")
    return events.groups(category="error")[0]["fingerprint"]


def test_mark_fixed_then_regression(srv):
    fp = _one_error()
    assert events.fix_stats()["open_errors"] == 1
    assert events.resolve(fp, "Added retry", by="admin")
    g = events.groups(category="error")[0]
    assert g["state"] == "fixed" and g["note"] == "Added retry"
    st = events.fix_stats()
    assert (st["fixed"], st["holding"], st["regressed"], st["open_errors"]) == (1, 1, 0, 0)

    time.sleep(0.01)
    logging.getLogger("x").error("Database locked")      # it happens again
    g = events.groups(category="error")[0]
    assert g["state"] == "regressed"
    st = events.fix_stats()
    assert (st["holding"], st["regressed"], st["open_errors"]) == (0, 1, 1)


def test_reopen_and_auto_fix_count(srv):
    fp = _one_error()
    events.resolve(fp, "auto restart", by="auto")
    assert events.fix_stats()["auto_fixed"] == 1
    assert events.reopen(fp)
    assert events.groups(category="error")[0]["state"] == "open"


def test_resolve_unknown_fingerprint(srv):
    assert events.resolve("nope") is False


# ── Admin dashboard & API ────────────────────────────────────────────────────

async def test_dashboard_page_and_security_headers(srv):
    async with client(srv) as c:
        page = await c.get("/admin")
        js = await c.get("/admin/app.js")
    assert page.status_code == 200 and "<title>Cited Admin</title>" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    import re
    assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write", js.text)


async def test_dashboard_hidden_without_admin_token(server_factory):
    s = server_factory(CITED_TRANSPORT="http")
    async with client(s) as c:
        assert (await c.get("/admin")).status_code == 404
        assert (await c.get("/admin/api/overview", headers=AUTH)).status_code == 404


async def test_admin_api_requires_token(srv):
    async with client(srv) as c:
        for path in ("/admin/api/overview", "/admin/api/searches", "/admin/api/errors"):
            assert (await c.get(path)).status_code == 401
        assert (await c.post("/admin/api/resolve", json={"fingerprint": "x"})).status_code == 401


async def test_overview_contents(srv, mock_http):
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=FEED)
    await srv.search_newsletter("https://feeds.example/feed.xml", "pricing")
    await srv.search_newsletter("https://127.0.0.1/feed", "x")
    async with client(srv) as c:
        o = (await c.get("/admin/api/overview", headers=AUTH)).json()
    assert o["health"]["summary"]
    assert o["searches"]["searches"] == 1
    assert o["security"]["total_24h"] == 1
    assert o["fixes"]["enabled"] is True
    assert "search_quality" in o["health"]["checks"] and "db_size" in o["health"]["checks"]


async def test_errors_and_searches_endpoints(srv, mock_http):
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=FEED)
    await srv.search_newsletter("https://feeds.example/feed.xml", "pricing")
    logging.getLogger("x").error("Something broke")
    async with client(srv) as c:
        errs = (await c.get("/admin/api/errors?category=error", headers=AUTH)).json()
        allsearch = (await c.get("/admin/api/searches", headers=AUTH)).json()
        bad = await c.get("/admin/api/nope", headers=AUTH)
    assert errs["groups"][0]["name"] == "Logged warning/error"
    assert allsearch["searches"][0]["question"] == "pricing"
    assert bad.status_code == 404


async def test_resolve_and_reopen_via_api(srv):
    fp = _one_error()
    async with client(srv) as c:
        r = await c.post("/admin/api/resolve", json={"fingerprint": fp, "note": "fixed it"}, headers=AUTH)
        assert r.status_code == 200
        assert (await c.get("/admin/api/resolve", headers=AUTH)).status_code == 405
        assert (await c.post("/admin/api/resolve", content=b"not json", headers=AUTH)).status_code == 400
        assert (await c.post("/admin/api/resolve", json={"fingerprint": "zzz"}, headers=AUTH)).status_code == 404
        assert (await c.post("/admin/api/reopen", json={"fingerprint": fp}, headers=AUTH)).status_code == 200


# ── New health checks ────────────────────────────────────────────────────────

async def test_search_quality_alert(srv):
    srv._recent_levels.clear()
    srv._recent_levels.extend(["low"] * 6 + ["high"] * 4)
    async with client(srv) as c:
        body = (await c.get("/health")).json()
    q = body["checks"]["search_quality"]
    assert q["status"] == "warn" and q["limited_pct"] == 60
    assert body["status"] == "degraded" and q["meaning"] and q["action"]


async def test_search_quality_needs_five_searches(srv):
    srv._recent_levels.clear()
    srv._recent_levels.extend(["low"] * 4)
    async with client(srv) as c:
        assert (await c.get("/health")).json()["checks"]["search_quality"]["status"] == "ok"


async def test_recent_errors_alert(srv):
    for i in range(5):
        logging.getLogger(f"mod{i}").error("distinct failure %d", i)
    async with client(srv) as c:
        body = (await c.get("/health")).json()
    assert body["checks"]["recent_errors"]["status"] == "warn"
    assert body["checks"]["recent_errors"]["errors_last_hour"] >= 5


async def test_db_size_alert(srv, monkeypatch):
    monkeypatch.setenv("CITED_DB_SIZE_WARN_MB", "0.0001")
    srv._history.save("https://x.substack.com", "q", "a", [], 0.5, update_memory=False)
    async with client(srv) as c:
        assert (await c.get("/health")).json()["checks"]["db_size"]["status"] == "warn"


async def test_health_change_is_logged(srv):
    srv._recent_levels.clear()
    async with client(srv) as c:
        await c.get("/health")                      # ok (first status: not logged)
        srv._recent_levels.extend(["low"] * 10)
        await c.get("/health")                      # → degraded
    g = events.groups(category="health")
    assert g and "from ok to degraded" in g[0]["message"]


def test_background_monitor_runs(server_factory, monkeypatch):
    """The lifespan hook starts the 60-second monitor (here: a short interval)."""
    import uvicorn, socket
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    calls = []
    real = s._HEALTH.report

    async def counting(*a, **k):
        calls.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(s._HEALTH, "report", counting)
    monkeypatch.setattr(s, "_monitor_task", None)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = uvicorn.Server(uvicorn.Config(s.build_http_app(monitor_interval=0.05), host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    deadline = time.time() + 5
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.05)
    server.should_exit = True; t.join(5)
    assert len(calls) >= 3
