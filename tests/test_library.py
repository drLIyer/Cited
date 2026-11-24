import json

import httpx
import pytest

import fetcher
import library
import pipeline
from conftest import FAKE_DNS, FakeSentenceTransformer

BASE = "https://good.substack.com/api/v1/posts?limit=25"


def post(i, text=None, audience="everyone"):
    body = text or f"Post number {i} discusses pricing strategy, growth loops and hiring in depth. " * 8
    return {"title": f"P{i}", "slug": f"p{i}", "post_date": f"2024-01-{(i % 28) + 1:02d}T00:00:00Z",
            "body_html": f"<p>{body}</p>", "audience": audience}


@pytest.fixture
def lib(tmp_path, mock_http):
    pipeline.prewarm()
    sources = [{"name": "Good", "url": "https://good.substack.com", "key": "good"}]
    L = library.Library(enabled=True, db_path=lambda: tmp_path / "library.db", sources=sources,
                        pending=[{"name": "Someone", "note": ""}], max_chars=60_000)
    return L


def serve(mock_http, pages):
    """pages: list of lists of post dicts, served with cursors."""
    for n, page in enumerate(pages):
        url = BASE if n == 0 else f"{BASE}&after=c{n}"
        body = {"posts": page}
        if n + 1 < len(pages):
            body["nextCursor"] = f"c{n + 1}"
        mock_http[url] = httpx.Response(200, json=body)


# ── Config ───────────────────────────────────────────────────────────────────

def test_load_sources(tmp_path, monkeypatch):
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [
        {"name": "A", "url": "http://news.example/"}, {"name": "B", "url": None, "note": "tbd"},
        {"name": "C", "url": "https://amivora.substack.com/p/the-full-catalogue"}]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    ready, pending = library.load_sources()
    assert ready == [{"name": "A", "url": "https://news.example", "key": "news.example"},
                     {"name": "C", "url": "https://amivora.substack.com", "key": "amivora"}]
    assert pending == [{"name": "B", "note": "tbd"}]


def test_repo_library_json_is_valid():
    ready, pending = library.load_sources(library.DEFAULT_FILE)
    names = [s["name"] for s in ready] + [p["name"] for p in pending]
    assert "Lenny's Newsletter" in names and len(names) == 8
    urls = {s["name"]: s["url"] for s in ready}
    assert urls["Deborah Liu"] == "https://debliu.substack.com"
    assert urls["The Skip (Nikhyl Singhal)"] == "https://theskip.substack.com"
    assert all(s["url"].startswith("https://") for s in ready)


def test_resolve_ignores_www_and_paths(tmp_path, monkeypatch):
    L = library.Library(enabled=True, db_path=lambda: tmp_path / "l.db",
                        sources=[{"name": "N", "url": "https://www.news.example", "key": "www.news.example"}])
    assert L.resolve("https://www.news.example/p/some-post") == "www.news.example"
    assert L.resolve("https://news.example") == "www.news.example"      # bare domain → www source
    assert L.resolve("http://www.news.example/") == "www.news.example"  # http upgraded, slash ignored
    assert L.resolve("https://other.example") is None


# ── Build, incremental refresh, persistence ──────────────────────────────────

async def test_full_build_skips_paid_posts(lib, mock_http):
    serve(mock_http, [[post(1), post(2, audience="only_paid"), post(3)], [post(4)]])
    r = await lib.refresh("good")
    assert r["posts"] == 3 and r["new_or_changed_posts"] == 3 and r["full"]
    assert lib.ready("good")
    hits, n = lib.search("good", "pricing strategy", 3)
    assert n == 3 and hits and {"similarity", "url", "text"} <= set(hits[0])
    assert all(h["url"] != "https://good.substack.com/p/p2" for h in hits)


async def test_incremental_refresh_embeds_only_new_and_stops_paging(lib, mock_http):
    serve(mock_http, [[post(i) for i in range(1, 26)], [post(i) for i in range(26, 51)],
                      [post(i) for i in range(51, 60)]])
    await lib.refresh("good")
    mock_http["__seen__"].clear()
    before = FakeSentenceTransformer.encode_calls

    # One new post at the top pushes everything down by one.
    serve(mock_http, [[post(100)] + [post(i) for i in range(1, 25)], [post(i) for i in range(25, 50)],
                      [post(i) for i in range(50, 60)]])
    r = await lib.refresh("good")
    assert r["new_or_changed_posts"] == 1 and r["posts"] == 60
    assert FakeSentenceTransformer.encode_calls - before == 1          # one batch: just the new post
    # Page 1 had something new, so page 2 was checked; page 2 was all known, so paging stopped.
    assert mock_http["__seen__"] == [BASE, f"{BASE}&after=c1"]


async def test_paging_stops_at_first_fully_known_page(lib, mock_http):
    serve(mock_http, [[post(i) for i in range(1, 26)], [post(i) for i in range(26, 51)], [post(i) for i in range(51, 60)]])
    await lib.refresh("good")
    mock_http["__seen__"].clear()
    r = await lib.refresh("good")                                     # nothing new
    assert r["new_or_changed_posts"] == 0
    assert mock_http["__seen__"] == [BASE]                            # stopped after page 1


async def test_edited_post_is_reembedded(lib, mock_http):
    serve(mock_http, [[post(1), post(2)]])
    await lib.refresh("good")
    serve(mock_http, [[post(1, text="Completely rewritten: onboarding emails and activation metrics. " * 8), post(2)]])
    r = await lib.refresh("good")
    assert r["new_or_changed_posts"] == 1 and r["posts"] == 2
    hits, _ = lib.search("good", "onboarding emails activation", 1)
    assert "rewritten" in hits[0]["text"]


async def test_weekly_full_rescan_removes_deleted_posts(lib, mock_http, monkeypatch):
    serve(mock_http, [[post(1), post(2), post(3)]])
    await lib.refresh("good")
    serve(mock_http, [[post(1), post(3)]])
    r = await lib.refresh("good", full=True)
    assert r["removed_posts"] == 1 and r["posts"] == 2


async def test_model_change_triggers_rebuild(lib, mock_http, monkeypatch):
    serve(mock_http, [[post(1), post(2)]])
    await lib.refresh("good")
    monkeypatch.setattr(pipeline, "EMBED_MODEL_NAME", "some-new-model")
    r = await lib.refresh("good")
    assert r["full"] and r["new_or_changed_posts"] == 2


async def test_index_survives_restart_without_network(lib, mock_http, tmp_path):
    serve(mock_http, [[post(1), post(2)]])
    await lib.refresh("good")
    mock_http.clear(); mock_http["__seen__"] = []
    fresh = library.Library(enabled=True, db_path=lambda: tmp_path / "library.db", sources=lib.sources.values())
    fresh.sources = lib.sources
    fresh.load_all()
    assert fresh.ready("good")
    hits, n = fresh.search("good", "growth loops", 2)
    assert n == 2 and hits


async def test_refresh_error_is_reported_and_old_index_kept(lib, mock_http):
    serve(mock_http, [[post(1)]])
    await lib.refresh("good")
    mock_http[BASE] = httpx.Response(503)
    r = await lib.refresh("good")
    assert "error" in r and lib.ready("good")
    st = lib.status()["sources"][0]
    assert st["state"] == "stale" and "503" in st["error"]


async def test_rss_library_keeps_posts_that_scroll_off_the_feed(tmp_path, mock_http):
    pipeline.prewarm()
    url = "https://feeds.example/feed.xml"
    L = library.Library(enabled=True, db_path=lambda: tmp_path / "l.db",
                        sources=[{"name": "F", "url": url, "key": fetcher.parse_subdomain(url)}])
    key = fetcher.parse_subdomain(url)
    item = lambda i: (f"<item><title>E{i}</title><link>https://x/{i}</link><description>"
                      + f"Episode {i} about product strategy and roadmaps. " * 6 + "</description></item>")
    mock_http[url] = httpx.Response(200, content=f"<rss><channel>{item(1)}{item(2)}</channel></rss>".encode())
    await L.refresh(key)
    mock_http[url] = httpx.Response(200, content=f"<rss><channel>{item(3)}{item(2)}</channel></rss>".encode())
    r = await L.refresh(key, full=True)
    assert r["posts"] == 3 and r["removed_posts"] == 0     # E1 kept even though the feed dropped it


# ── Server integration ───────────────────────────────────────────────────────

@pytest.fixture
def lib_server(server_factory, tmp_path, monkeypatch):
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [{"name": "Good", "url": "https://good.substack.com"},
                                         {"name": "Later", "url": None}]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    monkeypatch.setenv("CITED_LIBRARY_DB", str(tmp_path / "library.db"))
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    return s


async def test_search_uses_library_without_fetching(lib_server, mock_http):
    serve(mock_http, [[post(1), post(2), post(3)]])
    await lib_server._LIBRARY.refresh("good")
    mock_http["__seen__"].clear()
    out = json.loads(await lib_server.search_newsletter("https://good.substack.com/p/anything", "pricing"))
    assert mock_http["__seen__"] == []
    assert out["posts_indexed"] == 3 and "full archive of 3 free posts" in out["coverage_note"]
    listing = await lib_server.list_posts("https://good.substack.com")
    assert "3 free posts in Cited's archive" in listing and mock_http["__seen__"] == []
    assert "Good (https://good.substack.com)" in lib_server.mcp.instructions


async def test_unbuilt_library_source_falls_back_to_live_fetch(lib_server, mock_http):
    serve(mock_http, [[post(1)]])
    out = json.loads(await lib_server.search_newsletter("https://good.substack.com", "pricing"))
    assert out["posts_indexed"] == 1 and "up to 200" in out["coverage_note"]


async def test_library_in_dashboard_health_and_refresh_api(lib_server, mock_http):
    serve(mock_http, [[post(1)]])
    await lib_server._LIBRARY.refresh("good")
    app = lib_server.build_http_app()
    auth = {"Authorization": "Bearer s3cret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        o = (await c.get("/admin/api/overview", headers=auth)).json()
        assert o["library"]["sources"][0]["state"] == "ready"
        assert o["library"]["pending"] == [{"name": "Later", "note": ""}]
        assert o["health"]["checks"]["library"]["status"] == "ok"
        r = await c.post("/admin/api/library/refresh", json={"key": "good"}, headers=auth)
        assert r.status_code == 202
        assert (await c.post("/admin/api/library/refresh", json={"key": "nope"}, headers=auth)).status_code == 404


def test_library_disabled_in_local_mode(server_factory):
    s = server_factory()
    assert s._LIBRARY.enabled is False
