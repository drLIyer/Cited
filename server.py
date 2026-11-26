#!/usr/bin/env python3
"""
Cited — Precise, sourced answers from any Substack newsletter via MCP.

Exposes three tools to any MCP-capable Claude client
(Claude Desktop, Claude Code, Claude Cowork):

  search_newsletter   — hybrid search, returns ranked excerpts for Claude to cite
  list_posts        — list all public post titles + dates from a newsletter
  ask_newsletter      — full RAG + server-side answer via Haiku (needs API key)

Usage — Claude Code:

  claude mcp add cited -- /path/to/cited/.venv/bin/python3 /path/to/cited/server.py

Usage — Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):

  "mcpServers": {
    "cited": {
      "command": "/path/to/cited/.venv/bin/python3",
      "args": ["/path/to/cited/server.py"]
    }
  }

Env vars:
  ANTHROPIC_API_KEY      — enables ask_newsletter tool (local/stdio mode only)
  CITED_TRANSPORT        — "stdio" (default) or "http" (hosted)
  CITED_ADMIN_TOKEN      — HTTP mode: enables GET /admin/history (Bearer auth)
                           and query logging
  CITED_LOG_QUERIES      — "1"/"0" to force query logging on/off
                           (default: on in stdio; in HTTP only if CITED_ADMIN_TOKEN is set)
  CITED_ADMIN_MCP_TOOL   — "1" to also expose admin_get_history as an MCP tool
                           (off by default: the token would appear in chat transcripts)
  CITED_HISTORY_DB       — path to the SQLite history file
  CITED_HISTORY_RETENTION_DAYS — delete history older than N days (default 90; 0 = keep)
  CITED_ALLOW_PRIVATE_URLS — "1" to allow fetching private/local addresses (dev only)
  CITED_USE_PROXY        — "1" to honor HTTPS_PROXY (disables connect-time DNS pinning)
  CITED_RATE_LIMIT_PER_MIN — HTTP mode: searches per minute per client IP (default 15; 0 = off)
  CITED_TRUSTED_PROXY_HOPS — proxies in front of the app adding X-Forwarded-For (default 1)
  CITED_RATE_LIMIT_SHARED_CIDRS / CITED_RATE_LIMIT_SHARED_PER_MIN — higher limit for
                           shared egress IPs (see ratelimit.py)
  Health: GET /health/live, /health/ready, /health (see health.py)

Only https:// sources are fetched; pasted http:// URLs are upgraded to https://.
"""

import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations


def _read_only(title: str, open_world: bool = True) -> ToolAnnotations:
    """Annotations for Cited's tools: all of them only read; none modify anything."""
    return ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False,
                           idempotent_hint=True, open_world_hint=open_world)

import fetcher as _fetcher
import pipeline as _pipeline
import history as _history
import health as _health
import events as _events
import healer as _healer_mod
import digest as _digest
import metrics as _metrics
import library as _library_mod
from ratelimit import ClientRateLimit

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("cited")

_HTTP_MODE   = os.getenv("CITED_TRANSPORT", "stdio") == "http"
_ADMIN_TOKEN = os.getenv("CITED_ADMIN_TOKEN", "")
_ADMIN_MCP_TOOL = os.getenv("CITED_ADMIN_MCP_TOOL") == "1"

# Query logging: always on locally (it's your own machine). In hosted mode,
# only log when an admin token exists — otherwise nobody can read the log and
# we'd be storing every user's questions for no reason.
_log_env = os.getenv("CITED_LOG_QUERIES")
_LOG_QUERIES = (_log_env == "1") if _log_env in ("0", "1") else (
    True if not _HTTP_MODE else bool(_ADMIN_TOKEN))

# Error/event log for the admin dashboard: on whenever an admin can read it.
_events.configure(enabled=_HTTP_MODE and bool(_ADMIN_TOKEN),
                  db_path=lambda: _history.DB_PATH,
                  retention_days=int(os.getenv("CITED_EVENT_RETENTION_DAYS", "30")))
if _HTTP_MODE:
    _events.install_capture()

_metrics.configure(enabled=_events.enabled(), db_path=lambda: _history.DB_PATH)

# Per-IP rate limit for searches in hosted mode (local stdio is never limited).
_RATE = ClientRateLimit.from_env()

# Auto-fix agent: safe, verified, remembered fixes (see healer.py).
_HEALER = _healer_mod.Healer(
    enabled=_events.enabled() and os.getenv("CITED_AUTOHEAL", "1") != "0",
    db_path=lambda: _history.DB_PATH,
    block_ip=_RATE.block,
)

# Library: permanent, incrementally-updated full-archive indexes (see library.py).
_lib_sources, _lib_pending = _library_mod.load_sources()
_LIBRARY = _library_mod.Library(
    enabled=_HTTP_MODE and os.getenv("CITED_LIBRARY", "1") != "0",
    db_path=lambda: Path(os.getenv("CITED_LIBRARY_DB") or (_history.DB_PATH.parent / "library.db")),
    sources=_lib_sources, pending=_lib_pending,
    max_chars=int(os.getenv("CITED_LIBRARY_POST_CHARS", "60000")),
)
_LIBRARY_NOTE = (
    "## The Cited library\n"
    "Full archives of free posts are pre-indexed (fast, complete) for: "
    + ", ".join(f"{s['name']} ({s['url']})" for s in _lib_sources) + ".\n\n"
    "## Choosing a tool\n"
    "- A question about a topic without a specific source (e.g. 'how do teams run product "
    "discovery?'), or one that names library writers or newsletters (e.g. 'what does Marty "
    "Cagan say about…', 'compare Lenny and Teresa Torres on…') → call search_library. Pass "
    "`newsletters` to restrict it; otherwise it routes by the names in the question, or searches "
    "the whole library. It reports which newsletters cover the topic best — tell the user.\n"
    "- A specific newsletter or feed URL that is not in the library → call search_newsletter.\n"
    "- 'Which newsletters can you search?' → call list_library.\n\n"
) if _LIBRARY.enabled and _lib_sources else ""

mcp = MCPServer(
    "Cited",
    instructions=(
        "Cited searches the public posts of a Substack, Medium, or RSS/podcast feed "
        "and returns ranked excerpts with titles, dates, and links. "
        f"For Substack it indexes up to the {_fetcher.MAX_POSTS} most recent public posts; "
        "for Medium and RSS/podcast feeds it indexes whatever the feed publishes "
        "(often only the most recent 10–50 items). "
        f"Each post is indexed up to its first {_fetcher.POST_CHARS:,} characters. "
        "Paywalled content is not included.\n\n"
        + _LIBRARY_NOTE +

        "## When to use it\n"
        "Use search_newsletter when the user asks what a specific newsletter, author, "
        "or podcast has said about a topic. It searches the source's own text, which "
        "can surface posts that general web search ranks poorly.\n\n"

        "## When the user gives a URL\n"
        "Call search_newsletter directly with that URL and their question. "
        "It accepts Substack URLs, Medium URLs, and any RSS or podcast feed URL.\n\n"

        "## When the user gives only an author or podcast name (no URL)\n"
        "1. Use your web search to find the feed URL only — search for: "
        '"{name} substack", "{name} newsletter", or "{name} podcast RSS feed". '
        "Pick the most likely result — prefer a direct feed URL (/feed, /rss, .xml), "
        "a custom domain, or a substack.com URL over social media pages.\n"
        "2. Tell the user which URL you found, then call search_newsletter with it.\n\n"

        "## When the user gives a podcast name\n"
        "Search for '{podcast name} RSS feed' to find the feed URL, "
        "then call search_newsletter with it. Show notes and episode summaries are indexed.\n\n"

        "## Composing answers\n"
        "Always cite the article or episode title and link for every claim. "
        "If a result includes `attribution`, show it with the quote (e.g. 'From The Growth Podcast "
        "with Aakash Gupta — news.aakashg.com'). Podcast excerpts are labelled 'Speaker (hh:mm:ss)' "
        "and their links open at that moment; attribute each quote to the person who said it. "
        "Quote people — never speak as them or role-play a host or guest — and never suggest a "
        "guest endorses a product. "
        "Every search_newsletter result has a `confidence` with a level (High / Moderate / "
        "Limited coverage) and a best-match percentage. Tell the user the confidence in one "
        "short line, e.g. 'Confidence: High coverage (best match 71%)'. "
        "If the result is Limited coverage it includes a `disclaimer`: show that disclaimer "
        "to the user word for word at the top of your answer. If you then add anything from "
        "your general knowledge or web search, label it clearly as not coming from the "
        "newsletter, and keep newsletter-backed statements cited. "
        "If the source is paywalled, returns few results, or the topic may predate the indexed "
        "posts, say so honestly."
    ),
)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _coverage_note(n_posts: int, library_updated: Optional[float] = None, kind: str = "newsletter") -> str:
    if library_updated is not None:
        mins = max(1, round((time.time() - library_updated) / 60))
        if kind == "podcast":
            return (f"Searched full transcripts of all {n_posts} episodes (Cited's pre-built index, "
                    f"refreshed {mins} min ago). Links open the episode at the quoted moment.")
        return (f"Searched the full archive of {n_posts} free posts (Cited's pre-built index, "
                f"refreshed {mins} min ago). Paid-only posts are not included.")
    return (f"Searched {n_posts} public posts/episodes (Substack: up to {_fetcher.MAX_POSTS} "
            f"most recent; feeds: whatever the feed publishes). Each post is indexed up to "
            f"its first {_fetcher.POST_CHARS:,} characters.")


# Coverage levels of the most recent searches, for the low-confidence alert
# (same rule as ThinkWithMe's health agent: >50% of the last 20 are Limited).
_LOW_CONF_WINDOW = int(os.getenv("CITED_LOW_CONF_WINDOW", "20"))
_LOW_CONF_ALERT  = float(os.getenv("CITED_LOW_CONF_ALERT", "0.5"))
_recent_levels: deque = deque(maxlen=_LOW_CONF_WINDOW)


async def _retrieve(url: str, query: str, top_k: int):
    """Search a source. Library newsletters use their stored full-archive index;
    anything else is fetched (async I/O) and searched in a worker thread so
    CPU-bound model inference doesn't block the event loop.
    Returns (n_posts, hits, coverage, library_updated_at_or_None)."""
    lib_key = _LIBRARY.resolve(url)
    if lib_key and _LIBRARY.ready(lib_key):
        # A library match is one of our configured sources, searched from the stored
        # index — no network access, so no URL/DNS checks are needed here.
        hits, n = await asyncio.to_thread(_LIBRARY.search, lib_key, query, top_k)
        cov = _pipeline.coverage(hits)
        _recent_levels.append(cov["level"])
        src = _LIBRARY.sources[lib_key]
        if src.get("attribution"):
            for h in hits:
                h["attribution"] = src["attribution"]
        return n, hits, cov, _LIBRARY.updated_at(lib_key)

    posts = await _fetcher.get_posts(url)
    if not posts:
        return 0, [], _pipeline.coverage([]), None

    def _work():
        chunks = _pipeline.chunk_posts(posts)
        key    = _fetcher.parse_subdomain(url)
        hits   = _pipeline.search(query, chunks, top_k=top_k, cache_key=key)
        return hits, _pipeline.coverage(hits)

    hits, cov = await asyncio.to_thread(_work)
    _recent_levels.append(cov["level"])
    return len(posts), hits, cov, None


def _low_coverage_disclaimer(source: str, cov: dict) -> str:
    return (f"⚠️ Limited coverage: {source} has little on this topic "
            f"(best match {cov['percent']}%). This answer draws on general AI knowledge "
            "and web search rather than the newsletter itself — verify key claims independently.")


def _source_name(url: str) -> str:
    try:
        return _fetcher.display_name(_fetcher.parse_subdomain(url))
    except Exception:
        return "This source"


def _confidence_payload(cov: dict) -> dict:
    return {
        "level":         cov["level"],
        "label":         cov["label"],
        "percent":       cov["percent"],
        "best_match":    cov["top_similarity"],
        "average_match": cov["avg_similarity"],
        "display":       f"{cov['label'].split(' —')[0]} (best match {cov['percent']}%)",
    }


def _tool_problem(tool: str, url: str, exc: Exception, ip: Optional[str] = None) -> None:
    """Record an expected tool failure (bad URL, unreachable feed) in the error log."""
    if isinstance(exc, _fetcher.UnsafeURLError):
        kind, level = "blocked_url", "WARNING"
    elif isinstance(exc, _fetcher.UnsafeContentError):
        kind, level = "blocked_content", "WARNING"
    elif isinstance(exc, _fetcher.SourcePausedError):
        kind, level = "source_paused", "INFO"
    elif isinstance(exc, _fetcher.OptedOutError):
        kind, level = "opted_out", "INFO"
    elif isinstance(exc, _fetcher.NotSubstackError):
        kind, level = "invalid_source", "INFO"
    else:
        kind, level = "fetch_failed", "WARNING"
    if kind in ("blocked_url", "blocked_content"):
        _HEALER.note_offense(ip, kind)
    _events.record(kind, str(exc), level=level, source=tool,
                   context={"tool": tool, "url": url[:500]},
                   group_key=f"{kind}|{_fetcher._host(url)}|{type(exc).__name__}")


def _caller_ip(ctx: Optional[Context]) -> Optional[str]:
    """Client IP of the MCP request (hosted mode only)."""
    if not _HTTP_MODE or ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except Exception:
        return None
    if request is None:
        return None
    peer = request.client.host if getattr(request, "client", None) else None
    return _RATE.client_ip(peer, request.headers.get("x-forwarded-for"))


def _rate_limit_error(ctx: Optional[Context]) -> Optional[str]:
    """Return an error message if this caller is blocked or over the search rate limit."""
    ip = _caller_ip(ctx)
    if ip is None:
        return None
    _metrics.note_client(ip)
    blocked = _RATE.blocked_for(ip)
    if blocked:
        _events.record("blocked_ip", "A temporarily blocked client tried again.", level="INFO",
                       source="healer", group_key="blocked_ip")
        return ("This client is temporarily blocked after repeated abuse. "
                f"Try again in {max(1, round(blocked / 60))} minute(s).")
    allowed, limit, _remaining, retry = _RATE.check(ip)
    if allowed:
        return None
    _HEALER.note_offense(ip, "rate_limited")
    log.info("Rate limit hit for %s (limit %d/min)", ip, limit)
    _events.record("rate_limited", f"A client hit the rate limit ({limit} searches/min).",
                   level="INFO", source="ratelimit", group_key="rate_limited")
    return (f"Rate limit reached: Cited allows {limit} searches per minute. "
            f"Please wait {retry} seconds and try again.")


async def _save_history(url, question, answer, sources, cov: dict, tool) -> None:
    if not _LOG_QUERIES:
        return
    try:
        await asyncio.to_thread(_history.save, url, question, answer, sources,
                                cov["top_similarity"], tool, not _HTTP_MODE, cov["level"])
    except Exception:
        log.exception("Failed to save history")  # never break a tool call


# ── Tool 1: search_newsletter ────────────────────────────────────────────────────

@mcp.tool(title="Search a newsletter or podcast",
          annotations=_read_only("Search a newsletter or podcast"))
async def search_newsletter(url: str, query: str, top_k: int = 5,
                          ctx: Optional[Context] = None) -> str:
    """
    Search a newsletter or podcast feed for content relevant to a query.

    Accepts Substack URLs, Medium URLs, and any RSS or Atom feed URL (including
    podcast feeds). Fetches the source's public posts/episodes (Substack: up to
    the 200 most recent; feeds: whatever the feed lists; cached for 1 hour),
    builds a hybrid BM25 + vector index, and reranks with a
    cross-encoder. Returns ranked excerpts with titles, dates, URLs, and a
    confidence rating: High / Moderate / Limited coverage plus a best-match
    percentage. Limited coverage means the source probably doesn't cover the topic.

    Args:
        url:   Newsletter or feed URL — Substack, Medium, or any RSS/podcast feed
        query: The question or search query
        top_k: Number of excerpts to return (default 5, max 10)
    """
    limited = _rate_limit_error(ctx)
    if limited:
        return json.dumps({"error": limited, "rate_limited": True})
    try:
        top_k = max(1, min(int(top_k), 10))
    except (TypeError, ValueError):
        top_k = 5
    try:
        n_posts, hits, cov, lib_updated = await _retrieve(url, query, top_k)
        if not n_posts:
            return json.dumps({
                "error": "No public posts found. The newsletter may be empty or fully paywalled.",
                "url": url,
            })

        result: dict = {
            "newsletter": url,
            "posts_indexed": n_posts,
            "coverage_note": _coverage_note(
                n_posts, lib_updated,
                (_LIBRARY.sources.get(_LIBRARY.resolve(url) or "", {}) or {}).get("kind", "newsletter")),
            "confidence": _confidence_payload(cov),
            "low_confidence": cov["level"] == "low",
            "results": [
                {
                    "rank":       i + 1,
                    "title":      h["title"],
                    "date":       h["date"],
                    "url":        h["url"],
                    "match_pct":  max(0, min(100, round(h["similarity"] * 100))),
                    "excerpt":    h["text"],
                    **({"attribution": h["attribution"]} if h.get("attribution") else {}),
                }
                for i, h in enumerate(hits)
            ],
        }

        if cov["level"] == "low":
            result["disclaimer"] = _low_coverage_disclaimer(_source_name(url), cov)
            result["instructions_for_assistant"] = (
                "Show `disclaimer` to the user verbatim at the top of your answer. Clearly "
                "separate anything from general knowledge or web search from what the excerpts "
                "below actually say, and cite the excerpts you use."
            )

        # Save to history — store top excerpt as the answer so it's retrievable
        top_excerpt = hits[0]["text"] if hits else ""
        await _save_history(url, query, top_excerpt, result["results"], cov, "search")

        return json.dumps(result, indent=2)

    except _fetcher.NotSubstackError as exc:
        _tool_problem("search_newsletter", url, exc, _caller_ip(ctx))
        return json.dumps({"error": str(exc)})
    except _fetcher.FetchError as exc:
        _tool_problem("search_newsletter", url, exc, _caller_ip(ctx))
        return json.dumps({"error": f"Failed to fetch newsletter: {exc}"})
    except Exception as exc:
        log.exception("search_newsletter failed (url=%s)", url)
        return json.dumps({"error": f"Unexpected error: {exc}"})


# ── Library tools (hosted mode with a library) ─────────────────────────────────

if _LIBRARY.enabled and _lib_sources:

    @mcp.tool(title="Search the Cited library",
              annotations=_read_only("Search the Cited library"))
    async def search_library(question: str, newsletters: Optional[list[str]] = None,
                             top_k: int = 8, ctx: Optional[Context] = None) -> str:
        """
        Search Cited's library of pre-indexed newsletters (full archives of free posts)
        — several at once — and get the best passages across them, each labelled with
        its newsletter, plus how well each newsletter covers the question.

        Routing: if `newsletters` is given, only those are searched (names, authors or
        URLs, e.g. ["Marty Cagan", "Product Talk"]). Otherwise, newsletters or writers
        named in the question are searched; if none are named, the whole library is.
        Call list_library to see what's included.

        Args:
            question:    The question or search query
            newsletters: Optional names, authors or URLs to restrict the search to
            top_k:       Number of passages to return (default 8, max 15)
        """
        limited = _rate_limit_error(ctx)
        if limited:
            return json.dumps({"error": limited, "rate_limited": True})
        try:
            top_k = max(1, min(int(top_k), 15))
        except (TypeError, ValueError):
            top_k = 8
        if isinstance(newsletters, str):
            newsletters = [newsletters]
        try:
            route = _LIBRARY.route(question, newsletters)
            if not route["keys"]:
                if route.get("opted_out") and not route["not_ready"]:
                    problem = (", ".join(route["opted_out"]) + " opted out of AI tools, so Cited doesn't "
                               "index them. Please read them directly on their sites.")
                elif route["not_found"] and not route["not_ready"]:
                    problem = ("None of the requested newsletters are in the library: "
                               + ", ".join(route["not_found"]) + ".")
                else:
                    problem = ("The library is still being built — try again in a few minutes, or use "
                               "search_newsletter with the newsletter's URL.")
                return json.dumps({"error": problem, "routing": route | {"keys": []},
                                   "library": [c["name"] for c in _LIBRARY.catalog()]})

            hits, per_source = await asyncio.to_thread(_LIBRARY.search_many, route["keys"], question, top_k)
            cov = _pipeline.coverage(hits)
            _recent_levels.append(cov["level"])
            names = [_LIBRARY.sources[k]["name"] for k in route["keys"]]
            strong = [c["name"] for c in per_source if c["level"] in ("high", "medium")]
            result: dict = {
                "routing": {
                    "searched": names,
                    "reason": route["reason"],
                    "best_coverage": strong[:3] or None,
                    **({"not_in_library": route["not_found"]} if route["not_found"] else {}),
                    **({"still_building": route["not_ready"]} if route["not_ready"] else {}),
                    **({"opted_out_of_ai": route["opted_out"]} if route.get("opted_out") else {}),
                },
                "confidence": _confidence_payload(cov),
                "low_confidence": cov["level"] == "low",
                "coverage_by_newsletter": [
                    {"newsletter": c["name"], "coverage": {"high": "High", "medium": "Moderate",
                                                           "low": "Limited"}[c["level"]],
                     "best_match_pct": c["best_match_pct"], "free_posts": c["posts"]}
                    for c in per_source],
                "results": [
                    {"rank": i + 1, "newsletter": h["source_name"], "title": h["title"],
                     "date": h["date"], "url": h["url"],
                     "match_pct": max(0, min(100, round(h["similarity"] * 100))),
                     "excerpt": h["text"],
                     **({"attribution": h["attribution"]} if h.get("attribution") else {})}
                    for i, h in enumerate(hits)],
            }
            if cov["level"] == "low":
                who = names[0] if len(names) == 1 else "The Cited library"
                result["disclaimer"] = _low_coverage_disclaimer(who, cov)
                result["instructions_for_assistant"] = (
                    "Show `disclaimer` to the user verbatim at the top of your answer. Clearly "
                    "separate anything from general knowledge or web search from what the excerpts "
                    "below actually say, and cite the excerpts you use.")
            top_excerpt = hits[0]["text"] if hits else ""
            await _save_history("library://" + "+".join(route["keys"])[:400], question, top_excerpt,
                                result["results"], cov, "search")
            return json.dumps(result, indent=2)
        except Exception as exc:
            log.exception("search_library failed")
            return json.dumps({"error": f"Unexpected error: {exc}"})

    @mcp.tool(title="List the Cited library",
              annotations=_read_only("List the Cited library", open_world=False))
    async def list_library() -> str:
        """
        List the newsletters in Cited's library — the ones with full archives pre-indexed
        that search_library can search — with their URLs, authors and how many free posts
        are indexed.
        """
        return json.dumps({"newsletters": await asyncio.to_thread(_LIBRARY.catalog)}, indent=2)


# ── Tool 2: list_posts ─────────────────────────────────────────────────────────

@mcp.tool(title="List a newsletter's posts",
          annotations=_read_only("List a newsletter's posts"))
async def list_posts(url: str, ctx: Optional[Context] = None) -> str:
    """
    List the public posts or episodes Cited can see for a newsletter or feed.

    Accepts Substack URLs, Medium URLs, and any RSS or Atom feed URL (including
    podcast feeds). Returns titles, publication dates, and URLs. Useful for
    exploring what topics a source covers before searching.

    Args:
        url: Newsletter or feed URL — Substack, Medium, or any RSS/podcast feed
    """
    limited = _rate_limit_error(ctx)
    if limited:
        return f"Error: {limited}"
    try:
        lib_key = _LIBRARY.resolve(url)
        if lib_key and _LIBRARY.ready(lib_key):
            rows = await asyncio.to_thread(_LIBRARY.posts, lib_key, 300)
            total = next((r["_total"] for r in rows if "_total" in r), None)
            rows = [r for r in rows if "_total" not in r]
            head = (f"{total} free posts in Cited's archive of {url}; showing the {len(rows)} most recent\n"
                    if total else f"{len(rows)} free posts in Cited's archive of {url}\n")
            return "\n".join([head] + [f"• [{r['date']}] {r['title']}\n  {r['url']}" for r in rows])

        posts = await _fetcher.get_posts(url)
        if not posts:
            return f"No public posts found at {url}."

        lines = [f"{len(posts)} public posts from {url}\n"]
        for p in posts:
            lines.append(f"• [{p.date}] {p.title}\n  {p.url}")
        return "\n".join(lines)

    except _fetcher.NotSubstackError as exc:
        _tool_problem("list_posts", url, exc, _caller_ip(ctx))
        return f"Error: {exc}"
    except _fetcher.FetchError as exc:
        _tool_problem("list_posts", url, exc, _caller_ip(ctx))
        return f"Failed to fetch newsletter: {exc}"
    except Exception as exc:
        log.exception("list_posts failed (url=%s)", url)
        return f"Unexpected error: {exc}"


# ── Tool 3: ask_newsletter (only when API key is present) ────────────────────────

# ask_newsletter calls Haiku server-side — only available in local/stdio mode so
# remote users never consume the deployer's API credits. In HTTP mode, Claude
# in the user's chat synthesizes the answer from search_newsletter excerpts instead.
if not _HTTP_MODE and os.getenv("ANTHROPIC_API_KEY"):

    @mcp.tool(title="Ask a newsletter (answer written by Claude Haiku)",
              annotations=_read_only("Ask a newsletter (answer written by Claude Haiku)"))
    async def ask_newsletter(url: str, question: str) -> str:
        """
        Ask a question about a newsletter or podcast feed and get a direct cited answer.

        Fetches the newsletter, retrieves the most relevant excerpts, then calls
        Claude Haiku to generate a grounded answer with article citations.
        Returns the answer, sources used, and retrieval confidence.

        Use this when the user wants a direct answer rather than raw excerpts.

        Args:
            url:      Newsletter or feed URL — Substack, Medium, or any RSS/podcast feed
            question: The question to answer from the newsletter content
        """
        import anthropic

        try:
            n_posts, hits, cov, _lib = await _retrieve(url, question, 5)
            if not n_posts:
                return json.dumps({
                    "error": "No public posts found. The newsletter may be empty or fully paywalled.",
                    "url": url,
                })
            context    = _pipeline.build_context(hits)
            sources    = [{"title": h["title"], "url": h["url"], "date": h["date"]}
                          for h in hits]

            system = (
                "You are an expert on this newsletter. "
                "Answer the question ONLY from the provided article excerpts. "
                "Cite the article title for every specific claim. "
                "If the excerpts don't cover the question clearly, say so honestly."
            )
            disclaimer = (_low_coverage_disclaimer(_source_name(url), cov)
                          if cov["level"] == "low" else None)
            if disclaimer:
                system += (
                    f"\n\nNote: Limited coverage (best match {cov['percent']}%). "
                    "The newsletter may not cover this topic well. "
                    "Be explicit about uncertainty and recommend verifying with sources."
                )

            client = anthropic.AsyncAnthropic()
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=f"{system}\n\nARTICLE EXCERPTS:\n{context}",
                messages=[{"role": "user", "content": question}],
            )
            answer = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            if disclaimer:
                answer = f"{disclaimer}\n\n{answer}"

            result = {
                "question":       question,
                "answer":         answer,
                "confidence":     _confidence_payload(cov),
                "low_confidence": cov["level"] == "low",
                "disclaimer":     disclaimer,
                "sources":        sources,
                "posts_indexed":  n_posts,
            }

            await _save_history(url, question, answer, sources, cov, "ask")

            return json.dumps(result, indent=2)

        except _fetcher.NotSubstackError as exc:
            return json.dumps({"error": str(exc)})
        except _fetcher.FetchError as exc:
            return json.dumps({"error": f"Failed to fetch newsletter: {exc}"})
        except Exception as exc:
            log.exception("ask_newsletter error")
            return json.dumps({"error": f"Unexpected error: {exc}"})


# ── Tool 4 & 5: history tools ──────────────────────────────────────────────────
# Local (stdio): get_history and search_my_history are open — it's your own machine.
# Remote (http): all queries are saved, but only an admin with CITED_ADMIN_TOKEN
#   can retrieve them via admin_get_history. Regular users see no history tools.

# Brute-force throttle: after _MAX_FAILS bad tokens within _FAIL_WINDOW seconds,
# refuse all admin attempts until the window passes. Global (not per-client)
# because the server is stateless and sits behind a proxy.
_MAX_FAILS   = 5
_FAIL_WINDOW = 15 * 60
_fail_times: list[float] = []
_fail_lock  = threading.Lock()


def _check_admin(token: Optional[str]) -> Optional[str]:
    """Return an error string if the token is wrong, None if it's correct."""
    if not _ADMIN_TOKEN:
        return "Admin access is not enabled on this server."
    now = time.time()
    locked = just_locked = False
    with _fail_lock:
        _fail_times[:] = [t for t in _fail_times if now - t < _FAIL_WINDOW]
        if len(_fail_times) >= _MAX_FAILS:
            locked = True
        elif not token or not hmac.compare_digest(token.encode(), _ADMIN_TOKEN.encode()):
            _fail_times.append(now)
            just_locked = len(_fail_times) >= _MAX_FAILS
        else:
            return None
    # Record outside the lock. Never record the token value itself.
    if locked:
        return "Too many failed attempts. Try again later."
    _events.record("admin_auth_failed", "Wrong or missing admin token.", level="WARNING",
                   source="admin", group_key="admin_auth_failed")
    if just_locked:
        _events.record("admin_lockout",
                       f"{_MAX_FAILS} wrong admin tokens in {_FAIL_WINDOW // 60} min — admin access "
                       "locked temporarily.", level="ERROR", source="admin", group_key="admin_lockout")
    return "Invalid admin token."


def _format_history(rows: list[dict], header: str, with_meta: bool = True) -> str:
    lines = [header]
    for r in rows:
        entry = (f"[{r['date']}] {r['label']}\n"
                 f"  Q: {r['question']}\n"
                 f"  A: {r['answer'][:300].strip()}{'…' if len(r['answer']) > 300 else ''}\n")
        if with_meta:
            sources_str = ", ".join(s.get("title", "") for s in r["sources"][:3])
            conf = r["confidence"] if r["confidence"] is not None else 0.0
            level = {"high": "High", "medium": "Moderate", "low": "Limited"}.get(r.get("level") or "", "")
            conf_str = f"{level} coverage, best match {conf:.0%}" if level else f"{conf:.0%}"
            entry += f"  Sources: {sources_str or 'none'} | Confidence: {conf_str}\n"
        lines.append(entry)
    return "\n".join(lines)


if not _HTTP_MODE:

    @mcp.tool(title="Get my Cited history",
              annotations=_read_only("Get my Cited history", open_world=False))
    async def get_history(url: Optional[str] = None, limit: int = 20) -> str:
        """
        Retrieve your past questions and answers from Cited across all sessions.

        Returns Q&As saved from previous search_newsletter and ask_newsletter calls,
        newest first. Use this at the start of a session to resume prior research
        without repeating searches you've already done.

        Args:
            url:   Filter to one newsletter (e.g. https://aakashgupta.substack.com).
                   Omit to see history across all newsletters.
            limit: Max number of entries to return (default 20).
        """
        rows = await asyncio.to_thread(_history.get, url, max(1, min(int(limit), 100)))
        if not rows:
            return f"No history for {url} yet." if url else "No history yet."
        return _format_history(
            rows, f"{len(rows)} past Q&As" + (f" for {url}" if url else "") + ":\n")

    @mcp.tool(title="Search my Cited history",
              annotations=_read_only("Search my Cited history", open_world=False))
    async def search_my_history(query: str) -> str:
        """
        Search your Cited history by keyword across all past questions and answers.

        Useful when you remember asking about a topic but can't recall which
        newsletter or session it was in.

        Args:
            query: Keyword or phrase to search for in past questions and answers.
        """
        rows = await asyncio.to_thread(_history.keyword_search, query, 10)
        if not rows:
            return f"No history found matching '{query}'."
        return _format_history(rows, f"{len(rows)} result(s) for '{query}':\n",
                               with_meta=False)

elif _ADMIN_TOKEN and _ADMIN_MCP_TOOL:
    # ── Optional admin history tool (HTTP mode) ────────────────────────────────
    # Off by default: the preferred route is GET /admin/history with an
    # Authorization: Bearer header, which keeps the token out of chat transcripts
    # and hides the tool from regular users. Enable with CITED_ADMIN_MCP_TOOL=1.

    @mcp.tool(title="[Admin] View all queries",
              annotations=_read_only("[Admin] View all queries", open_world=False))
    async def admin_get_history(
        admin_token: str,
        url: Optional[str] = None,
        limit: int = 50,
        query: Optional[str] = None,
    ) -> str:
        """
        [ADMIN ONLY] View all queries made to this Cited server across all users.

        Requires the CITED_ADMIN_TOKEN set on the server. Every search_newsletter
        and ask_newsletter call is logged; this tool retrieves that log.

        Args:
            admin_token: The server's admin token (set via CITED_ADMIN_TOKEN env var).
            url:         Filter to one newsletter URL (optional).
            limit:       Max rows to return (default 50, max 500).
            query:       Keyword filter across questions and answers (optional).
        """
        err = _check_admin(admin_token)
        if err:
            return f"Access denied: {err}"

        rows = await asyncio.to_thread(_admin_rows, url, limit, query)
        if not rows:
            return "No history found."
        return _format_history(
            rows, f"{len(rows)} entries" + (f" matching '{query}'" if query else "") + ":\n")


# ── Entry point ────────────────────────────────────────────────────────────────
#
# CITED_TRANSPORT=stdio (default) — local MCP for Claude Code / Claude Desktop
# CITED_TRANSPORT=http           — hosted MCP for claude.ai Integrations
#
# When hosted, set PORT (default 8000). ANTHROPIC_API_KEY is not used in HTTP mode.
# Users connect by adding https://<host>/mcp in claude.ai → Customize → Connectors.

def _admin_rows(url: Optional[str], limit, query: Optional[str]) -> list[dict]:
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    if query:
        return _history.keyword_search(query, limit=limit)
    return _history.get(url=url, limit=limit)


async def _send_json(send, status: int, payload: dict, head: bool = False) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [[b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                            [b"cache-control", b"no-store"]]})
    await send({"type": "http.response.body", "body": b"" if head else body})


async def _tool_names() -> list[str]:
    return [t.name for t in await mcp.list_tools()]


def _on_health_change(previous, status, checks) -> None:
    failing = [f"{_health.CHECK_LABELS.get(k, k)}: {v['status']}"
               for k, v in checks.items() if v["status"] not in ("ok", "disabled")]
    _events.record("health_change",
                   f"Health changed from {previous or 'unknown'} to {status}"
                   + (f" ({'; '.join(failing)})" if failing else ""),
                   level="INFO" if status == "ok" else ("WARNING" if status == "degraded" else "ERROR"),
                   source="health", context={"from": previous, "to": status},
                   group_key=f"health|{previous}|{status}")


def _recent_errors_last_hour() -> Optional[int]:
    if not _events.enabled():
        return None
    s = _events.summary(hours=1)["by_level"]
    return int(s.get("ERROR", 0) + s.get("CRITICAL", 0))


_HEALTH = _health.HealthChecker(
    list_tools=_tool_names,
    log_queries=_LOG_QUERIES,
    db_path=lambda: _history.DB_PATH,
    db_check=_history.check_writable,
    rate_limit_per_min=(_RATE.per_min or None) if _HTTP_MODE else None,
    on_status_change=_on_health_change,
    recent_levels=lambda: list(_recent_levels),
    low_conf_alert=_LOW_CONF_ALERT,
    recent_errors=_recent_errors_last_hour,
    library_status=lambda: _LIBRARY.status(),
)


_ADMIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin")
_ADMIN_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
              "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
              "base-uri 'none'; form-action 'none'")


_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_FILES = {   # public landing page
    "/":           ("web/index.html", b"text/html; charset=utf-8"),
    "/landing.js": ("web/landing.js", b"text/javascript; charset=utf-8"),
    "/icon.png":   (".claude-plugin/icon.png", b"image/png"),
    "/favicon.ico": (".claude-plugin/icon.png", b"image/png"),
}
_WEB_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


async def _send_file(send, name: str, content_type: bytes, head: bool = False,
                     base: str = _ADMIN_DIR, csp: str = _ADMIN_CSP, cache: bytes = b"no-store") -> None:
    with open(os.path.join(base, name), "rb") as f:
        body = f.read()
    await send({"type": "http.response.start", "status": 200, "headers": [
        [b"content-type", content_type],
        [b"content-length", str(len(body)).encode()],
        [b"cache-control", cache],
        [b"content-security-policy", csp.encode()],
        [b"x-frame-options", b"DENY"],
        [b"x-content-type-options", b"nosniff"],
        [b"referrer-policy", b"no-referrer"],
    ]})
    await send({"type": "http.response.body", "body": b"" if head else body})


def _qs_int(qs: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int((qs.get(key) or [default])[0]), hi))
    except (TypeError, ValueError):
        return default


async def _admin_api(path: str, qs: dict) -> dict:
    """Data for the admin dashboard (caller has already been authenticated)."""
    one = lambda k: (qs.get(k) or [None])[0]
    if path == "/admin/api/overview":
        _, health = await _HEALTH.report(len(_pipeline._EMBED_CACHE))
        searches = await asyncio.to_thread(_history.stats, 24) if _LOG_QUERIES else None
        events_summary = await asyncio.to_thread(_events.summary, 24)
        security = await asyncio.to_thread(_events.security_summary)
        fixes = await asyncio.to_thread(_events.fix_stats, 30)
        autofix = await asyncio.to_thread(_HEALER.snapshot, _RATE.blocked())
        growth = (await asyncio.to_thread(_metrics.growth, _history.DB_PATH, 30)
                  if _LOG_QUERIES else None)
        return {"health": health, "searches": searches, "events": events_summary,
                "security": security, "fixes": fixes, "autofix": autofix,
                "digest": _digest.status_for_dashboard(), "growth": growth,
                "library": await asyncio.to_thread(_LIBRARY.status)}
    if path == "/admin/api/searches":
        if not _LOG_QUERIES:
            return {"enabled": False, "searches": []}
        level = one("level")
        level = level if level in ("high", "medium", "low") else None
        rows = await asyncio.to_thread(_history.recent, _qs_int(qs, "limit", 50, 1, 500), level)
        return {"enabled": True, "searches": rows}
    if path == "/admin/api/errors":
        hours = _qs_int(qs, "hours", 24, 1, 24 * 90)
        category = one("category")
        category = category if category in ("error", "security", "usage", "health") else None
        rows = await asyncio.to_thread(_events.groups, hours, category, one("level"),
                                       _qs_int(qs, "limit", 200, 1, 1000))
        return {"enabled": _events.enabled(), "groups": rows}
    raise KeyError(path)


async def _admin_action(path: str, body: dict) -> tuple[int, dict]:
    if path == "/admin/api/library/refresh":
        key = str(body.get("key") or "")
        if not _LIBRARY.has(key):
            return 404, {"error": "Not a library source"}
        asyncio.create_task(_LIBRARY.refresh(key, full=bool(body.get("full"))))
        return 202, {"ok": True, "started": key}
    if path == "/admin/api/digest/test":
        if not _digest.config()["enabled"]:
            return 400, {"error": "Digest isn't configured: set CITED_DIGEST_TO and RESEND_API_KEY "
                                  "(or SMTP_HOST) on the server."}
        try:
            return 200, {"ok": True, "result": await _send_digest(mark=False)}
        except Exception as exc:
            log.warning("Test digest failed: %s", exc)
            return 502, {"error": f"Sending failed: {exc}"}
    fp = str(body.get("fingerprint") or "")
    if not fp or len(fp) > 64:
        return 400, {"error": "fingerprint is required"}
    if path == "/admin/api/forget":
        ok = await asyncio.to_thread(_HEALER.forget, fp)
    elif path == "/admin/api/resolve":
        ok = await asyncio.to_thread(_events.resolve, fp, str(body.get("note") or ""), "admin")
    elif path == "/admin/api/reopen":
        ok = await asyncio.to_thread(_events.reopen, fp)
    else:
        return 404, {"error": "Not found"}
    return (200, {"ok": True}) if ok else (404, {"error": "No such error"})


async def _read_json_body(receive, limit: int = 16_384) -> Optional[dict]:
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            return None
        chunk = msg.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not msg.get("more_body"):
            break
    try:
        data = json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_monitor_task: Optional[asyncio.Task] = None
_library_task: Optional[asyncio.Task] = None


def _start_library() -> None:
    """Start the library's background refresher (loads stored indexes first)."""
    global _library_task
    if not _LIBRARY.enabled or _library_task is not None:
        return
    interval = float(os.getenv("CITED_LIBRARY_REFRESH_MINUTES", "60")) * 60
    _library_task = asyncio.create_task(_LIBRARY.run_forever(interval, _pipeline.models_loaded))


async def _health_monitor(interval: float) -> None:
    """Run the full health report every `interval` seconds (like ThinkWithMe's
    health agent), so status changes and warnings are logged even when nobody
    is looking at the dashboard."""
    while True:
        try:
            _status, report = await _HEALTH.report(len(_pipeline._EMBED_CACHE))
            await asyncio.to_thread(_HEALER.run_cycle, report)
        except Exception:
            log.exception("Background health check failed")
        try:
            if _events.enabled() and await asyncio.to_thread(_digest.is_due, _history.DB_PATH):
                await _send_digest()
        except Exception:
            log.exception("Daily digest failed")
        await asyncio.sleep(interval)


async def _digest_data() -> dict:
    _, health = await _HEALTH.report(len(_pipeline._EMBED_CACHE))
    return {
        "health": health,
        "searches": await asyncio.to_thread(_history.stats, 24) if _LOG_QUERIES else None,
        "security": await asyncio.to_thread(_events.security_summary),
        "fixes": await asyncio.to_thread(_events.fix_stats, 30),
        "fixes_24h": await asyncio.to_thread(_events.fix_stats, 1),
        "autofix": await asyncio.to_thread(_HEALER.snapshot, _RATE.blocked()),
        "errors": await asyncio.to_thread(_events.groups, 24, None, None, 300),
    }


async def _send_digest(mark: bool = True) -> str:
    """Build and send the digest. Marks today as sent (unless it's a test)."""
    from datetime import datetime, timezone
    data = await _digest_data()
    subject, text, html_body = _digest.build(data, _digest.config()["public_url"])
    result = await asyncio.to_thread(_digest.send, subject, text, html_body)
    if mark:
        await asyncio.to_thread(_digest.mark_sent, _history.DB_PATH,
                                datetime.now(timezone.utc).date().isoformat())
    log.info("Daily digest %s", result)
    return result


def build_http_app(monitor_interval: Optional[float] = None):
    """ASGI app: /health*, /admin (dashboard + API), and the MCP endpoint at /mcp.

    monitor_interval: if set, start the background health monitor at startup.
    """
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host="0.0.0.0",
    )

    health_routes = {
        "/health/live":  lambda: _HEALTH.live(),
        "/health/ready": lambda: _HEALTH.ready(),
        "/health":       lambda: _HEALTH.report(len(_pipeline._EMBED_CACHE)),
    }

    async def app(scope, receive, send):
        path = scope.get("path") if scope["type"] == "http" else None
        if path is not None and path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        method = scope.get("method") if path else None
        if path in health_routes or path == "/":
            if method not in ("GET", "HEAD"):
                await _send_json(send, 405, {"error": "Method not allowed"})
                return
            accept = dict((k.decode("latin-1").lower(), v.decode("latin-1"))
                          for k, v in scope.get("headers", [])).get("accept", "")
            if path == "/" and "text/html" in accept:
                await _send_file(send, "web/index.html", b"text/html; charset=utf-8",
                                 method == "HEAD", base=_ROOT_DIR, csp=_WEB_CSP,
                                 cache=b"public, max-age=300")
                return
            if path == "/":
                status, payload = 200, {
                    "name": "Cited",
                    "version": _health.VERSION,
                    "mcp_endpoint": "/mcp",
                    "health": "/health",
                    "usage": "Add https://<this-host>/mcp in claude.ai → Customize → Connectors",
                }
            else:
                try:
                    status, payload = await health_routes[path]()
                except Exception as exc:  # a health check must never crash the server
                    log.exception("Health check failed")
                    status, payload = 503, {"status": "unavailable",
                                            "error": f"{type(exc).__name__}: {exc}"}
            await _send_json(send, status, payload, head=(method == "HEAD"))
        elif path in _WEB_FILES and path != "/":
            if method not in ("GET", "HEAD"):
                await _send_json(send, 405, {"error": "Method not allowed"})
                return
            name, ctype = _WEB_FILES[path]
            await _send_file(send, name, ctype, method == "HEAD", base=_ROOT_DIR, csp=_WEB_CSP,
                             cache=b"public, max-age=3600")
        elif path in ("/admin", "/admin/app.js"):
            if method not in ("GET", "HEAD"):
                await _send_json(send, 405, {"error": "Method not allowed"})
                return
            if not _ADMIN_TOKEN:
                await _send_json(send, 404, {"error": "Admin access is not enabled on this server."})
                return
            if path == "/admin":
                await _send_file(send, "dashboard.html", b"text/html; charset=utf-8", method == "HEAD")
            else:
                await _send_file(send, "app.js", b"text/javascript; charset=utf-8", method == "HEAD")
        elif path and path.startswith("/admin/api/"):
            is_action = path in ("/admin/api/resolve", "/admin/api/reopen", "/admin/api/forget",
                                 "/admin/api/digest/test", "/admin/api/library/refresh")
            if method != ("POST" if is_action else "GET"):
                await _send_json(send, 405, {"error": "Method not allowed"})
                return
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            auth  = headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
            client = scope.get("client") or (None, None)
            ip = _RATE.client_ip(client[0], headers.get("x-forwarded-for"))
            if _ADMIN_TOKEN and _RATE.blocked_for(ip):
                await _send_json(send, 403, {"error": "Temporarily blocked."})
                return
            err = _check_admin(token)
            if err:
                if _ADMIN_TOKEN and "Invalid" in err:
                    _HEALER.note_offense(ip, "admin_auth_failed")
                status = 404 if not _ADMIN_TOKEN else (429 if "Too many" in err else 401)
                await _send_json(send, status, {"error": err})
                return
            if is_action:
                body = await _read_json_body(receive)
                if body is None:
                    await _send_json(send, 400, {"error": "Expected a small JSON object"})
                    return
                status, payload = await _admin_action(path, body)
                await _send_json(send, status, payload)
                return
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            try:
                payload = await _admin_api(path, qs)
            except KeyError:
                await _send_json(send, 404, {"error": "Not found"})
                return
            await _send_json(send, 200, payload)
        elif path == "/admin/history":
            if scope.get("method") != "GET":
                await _send_json(send, 405, {"error": "Method not allowed"})
                return
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            auth  = headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
            err = _check_admin(token)
            if err:
                status = 404 if not _ADMIN_TOKEN else (429 if "Too many" in err else 401)
                await _send_json(send, status, {"error": err})
                return
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            one = lambda k: (qs.get(k) or [None])[0]
            rows = await asyncio.to_thread(_admin_rows, one("url"),
                                           one("limit") or 50, one("query"))
            await _send_json(send, 200, {"count": len(rows), "entries": rows})
        else:
            await _guarded(mcp_app, scope, receive, send)

    async def _guarded(inner, scope, receive, send):
        """Run the MCP app; log anything it fails to handle and return a clean 500."""
        if scope["type"] == "http":
            _events.install_loop_handler(asyncio.get_running_loop())
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await inner(scope, receive, tracking_send)
        except Exception:
            if scope["type"] != "http":
                raise
            log.exception("Unhandled error serving %s %s", scope.get("method"), scope.get("path"))
            if not started:
                await _send_json(send, 500, {"error": "Internal server error"})

    async def outer(scope, receive, send):
        if scope["type"] == "lifespan" and monitor_interval:
            async def lifespan_receive():
                global _monitor_task
                message = await receive()
                if message["type"] == "lifespan.startup" and _monitor_task is None:
                    _monitor_task = asyncio.create_task(_health_monitor(monitor_interval))
                    _start_library()
                return message
            return await app(scope, lifespan_receive, send)
        try:
            await app(scope, receive, send)
        except Exception:
            if scope["type"] != "http":
                raise
            log.exception("Unhandled error serving %s %s", scope.get("method"), scope.get("path"))
            raise

    return outer


if __name__ == "__main__":
    transport = os.getenv("CITED_TRANSPORT", "stdio")
    if transport == "http":
        import uvicorn

        port = int(os.getenv("PORT", "8000"))
        log.info("Starting Cited HTTP server on port %d (path /mcp)", port)

        # Pre-warm models at startup. Popular newsletters live in the library
        # (library.json); CITED_PREWARM_URLS can still warm extra sources in memory.
        _PREWARM_URLS = [
            u.strip() for u in os.getenv("CITED_PREWARM_URLS", "").split(",") if u.strip()
        ]

        async def _prewarm_all():
            # Load models, retrying with backoff: a transient download failure
            # shouldn't leave the server permanently "not ready".
            _HEALTH.prewarm_started()
            delays = [5, 15, 30, 60, 120]
            for attempt in range(len(delays) + 1):
                try:
                    await asyncio.to_thread(_pipeline.prewarm)
                    _HEALTH.prewarm_finished()
                    break
                except Exception as exc:
                    if attempt == len(delays):
                        log.error("Model loading failed after %d attempts: %s", attempt + 1, exc)
                        _HEALTH.prewarm_finished(exc)
                        return
                    log.warning("Model loading failed (attempt %d), retrying in %ds: %s",
                                attempt + 1, delays[attempt], exc)
                    await asyncio.sleep(delays[attempt])
            for url in _PREWARM_URLS:
                try:
                    posts  = await _fetcher.get_posts(url)
                    chunks = _pipeline.chunk_posts(posts)
                    key    = _fetcher.parse_subdomain(url)
                    await asyncio.to_thread(_pipeline.precompute_embeddings, key, chunks)
                except Exception as exc:
                    log.warning("Pre-warm failed for %s: %s", url, exc)

        def _run_prewarm():
            asyncio.run(_prewarm_all())
        threading.Thread(target=_run_prewarm, daemon=True).start()

        interval = float(os.getenv("CITED_HEALTH_MONITOR_SECONDS", "60"))
        uvicorn.run(build_http_app(monitor_interval=interval if interval > 0 else None),
                    host="0.0.0.0", port=port)
    else:
        asyncio.run(mcp.run_stdio_async())
