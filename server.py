#!/usr/bin/env python3
"""
Cited — Precise, sourced answers from any Substack newsletter via MCP.

Exposes three tools to any MCP-capable Claude client
(Claude Desktop, Claude Code, Claude Cowork):

  search_substack   — hybrid search, returns ranked excerpts for Claude to cite
  list_posts        — list all public post titles + dates from a newsletter
  ask_substack      — full RAG + server-side answer via Haiku (needs API key)

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
  ANTHROPIC_API_KEY      — enables ask_substack tool (local/stdio mode only)
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
from typing import Optional
from urllib.parse import parse_qs

from mcp.server.mcpserver import Context, MCPServer

import fetcher as _fetcher
import pipeline as _pipeline
import history as _history
import health as _health
import events as _events
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

# Per-IP rate limit for searches in hosted mode (local stdio is never limited).
_RATE = ClientRateLimit.from_env()

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

        "## When to use it\n"
        "Use search_substack when the user asks what a specific newsletter, author, "
        "or podcast has said about a topic. It searches the source's own text, which "
        "can surface posts that general web search ranks poorly.\n\n"

        "## When the user gives a URL\n"
        "Call search_substack directly with that URL and their question. "
        "It accepts Substack URLs, Medium URLs, and any RSS or podcast feed URL.\n\n"

        "## When the user gives only an author or podcast name (no URL)\n"
        "1. Use your web search to find the feed URL only — search for: "
        '"{name} substack", "{name} newsletter", or "{name} podcast RSS feed". '
        "Pick the most likely result — prefer a direct feed URL (/feed, /rss, .xml), "
        "a custom domain, or a substack.com URL over social media pages.\n"
        "2. Tell the user which URL you found, then call search_substack with it.\n\n"

        "## When the user gives a podcast name\n"
        "Search for '{podcast name} RSS feed' to find the feed URL, "
        "then call search_substack with it. Show notes and episode summaries are indexed.\n\n"

        "## Composing answers\n"
        "Always cite the article or episode title and link for every claim. "
        "Every search_substack result has a `confidence` with a level (High / Moderate / "
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

def _coverage_note(n_posts: int) -> str:
    return (f"Searched {n_posts} public posts/episodes (Substack: up to {_fetcher.MAX_POSTS} "
            f"most recent; feeds: whatever the feed publishes). Each post is indexed up to "
            f"its first {_fetcher.POST_CHARS:,} characters.")


# Coverage levels of the most recent searches, for the low-confidence alert
# (same rule as ThinkWithMe's health agent: >50% of the last 20 are Limited).
_LOW_CONF_WINDOW = int(os.getenv("CITED_LOW_CONF_WINDOW", "20"))
_LOW_CONF_ALERT  = float(os.getenv("CITED_LOW_CONF_ALERT", "0.5"))
_recent_levels: deque = deque(maxlen=_LOW_CONF_WINDOW)


async def _retrieve(url: str, query: str, top_k: int):
    """Fetch posts (async I/O) then run chunking + search in a worker thread
    so CPU-bound model inference doesn't block the event loop.
    Returns (posts, hits, coverage) — coverage per pipeline.coverage()."""
    posts = await _fetcher.get_posts(url)
    if not posts:
        return posts, [], _pipeline.coverage([])

    def _work():
        chunks = _pipeline.chunk_posts(posts)
        key    = _fetcher.parse_subdomain(url)
        hits   = _pipeline.search(query, chunks, top_k=top_k, cache_key=key)
        return hits, _pipeline.coverage(hits)

    hits, cov = await asyncio.to_thread(_work)
    _recent_levels.append(cov["level"])
    return posts, hits, cov


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


def _tool_problem(tool: str, url: str, exc: Exception) -> None:
    """Record an expected tool failure (bad URL, unreachable feed) in the error log."""
    if isinstance(exc, _fetcher.UnsafeURLError):
        kind, level = "blocked_url", "WARNING"
    elif isinstance(exc, _fetcher.UnsafeContentError):
        kind, level = "blocked_content", "WARNING"
    elif isinstance(exc, _fetcher.NotSubstackError):
        kind, level = "invalid_source", "INFO"
    else:
        kind, level = "fetch_failed", "WARNING"
    _events.record(kind, str(exc), level=level, source=tool,
                   context={"tool": tool, "url": url[:500]},
                   group_key=f"{kind}|{_fetcher._host(url)}|{type(exc).__name__}")


def _rate_limit_error(ctx: Optional[Context]) -> Optional[str]:
    """Return an error message if this caller is over the search rate limit."""
    if not _HTTP_MODE or ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except Exception:
        request = None
    if request is None:
        return None
    peer = request.client.host if getattr(request, "client", None) else None
    ip = _RATE.client_ip(peer, request.headers.get("x-forwarded-for"))
    allowed, limit, _remaining, retry = _RATE.check(ip)
    if allowed:
        return None
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


# ── Tool 1: search_substack ────────────────────────────────────────────────────

@mcp.tool()
async def search_substack(url: str, query: str, top_k: int = 5,
                          ctx: Optional[Context] = None) -> str:
    """
    Search a newsletter or podcast feed for content relevant to a query.

    Accepts Substack URLs, Medium URLs, and any RSS or Atom feed URL (including
    podcast feeds). Fetches all publicly available posts/episodes (cached for
    1 hour), builds a hybrid BM25 + vector index, and reranks with a
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
        posts, hits, cov = await _retrieve(url, query, top_k)
        if not posts:
            return json.dumps({
                "error": "No public posts found. The newsletter may be empty or fully paywalled.",
                "url": url,
            })

        result: dict = {
            "newsletter": url,
            "posts_indexed": len(posts),
            "coverage_note": _coverage_note(len(posts)),
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
        _tool_problem("search_substack", url, exc)
        return json.dumps({"error": str(exc)})
    except _fetcher.FetchError as exc:
        _tool_problem("search_substack", url, exc)
        return json.dumps({"error": f"Failed to fetch newsletter: {exc}"})
    except Exception as exc:
        log.exception("search_substack failed (url=%s)", url)
        return json.dumps({"error": f"Unexpected error: {exc}"})


# ── Tool 2: list_posts ─────────────────────────────────────────────────────────

@mcp.tool()
async def list_posts(url: str, ctx: Optional[Context] = None) -> str:
    """
    List all publicly available posts or episodes from a newsletter or feed.

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
        posts = await _fetcher.get_posts(url)
        if not posts:
            return f"No public posts found at {url}."

        lines = [f"{len(posts)} public posts from {url}\n"]
        for p in posts:
            lines.append(f"• [{p.date}] {p.title}\n  {p.url}")
        return "\n".join(lines)

    except _fetcher.NotSubstackError as exc:
        _tool_problem("list_posts", url, exc)
        return f"Error: {exc}"
    except _fetcher.FetchError as exc:
        _tool_problem("list_posts", url, exc)
        return f"Failed to fetch newsletter: {exc}"
    except Exception as exc:
        log.exception("list_posts failed (url=%s)", url)
        return f"Unexpected error: {exc}"


# ── Tool 3: ask_substack (only when API key is present) ────────────────────────

# ask_substack calls Haiku server-side — only available in local/stdio mode so
# remote users never consume the deployer's API credits. In HTTP mode, Claude
# in the user's chat synthesizes the answer from search_substack excerpts instead.
if not _HTTP_MODE and os.getenv("ANTHROPIC_API_KEY"):

    @mcp.tool()
    async def ask_substack(url: str, question: str) -> str:
        """
        Ask a question about a Substack newsletter and get a direct cited answer.

        Fetches the newsletter, retrieves the most relevant excerpts, then calls
        Claude Haiku to generate a grounded answer with article citations.
        Returns the answer, sources used, and retrieval confidence.

        Use this when the user wants a direct answer rather than raw excerpts.

        Args:
            url:      Substack newsletter URL, e.g. https://aakashgupta.substack.com
            question: The question to answer from the newsletter content
        """
        import anthropic

        try:
            posts, hits, cov = await _retrieve(url, question, 5)
            if not posts:
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
                "posts_indexed":  len(posts),
            }

            await _save_history(url, question, answer, sources, cov, "ask")

            return json.dumps(result, indent=2)

        except _fetcher.NotSubstackError as exc:
            return json.dumps({"error": str(exc)})
        except _fetcher.FetchError as exc:
            return json.dumps({"error": f"Failed to fetch newsletter: {exc}"})
        except Exception as exc:
            log.exception("ask_substack error")
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

    @mcp.tool()
    async def get_history(url: Optional[str] = None, limit: int = 20) -> str:
        """
        Retrieve your past questions and answers from Cited across all sessions.

        Returns Q&As saved from previous search_substack and ask_substack calls,
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

    @mcp.tool()
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

    @mcp.tool()
    async def admin_get_history(
        admin_token: str,
        url: Optional[str] = None,
        limit: int = 50,
        query: Optional[str] = None,
    ) -> str:
        """
        [ADMIN ONLY] View all queries made to this Cited server across all users.

        Requires the CITED_ADMIN_TOKEN set on the server. Every search_substack
        and ask_substack call is logged; this tool retrieves that log.

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
)


_ADMIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin")
_ADMIN_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
              "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
              "base-uri 'none'; form-action 'none'")


async def _send_file(send, name: str, content_type: bytes, head: bool = False) -> None:
    with open(os.path.join(_ADMIN_DIR, name), "rb") as f:
        body = f.read()
    await send({"type": "http.response.start", "status": 200, "headers": [
        [b"content-type", content_type],
        [b"content-length", str(len(body)).encode()],
        [b"cache-control", b"no-store"],
        [b"content-security-policy", _ADMIN_CSP.encode()],
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
        return {"health": health, "searches": searches, "events": events_summary,
                "security": security, "fixes": fixes}
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
    fp = str(body.get("fingerprint") or "")
    if not fp or len(fp) > 64:
        return 400, {"error": "fingerprint is required"}
    if path == "/admin/api/resolve":
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


async def _health_monitor(interval: float) -> None:
    """Run the full health report every `interval` seconds (like ThinkWithMe's
    health agent), so status changes and warnings are logged even when nobody
    is looking at the dashboard."""
    while True:
        try:
            await _HEALTH.report(len(_pipeline._EMBED_CACHE))
        except Exception:
            log.exception("Background health check failed")
        await asyncio.sleep(interval)


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
            is_action = path in ("/admin/api/resolve", "/admin/api/reopen")
            if method != ("POST" if is_action else "GET"):
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

        # Pre-warm models + index popular newsletters at startup.
        # CITED_PREWARM_URLS = comma-separated list (default: Lenny + Aakash).
        _PREWARM_URLS = [
            u.strip() for u in os.getenv(
                "CITED_PREWARM_URLS",
                "https://www.lennysnewsletter.com,https://news.aakashg.com",
            ).split(",") if u.strip()
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
