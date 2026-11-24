"""Routing decision model: which library newsletters a question searches, and cross-library search."""
import json
from types import SimpleNamespace

import httpx
import pytest

import library
import pipeline
from conftest import FAKE_DNS

PRICING = "Pricing strategy: charge for value, packaging tiers, annual discounts and usage-based billing. "
HIRING = "Hiring product managers: structured interviews, work samples, references and onboarding plans. "


def substack(mock_http, host, nums, text):
    posts = [{"title": f"{host} post {n}", "slug": f"p{n}", "post_date": f"2024-02-{n + 1:02d}T00:00:00Z",
              "body_html": f"<p>{text * 6}</p>", "audience": "everyone"} for n in nums]
    mock_http[f"https://{host}/api/v1/posts?limit=25"] = httpx.Response(200, json={"posts": posts})


@pytest.fixture
def lib_files(tmp_path, monkeypatch, mock_http):
    monkeypatch.setitem(FAKE_DNS, "other.substack.com", ["104.18.0.2"])
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [
        {"name": "Pricing Weekly (Pat Price)", "url": "https://good.substack.com", "aliases": ["Pat Price", "Pat"]},
        {"name": "The Hiring Desk", "url": "https://other.substack.com", "aliases": ["Hannah Hire"]},
        {"name": "Not Built Yet", "url": "https://news.example", "aliases": ["Nora"]},
    ]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    monkeypatch.setenv("CITED_LIBRARY_DB", str(tmp_path / "library.db"))
    substack(mock_http, "good.substack.com", range(3), PRICING)
    substack(mock_http, "other.substack.com", range(3), HIRING)


@pytest.fixture
async def srv(lib_files, server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    await s._LIBRARY.refresh("good")
    await s._LIBRARY.refresh("other")
    return s


# ── The routing decision ─────────────────────────────────────────────────────

async def test_no_names_searches_whole_ready_library(srv):
    r = srv._LIBRARY.route("How should I structure interviews?")
    assert set(r["keys"]) == {"good", "other"} and "all 2 ready" in r["reason"]


async def test_names_in_question_restrict_the_search(srv):
    r = srv._LIBRARY.route("What does Hannah Hire say about onboarding?")
    assert r["keys"] == ["other"] and "The Hiring Desk" in r["reason"]
    r = srv._LIBRARY.route("Compare Pat Price and The Hiring Desk on annual plans")
    assert set(r["keys"]) == {"good", "other"}


async def test_alias_matching_is_whole_word(srv):
    assert srv._LIBRARY.mentioned_in("Patience is key when hiring") == []
    assert srv._LIBRARY.mentioned_in("what would pat say?") == ["good"]


async def test_requested_by_name_alias_or_url(srv):
    r = srv._LIBRARY.route("anything", ["pricing weekly", "https://other.substack.com/p/x", "Nobody", "Nora"])
    assert r["keys"] == ["good", "other"]
    assert r["not_found"] == ["Nobody"] and r["not_ready"] == ["Not Built Yet"]


async def test_named_but_not_built_is_reported(srv):
    r = srv._LIBRARY.route("What does Nora think?")
    assert r["keys"] == [] and r["not_ready"] == ["Not Built Yet"]


def test_real_library_aliases_route_correctly(monkeypatch, tmp_path):
    monkeypatch.delenv("CITED_LIBRARY_FILE", raising=False)
    ready, pending = library.load_sources(library.DEFAULT_FILE)
    L = library.Library(enabled=True, db_path=lambda: tmp_path / "l.db", sources=ready, pending=pending)
    name = lambda q: [L.sources[k]["name"] for k in L.mentioned_in(q)]
    assert name("What does Marty Cagan say about empowered teams?") == ["SVPG (Marty Cagan)"]
    assert name("Compare Lenny and Teresa Torres on discovery") == ["Lenny's Newsletter", "Product Talk (Teresa Torres)"]
    assert name("Deb Liu on leadership") == ["Deborah Liu"]
    assert name("What does the skip say about careers") == ["The Skip (Nikhyl Singhal)"]
    assert L.match_name("productcompass.pm") == "www.productcompass.pm"


# ── Cross-library search ─────────────────────────────────────────────────────

async def test_best_newsletter_ranks_first(srv):
    hits, coverage = srv._LIBRARY.search_many(["good", "other"], "structured interviews work samples", 4)
    assert hits[0]["source_name"] == "The Hiring Desk"
    assert coverage[0]["name"] == "The Hiring Desk"
    assert {c["name"] for c in coverage} == {"The Hiring Desk", "Pricing Weekly (Pat Price)"}


async def test_search_library_tool_output(srv):
    out = json.loads(await srv.search_library("annual discounts and usage-based billing"))
    assert out["routing"]["searched"] == ["Pricing Weekly (Pat Price)", "The Hiring Desk"]
    assert "No newsletter named" in out["routing"]["reason"]
    assert out["results"][0]["newsletter"] == "Pricing Weekly (Pat Price)"
    assert out["coverage_by_newsletter"][0]["newsletter"] == "Pricing Weekly (Pat Price)"
    assert out["confidence"]["display"].endswith("%)")
    row = srv._history.get()[0]
    assert row["question"] == "annual discounts and usage-based billing" and row["label"].startswith("library://")


async def test_search_library_restricted_and_unknown(srv):
    out = json.loads(await srv.search_library("pricing", newsletters=["Hannah Hire"]))
    assert out["routing"]["searched"] == ["The Hiring Desk"]
    out = json.loads(await srv.search_library("pricing", newsletters=["Nobody"]))
    assert "None of the requested newsletters" in out["error"] and "The Hiring Desk" in out["library"]


async def test_search_library_low_coverage_disclaimer(srv, monkeypatch):
    monkeypatch.setattr(srv._pipeline, "coverage", lambda hits: {
        "level": "low", "label": "Limited coverage — draws on general AI knowledge", "color": "#e74c3c",
        "top_similarity": 0.2, "avg_similarity": 0.1, "percent": 20})
    out = json.loads(await srv.search_library("quantum computing"))
    assert out["disclaimer"].startswith("⚠️ Limited coverage: The Cited library")


async def test_search_library_rate_limited(srv):
    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"), headers={"x-forwarded-for": "203.0.113.9"})
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=req))
    srv._RATE.block("203.0.113.9", 60)
    out = json.loads(await srv.search_library("pricing", ctx=ctx))
    assert out["rate_limited"] is True


async def test_list_library(srv):
    out = json.loads(await srv.list_library())
    by = {n["name"]: n for n in out["newsletters"]}
    assert by["The Hiring Desk"]["free_posts"] == 3 and by["The Hiring Desk"]["state"] == "ready"
    assert by["Not Built Yet"]["state"] in ("building", "error")
    assert "Hannah Hire" in by["The Hiring Desk"]["also_known_as"]


async def test_instructions_explain_routing(srv):
    text = srv.mcp.instructions
    assert "## Choosing a tool" in text and "search_library" in text and "list_library" in text
