"""Respecting writers' AI opt-outs: robots.txt, noai, TDM reservation."""
import json

import httpx
import pytest

import fetcher
import library
import optout
import pipeline

SUB = "https://good.substack.com"


@pytest.fixture(autouse=True)
def check_on(monkeypatch):
    monkeypatch.setenv("CITED_RESPECT_AI_OPTOUT", "1")
    monkeypatch.delenv("CITED_OPTOUT_POLICY", raising=False)


def ev(text, **kw):
    return optout.evaluate(text, **kw)


# ── robots.txt rules ─────────────────────────────────────────────────────────

SUBSTACK_STYLE = """
User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /
"""


def test_training_crawler_block_is_opt_out_under_strict():
    d = ev(SUBSTACK_STYLE)
    assert not d.allowed and "GPTBot" in d.reason


def test_retrieval_policy_allows_training_only_blocks():
    assert ev(SUBSTACK_STYLE, pol="retrieval").allowed


def test_assistant_crawler_block_is_opt_out_under_both():
    text = "User-agent: ChatGPT-User\nDisallow: /\n"
    assert not ev(text).allowed and not ev(text, pol="retrieval").allowed


def test_block_everyone_and_block_cited():
    assert "all crawlers" in ev("User-agent: *\nDisallow: /\n").reason
    d = ev("User-agent: Cited\nDisallow: /\n")
    assert not d.allowed and "blocks Cited" in d.reason


def test_ordinary_robots_is_fine():
    text = "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n\nSitemap: https://x/s.xml\n"
    assert ev(text).allowed


def test_multi_agent_group_comments_and_case():
    text = "user-AGENT: gptbot   # OpenAI\nUser-Agent: Claude-Web\nDISALLOW: /\n"
    d = ev(text)
    assert not d.allowed and set(d.blocked_agents) >= {"GPTBot", "Claude-Web"}


def test_partial_block_of_unrelated_paths_is_not_opt_out():
    assert ev("User-agent: GPTBot\nDisallow: /private/\n").allowed


def test_longest_match_and_allow_wins_ties():
    groups, _ = optout.parse("User-agent: x\nDisallow: /p/\nAllow: /p/public\nDisallow: /a\nAllow: /a\n")
    assert not optout.is_allowed(groups, "x", "/p/secret")
    assert optout.is_allowed(groups, "x", "/p/public-post")
    assert optout.is_allowed(groups, "x", "/a")                      # tie → Allow


def test_wildcards_and_feed_path():
    text = "User-agent: Cited\nDisallow: /*.xml$\n"
    assert ev(text).allowed
    assert not ev(text, feed_path="/podcast.xml").allowed


def test_content_signals():
    assert not ev("Content-Signal: ai-input=no, search=yes\n").allowed
    assert not ev("Content-Signal: ai-train=no\n").allowed                       # strict
    assert ev("Content-Signal: ai-train=no\n", pol="retrieval").allowed
    assert ev("Content-Signal: ai-input=yes, ai-train=no\n" + SUBSTACK_STYLE).allowed   # explicit yes


# ── noai and TDM reservation ─────────────────────────────────────────────────

def test_page_signals():
    assert optout.evaluate_page({}, '<meta name="robots" content="index, noai">')
    assert optout.evaluate_page({"X-Robots-Tag": "noimageai"}, "")
    assert optout.evaluate_page({}, "<meta name='tdm-reservation' content='1'>")
    assert optout.evaluate_page({}, '<meta name="robots" content="index, follow">') is None


def test_tdmrep_json():
    assert optout.evaluate_tdmrep([{"location": "/", "tdm-reservation": 1}])
    assert optout.evaluate_tdmrep([{"location": "/private/*", "tdm-reservation": 1}]) is None
    assert optout.evaluate_tdmrep({"nope": 1}) is None


# ── Fetching ─────────────────────────────────────────────────────────────────

async def test_missing_robots_means_allowed_and_is_cached(mock_http):
    d = await optout.check(SUB)
    assert d.allowed and "no robots.txt" in d.reason
    n = len(mock_http["__seen__"])
    await optout.check(SUB)
    assert len(mock_http["__seen__"]) == n                                      # cached


async def test_noai_meta_on_home_page(mock_http):
    mock_http[f"{SUB}/"] = httpx.Response(200, content=b'<html><head><meta name="robots" content="noai"></head></html>')
    assert not (await optout.check(SUB)).allowed


async def test_unreadable_robots_is_unknown_not_allowed(mock_http):
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(503)
    with pytest.raises(optout.OptOutCheckError):
        await optout.check(SUB)
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(403)
    optout.clear_cache()
    with pytest.raises(optout.OptOutCheckError):
        await optout.check(SUB)


async def test_disabled_by_env(monkeypatch, mock_http):
    monkeypatch.setenv("CITED_RESPECT_AI_OPTOUT", "0")
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    mock_http[f"{SUB}/api/v1/posts?limit=25"] = httpx.Response(200, json={"posts": []})
    await fetcher.get_posts(SUB)                                                # no error


# ── Live searches ────────────────────────────────────────────────────────────

async def test_live_search_of_opted_out_site_is_declined(server_factory, mock_http):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(200, text=SUBSTACK_STYLE)
    out = json.loads(await s.search_newsletter(SUB, "pricing"))
    assert "opted out of AI tools" in out["error"]
    assert not any("api/v1/posts" in u for u in mock_http["__seen__"])          # content never fetched
    assert [g["kind"] for g in s._events.groups(category="usage")] == ["opted_out"]


# ── Library ──────────────────────────────────────────────────────────────────

def post(i):
    return {"title": f"P{i}", "slug": f"p{i}", "post_date": "2024-01-0%dT00:00:00Z" % (i + 1),
            "body_html": "<p>" + "Pricing strategy and growth loops for SaaS. " * 10 + "</p>", "audience": "everyone"}


@pytest.fixture
def lib(tmp_path, mock_http):
    pipeline.prewarm()
    mock_http[f"{SUB}/api/v1/posts?limit=25"] = httpx.Response(200, json={"posts": [post(1), post(2)]})
    return library.Library(enabled=True, db_path=lambda: tmp_path / "library.db",
                           sources=[{"name": "Good", "url": SUB, "key": "good", "aliases": ["Goodie"]}])


async def test_library_opt_out_deletes_stored_posts(lib, mock_http):
    await lib.refresh("good")
    assert lib.ready("good")
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(200, text=SUBSTACK_STYLE)     # writer opts out later
    optout.clear_cache()
    r = await lib.refresh("good")
    assert "GPTBot" in r["opted_out"] and not lib.ready("good")
    st = lib.status()["sources"][0]
    assert st["state"] == "opted_out" and st["posts"] == 0 and st["error"] is None
    import sqlite3
    db = sqlite3.connect(lib._db_path())
    assert db.execute("SELECT COUNT(*) FROM lib_chunks").fetchone()[0] == 0
    route = lib.route("What does Goodie say?")
    assert route["opted_out"] == ["Good"] and route["keys"] == []


async def test_library_permission_overrides_opt_out(tmp_path, mock_http):
    pipeline.prewarm()
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(200, text=SUBSTACK_STYLE)
    mock_http[f"{SUB}/api/v1/posts?limit=25"] = httpx.Response(200, json={"posts": [post(1)]})
    L = library.Library(enabled=True, db_path=lambda: tmp_path / "l.db",
                        sources=[{"name": "Good", "url": SUB, "key": "good", "permission": True}])
    r = await L.refresh("good")
    assert r["posts"] == 1 and L.status()["sources"][0]["permission"] is True
    assert (await fetcher.get_posts(SUB))                                       # live fetch allowed too


async def test_library_unreadable_robots_does_not_index(lib, mock_http):
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(503)
    r = await lib.refresh("good")
    assert "error" in r and not lib.ready("good")
    assert lib.status()["sources"][0]["state"] == "error"


async def test_search_library_explains_opt_out(server_factory, tmp_path, monkeypatch, mock_http):
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [{"name": "Good", "url": SUB, "aliases": ["Goodie"]}]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    monkeypatch.setenv("CITED_LIBRARY_DB", str(tmp_path / "library.db"))
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    mock_http[f"{SUB}/robots.txt"] = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    await s._LIBRARY.refresh("good")
    out = json.loads(await s.search_library("What does Goodie think about pricing?"))
    assert "opted out of AI tools" in out["error"]
