"""Podcast transcript repositories (Growth Podcast format)."""
import json

import httpx
import pytest

import fetcher
import library
import pipeline
import transcripts as tx
from conftest import FAKE_DNS

REPO = "https://github.com/aakashg/growth-podcast-transcripts"
RAW = "https://raw.githubusercontent.com/aakashg/growth-podcast-transcripts/main/"

EP1 = """---
title: "The Ultimate Guide to Positioning"
date: "2024-08-19"
youtube_url: "https://www.youtube.com/watch?v=abc123"
---

# The Ultimate Guide to Positioning

**Anthony Pierri** (00:00:01)

Positioning is all about clarity, and clarity comes from specificity about who you serve.

**Aakash Gupta** (00:01:01)

Welcome to the Product Growth Podcast. Today we talk positioning and homepages.

<!-- sponsor -->
**Aakash Gupta** (00:05:34)

This episode is brought to you by Attio, the next generation CRM. Go to attio.com slash productgrowth.

<!-- sponsor -->
**Aakash Gupta** (00:06:44)

And you've written about two types of positioning?

**Anthony Pierri** (00:06:50)

Yes. Market category positioning and the homepage storefront. A homepage is a storefront, not a Wikipedia page. Today's episode is brought to you by June, the analytics tool. Try it free at june.so slash aakash for a discount. Anyway, people scan homepages rather than read them.
"""

EP2 = """---
title: "Pricing Masterclass"
---

**Speakers (not separated)** (00:00:02)

Usage-based pricing aligns cost with value for customers who grow over time.
"""


def index(word_counts=(100, 50), files=("episodes/ep1.md", "episodes/ep2.md")):
    files = tuple(files) + ("episodes/ep2.md",) * (2 - len(files))   # helper always builds 2 entries
    entries = [
        {"title": "The Ultimate Guide to Positioning", "date": "2024-08-19", "guests": "Anthony Pierri",
         "substack_url": "https://www.news.aakashg.com/p/positioning", "youtube_url": "https://www.youtube.com/watch?v=abc123",
         "word_count": word_counts[0], "file": files[0]},
        {"title": "Pricing Masterclass", "date": "2024-08-20", "guests": "Kyle Poyar",
         "substack_url": "https://www.news.aakashg.com/p/pricing", "youtube_url": "https://www.youtube.com/watch?v=def456",
         "word_count": word_counts[1], "file": files[1]},
    ]
    return entries


@pytest.fixture
def repo(mock_http, monkeypatch):
    monkeypatch.setitem(FAKE_DNS, "raw.githubusercontent.com", ["185.199.108.133"])

    def serve(entries=None, ep1=EP1):
        mock_http[RAW + "index.json"] = httpx.Response(200, json=entries if entries is not None else index())
        mock_http[RAW + "episodes/ep1.md"] = httpx.Response(200, text=ep1)
        mock_http[RAW + "episodes/ep2.md"] = httpx.Response(200, text=EP2)
    serve()
    return serve


def post(body, yt="https://www.youtube.com/watch?v=abc123"):
    return fetcher.Post(title="T", url="https://site/p/t", date="2024-01-01", text=body,
                        meta={"youtube_url": yt})


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_turns_skips_marked_sponsor_turns():
    turns = tx.parse_turns(EP1.split("---", 2)[2])
    # 00:05:34 was nothing but an ad read, so the scrubber removed it entirely;
    # 00:06:44 is marked by the repo and kept as a flagged turn (chunking skips it).
    assert [(t["speaker"], t["ts"], t["sponsor"]) for t in turns] == [
        ("Anthony Pierri", "00:00:01", False), ("Aakash Gupta", "00:01:01", False),
        ("Aakash Gupta", "00:06:44", True), ("Anthony Pierri", "00:06:50", False)]


def test_unmarked_mid_turn_ad_is_scrubbed():
    turn = tx.parse_turns(EP1.split("---", 2)[2])[-1]
    assert "brought to you by" not in turn["text"] and "june.so" not in turn["text"]
    assert turn["text"].startswith("Yes. Market category positioning")
    assert turn["text"].endswith("people scan homepages rather than read them.")


def test_scrub_without_call_to_action_removes_only_a_short_span():
    text = ("Great point. This episode is brought to you by Acme. Acme is great. Teams love it. "
            "Back to discovery: interview five customers a week. Then synthesise.")
    out = tx.scrub_ads(text)
    assert out.startswith("Great point.") and "interview five customers" in out and "Acme" not in out


def test_unseparated_speakers_are_labelled_generically():
    chunks = tx.chunk_episode(post(EP2))
    assert chunks[0].text.startswith("Speaker (00:00:02):")


def test_chunks_link_to_the_moment_and_exclude_sponsors():
    chunks = tx.chunk_episode(post(EP1), words=30)
    assert all("Attio" not in c.text for c in chunks)
    assert all("two types of positioning?" not in c.text for c in chunks)   # repo-marked turn skipped
    assert chunks[0].url == "https://www.youtube.com/watch?v=abc123&t=1s"
    assert any(c.url.endswith("&t=410s") for c in chunks)          # 00:06:50
    assert all("(" in c.text.split(":", 1)[0] for c in chunks)     # every passage names its speaker


def test_long_monologue_is_split():
    body = "**Guest** (00:10:00)\n\n" + "word " * 700
    chunks = tx.chunk_episode(post(body), words=250)
    assert len(chunks) == 3 and all(c.url.endswith("&t=600s") for c in chunks)


def test_timestamp_link_replaces_existing_t():
    assert tx.timestamp_link("https://www.youtube.com/watch?v=x&t=5s", 90) == "https://www.youtube.com/watch?v=x&t=90s"
    assert tx.timestamp_link("", 90) == ""


def test_raw_base():
    assert tx.raw_base(REPO) == RAW
    with pytest.raises(fetcher.NotSubstackError):
        tx.raw_base("https://gitlab.com/a/b")


# ── Library: build, incremental refresh ──────────────────────────────────────

@pytest.fixture
def lib(tmp_path):
    pipeline.prewarm()
    src = {"name": "The Growth Podcast", "url": REPO, "key": "gh:aakashg/growth-podcast-transcripts",
           "type": "transcripts", "kind": "podcast", "permission": True,
           "attribution": {"text": "The Growth Podcast with Aakash Gupta", "url": "https://www.news.aakashg.com"}}
    return library.Library(enabled=True, db_path=lambda: tmp_path / "l.db", sources=[src])


KEY = "gh:aakashg/growth-podcast-transcripts"


async def test_build_and_search_with_timestamps(lib, repo):
    r = await lib.refresh(KEY)
    assert r["posts"] == 2 and r["new_or_changed_posts"] == 2
    hits, n = lib.search(KEY, "homepage storefront positioning", 2)
    assert n == 2 and "&t=" in hits[0]["url"] and "Anthony Pierri" in hits[0]["text"]


async def test_refresh_downloads_only_new_or_edited_episodes(lib, repo, mock_http):
    await lib.refresh(KEY)
    mock_http["__seen__"].clear()
    r = await lib.refresh(KEY)                                         # nothing changed
    assert r["new_or_changed_posts"] == 0 and mock_http["__seen__"] == [RAW + "index.json"]

    repo(entries=index(word_counts=(101, 50)), ep1=EP1 + "\n**Guest** (00:50:00)\n\nA new closing thought.\n")
    mock_http["__seen__"].clear()
    r = await lib.refresh(KEY)                                         # episode 1 edited upstream
    assert r["new_or_changed_posts"] == 1
    assert mock_http["__seen__"] == [RAW + "index.json", RAW + "episodes/ep1.md"]


async def test_episode_removed_upstream_is_removed(lib, repo):
    await lib.refresh(KEY)
    repo(entries=index()[:1])
    r = await lib.refresh(KEY)
    assert r["removed_posts"] == 1 and r["posts"] == 1


def test_github_url_resolves_to_its_own_key(lib):
    assert lib.resolve(REPO + "/tree/main/episodes") == KEY
    assert lib.resolve("https://github.com/someone/else") is None


def test_permission_does_not_exempt_all_of_github(lib):
    assert "github.com" not in fetcher.PERMITTED_HOSTS


# ── Server: attribution and licence rules ────────────────────────────────────

async def test_server_results_carry_attribution(server_factory, tmp_path, monkeypatch, repo):
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [{
        "name": "The Growth Podcast (Aakash Gupta)", "type": "transcripts", "kind": "podcast", "url": REPO,
        "permission": "granted", "aliases": ["Growth Podcast", "Aakash"],
        "attribution": {"text": "The Growth Podcast with Aakash Gupta", "url": "https://www.news.aakashg.com"}}]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    monkeypatch.setenv("CITED_LIBRARY_DB", str(tmp_path / "library.db"))
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    await s._LIBRARY.refresh(KEY)

    out = json.loads(await s.search_library("What did guests on Aakash's podcast say about homepages?"))
    assert out["routing"]["searched"] == ["The Growth Podcast (Aakash Gupta)"]
    assert out["results"][0]["attribution"]["text"] == "The Growth Podcast with Aakash Gupta"

    out = json.loads(await s.search_newsletter(REPO, "usage-based pricing"))
    assert "error" not in out, out
    assert "transcripts of all 2 episodes" in out["coverage_note"]
    assert out["results"][0]["attribution"]["url"] == "https://www.news.aakashg.com"

    text = s.mcp.instructions
    assert "never speak as them or role-play" in text and "attribution" in text
