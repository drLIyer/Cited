import json
import threading

import httpx
import pytest

from conftest import FakeSentenceTransformer

LONG = "Pricing strategy for SaaS: charge for value, not cost, and test tiers. " * 10


def _rss_feed():
    return (f"<rss><channel><item><title>Pricing</title><link>https://x/p</link>"
            f"<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>"
            f"<description>{LONG}</description></item></channel></rss>").encode()


async def _tool_names(server):
    return {t.name for t in await server.mcp.list_tools()}


# ── Tool registration per mode ───────────────────────────────────────────────

async def test_stdio_tools(server_factory):
    s = server_factory()
    assert await _tool_names(s) == {"search_newsletter", "list_posts", "get_history",
                                    "search_my_history"}


async def test_http_tools_hide_admin_by_default(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    assert await _tool_names(s) == {"search_newsletter", "list_posts"}


async def test_http_admin_tool_opt_in(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret",
                       CITED_ADMIN_MCP_TOOL="1")
    assert "admin_get_history" in await _tool_names(s)


async def test_http_never_exposes_ask_newsletter(server_factory):
    s = server_factory(CITED_TRANSPORT="http", ANTHROPIC_API_KEY="k")
    assert "ask_newsletter" not in await _tool_names(s)


def test_instructions_do_not_overclaim(server_factory):
    s = server_factory()
    text = s.mcp.instructions
    assert "FULL archive" not in text
    assert "every public post" not in text
    assert "BEFORE falling back" not in text


# ── search_newsletter end-to-end (mocked network + fake models) ────────────────

async def test_search_newsletter_end_to_end(server_factory, mock_http):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    out = json.loads(await s.search_newsletter("https://feeds.example/feed.xml", "pricing tiers", 3))
    assert out["posts_indexed"] == 1
    assert out["results"][0]["date"] == "2024-01-01"
    assert "coverage_note" in out
    conf = out["confidence"]
    assert conf["level"] in ("high", "medium", "low")
    assert 0 <= conf["percent"] <= 100
    assert conf["display"].endswith(f"(best match {conf['percent']}%)")
    assert all(0 <= r["match_pct"] <= 100 for r in out["results"])
    rows = s._history.get()
    assert len(rows) == 1 and rows[0]["question"] == "pricing tiers"


async def test_search_runs_models_off_event_loop(server_factory, mock_http):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    FakeSentenceTransformer.threads.clear()
    await s.search_newsletter("https://feeds.example/feed.xml", "pricing")
    assert FakeSentenceTransformer.threads
    assert threading.get_ident() not in FakeSentenceTransformer.threads


async def test_search_newsletter_ssrf_error_message(server_factory, mock_http):
    s = server_factory(CITED_TRANSPORT="http")
    out = json.loads(await s.search_newsletter("http://169.254.169.254/latest/feed", "x"))
    assert "private or local" in out["error"]


async def test_search_bad_top_k(server_factory, mock_http):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    out = json.loads(await s.search_newsletter("https://feeds.example/feed.xml", "pricing", 0))
    assert len(out["results"]) == 1


async def test_http_mode_no_logging_without_admin_token(server_factory, mock_http):
    s = server_factory(CITED_TRANSPORT="http")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    await s.search_newsletter("https://feeds.example/feed.xml", "pricing")
    assert s._history.get() == []


async def test_http_mode_logs_with_token_but_no_memory_file(server_factory, mock_http, tmp_path):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    await s.search_newsletter("https://feeds.example/feed.xml", "pricing")
    assert len(s._history.get()) == 1
    assert not (tmp_path / "memory").exists()


async def test_ask_newsletter_empty_feed(server_factory, mock_http):
    s = server_factory(ANTHROPIC_API_KEY="k")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(
        200, content=b"<rss><channel></channel></rss>")
    out = json.loads(await s.ask_newsletter("https://feeds.example/feed.xml", "q"))
    assert "No public posts" in out["error"]


async def test_get_history_label(server_factory):
    s = server_factory()
    s._history.save("https://news.aakashg.com", "q", "a", [], 0.9)
    out = await s.get_history()
    assert "news.aakashg.com\n" in out and ".com.substack.com" not in out


# ── HTTP app: health + admin route ───────────────────────────────────────────

def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_health_lists_real_tools(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    s._pipeline.prewarm()
    async with _client(s.build_http_app()) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert sorted(r.json()["tools"]) == ["list_posts", "search_newsletter"]


async def test_admin_route(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    s._history.save("https://x.substack.com", "secret question", "a", [], 0.5)
    async with _client(s.build_http_app()) as c:
        assert (await c.get("/admin/history")).status_code == 401
        assert (await c.get("/admin/history",
                            headers={"Authorization": "Bearer wrong"})).status_code == 401
        r = await c.get("/admin/history?query=secret",
                        headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
        assert r.json()["entries"][0]["question"] == "secret question"
        assert r.headers["cache-control"] == "no-store"


async def test_admin_route_throttles(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    async with _client(s.build_http_app()) as c:
        for _ in range(5):
            await c.get("/admin/history", headers={"Authorization": "Bearer bad"})
        r = await c.get("/admin/history", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 429


async def test_admin_route_disabled_without_token(server_factory):
    s = server_factory(CITED_TRANSPORT="http")
    async with _client(s.build_http_app()) as c:
        r = await c.get("/admin/history", headers={"Authorization": "Bearer "})
    assert r.status_code == 404


async def test_admin_mcp_tool_uses_constant_time_check(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret",
                       CITED_ADMIN_MCP_TOOL="1")
    assert (await s.admin_get_history("nope")).startswith("Access denied")
    assert (await s.admin_get_history("s3cret")) == "No history found."


async def test_low_coverage_adds_disclaimer(server_factory, mock_http, monkeypatch):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    monkeypatch.setattr(s._pipeline, "coverage", lambda hits: {
        "level": "low", "label": "Limited coverage — draws on general AI knowledge",
        "color": "#e74c3c", "top_similarity": 0.31, "avg_similarity": 0.2, "percent": 31})
    out = json.loads(await s.search_newsletter("https://feeds.example/feed.xml", "quantum physics"))
    assert out["low_confidence"] is True
    assert out["disclaimer"].startswith("⚠️ Limited coverage: rss:feeds.example/feed.xml")
    assert "general AI knowledge" in out["disclaimer"] and "31%" in out["disclaimer"]
    assert "verbatim" in out["instructions_for_assistant"]
    row = s._history.get()[0]
    assert row["level"] == "low" and row["confidence"] == 0.31


async def test_high_coverage_has_no_disclaimer(server_factory, mock_http, monkeypatch):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    monkeypatch.setattr(s._pipeline, "coverage", lambda hits: {
        "level": "high", "label": "High coverage", "color": "#27ae60",
        "top_similarity": 0.7, "avg_similarity": 0.65, "percent": 70})
    out = json.loads(await s.search_newsletter("https://feeds.example/feed.xml", "pricing"))
    assert "disclaimer" not in out and out["low_confidence"] is False
    assert out["confidence"]["display"] == "High coverage (best match 70%)"


def test_instructions_require_showing_confidence(server_factory):
    text = server_factory().mcp.instructions
    assert "Confidence: High coverage (best match 71%)" in text
    assert "word for word" in text


async def test_tools_have_directory_annotations(server_factory):
    """Anthropic's directory requires a title and readOnlyHint/destructiveHint on every tool."""
    for env in ({}, {"CITED_TRANSPORT": "http"}):
        s = server_factory(**env)
        for tool in await s.mcp.list_tools():
            a = tool.annotations
            assert tool.title and a is not None, tool.name
            assert a.read_only_hint is True and a.destructive_hint is False, tool.name
            assert len(tool.name) <= 64


async def test_tool_is_named_search_newsletter(server_factory):
    names = {t.name for t in await server_factory().mcp.list_tools()}
    assert "search_newsletter" in names and "search_substack" not in names


async def test_landing_page_for_browsers_json_for_tools(server_factory):
    s = server_factory(CITED_TRANSPORT="http")
    async with _client(s.build_http_app()) as c:
        page = await c.get("/", headers={"accept": "text/html,application/xhtml+xml"})
        api = await c.get("/")
        js = await c.get("/landing.js")
        icon = await c.get("/icon.png")
    assert page.status_code == 200 and "Ask any newsletter or podcast" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "Illustration with a fictional newsletter" in page.text
    assert api.json()["mcp_endpoint"] == "/mcp"
    assert js.status_code == 200 and icon.headers["content-type"] == "image/png"
    assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"
