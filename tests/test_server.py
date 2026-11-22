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
    assert await _tool_names(s) == {"search_substack", "list_posts", "get_history",
                                    "search_my_history"}


async def test_http_tools_hide_admin_by_default(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    assert await _tool_names(s) == {"search_substack", "list_posts"}


async def test_http_admin_tool_opt_in(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret",
                       CITED_ADMIN_MCP_TOOL="1")
    assert "admin_get_history" in await _tool_names(s)


async def test_http_never_exposes_ask_substack(server_factory):
    s = server_factory(CITED_TRANSPORT="http", ANTHROPIC_API_KEY="k")
    assert "ask_substack" not in await _tool_names(s)


def test_instructions_do_not_overclaim(server_factory):
    s = server_factory()
    text = s.mcp.instructions
    assert "FULL archive" not in text
    assert "every public post" not in text
    assert "BEFORE falling back" not in text


# ── search_substack end-to-end (mocked network + fake models) ────────────────

async def test_search_substack_end_to_end(server_factory, mock_http):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    out = json.loads(await s.search_substack("https://feeds.example/feed.xml", "pricing tiers", 3))
    assert out["posts_indexed"] == 1
    assert out["results"][0]["date"] == "2024-01-01"
    assert "coverage" in out
    rows = s._history.get()
    assert len(rows) == 1 and rows[0]["question"] == "pricing tiers"


async def test_search_runs_models_off_event_loop(server_factory, mock_http):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    FakeSentenceTransformer.threads.clear()
    await s.search_substack("https://feeds.example/feed.xml", "pricing")
    assert FakeSentenceTransformer.threads
    assert threading.get_ident() not in FakeSentenceTransformer.threads


async def test_search_substack_ssrf_error_message(server_factory, mock_http):
    s = server_factory(CITED_TRANSPORT="http")
    out = json.loads(await s.search_substack("http://169.254.169.254/latest/feed", "x"))
    assert "private or local" in out["error"]


async def test_search_bad_top_k(server_factory, mock_http):
    s = server_factory()
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    out = json.loads(await s.search_substack("https://feeds.example/feed.xml", "pricing", 0))
    assert len(out["results"]) == 1


async def test_http_mode_no_logging_without_admin_token(server_factory, mock_http):
    s = server_factory(CITED_TRANSPORT="http")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    await s.search_substack("https://feeds.example/feed.xml", "pricing")
    assert s._history.get() == []


async def test_http_mode_logs_with_token_but_no_memory_file(server_factory, mock_http, tmp_path):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=_rss_feed())
    await s.search_substack("https://feeds.example/feed.xml", "pricing")
    assert len(s._history.get()) == 1
    assert not (tmp_path / "memory").exists()


async def test_ask_substack_empty_feed(server_factory, mock_http):
    s = server_factory(ANTHROPIC_API_KEY="k")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(
        200, content=b"<rss><channel></channel></rss>")
    out = json.loads(await s.ask_substack("https://feeds.example/feed.xml", "q"))
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
    async with _client(s.build_http_app()) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert sorted(r.json()["tools"]) == ["list_posts", "search_substack"]


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
