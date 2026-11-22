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
  CITED_ALLOW_PRIVATE_URLS — "1" to allow fetching private/local addresses (dev only)
"""

import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import time
from typing import Optional
from urllib.parse import parse_qs

from mcp.server.mcpserver import MCPServer

import fetcher as _fetcher
import pipeline as _pipeline
import history as _history

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
        "If confidence is below 0.5, say so and recommend the user verify with the original source. "
        "If the source is paywalled, returns few results, or the topic may predate the indexed "
        "posts, say so honestly."
    ),
)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _coverage_note(n_posts: int) -> str:
    return (f"Searched {n_posts} public posts/episodes (Substack: up to {_fetcher.MAX_POSTS} "
            f"most recent; feeds: whatever the feed publishes). Each post is indexed up to "
            f"its first {_fetcher.POST_CHARS:,} characters.")


async def _retrieve(url: str, query: str, top_k: int):
    """Fetch posts (async I/O) then run chunking + search in a worker thread
    so CPU-bound model inference doesn't block the event loop."""
    posts = await _fetcher.get_posts(url)
    if not posts:
        return posts, [], 0.0

    def _work():
        chunks = _pipeline.chunk_posts(posts)
        key    = _fetcher.parse_subdomain(url)
        hits   = _pipeline.search(query, chunks, top_k=top_k, cache_key=key)
        return hits, _pipeline.retrieval_confidence(hits)

    hits, confidence = await asyncio.to_thread(_work)
    return posts, hits, confidence


async def _save_history(url, question, answer, sources, confidence, tool) -> None:
    if not _LOG_QUERIES:
        return
    try:
        await asyncio.to_thread(_history.save, url, question, answer, sources,
                                confidence, tool, not _HTTP_MODE)
    except Exception:
        log.exception("Failed to save history")  # never break a tool call


# ── Tool 1: search_substack ────────────────────────────────────────────────────

@mcp.tool()
async def search_substack(url: str, query: str, top_k: int = 5) -> str:
    """
    Search a newsletter or podcast feed for content relevant to a query.

    Accepts Substack URLs, Medium URLs, and any RSS or Atom feed URL (including
    podcast feeds). Fetches all publicly available posts/episodes (cached for
    1 hour), builds a hybrid BM25 + vector index, and reranks with a
    cross-encoder. Returns ranked excerpts with titles, dates, URLs, and a
    retrieval confidence score.

    Low confidence (< 0.5) means the source probably doesn't cover the topic.

    Args:
        url:   Newsletter or feed URL — Substack, Medium, or any RSS/podcast feed
        query: The question or search query
        top_k: Number of excerpts to return (default 5, max 10)
    """
    try:
        top_k = max(1, min(int(top_k), 10))
    except (TypeError, ValueError):
        top_k = 5
    try:
        posts, hits, confidence = await _retrieve(url, query, top_k)
        if not posts:
            return json.dumps({
                "error": "No public posts found. The newsletter may be empty or fully paywalled.",
                "url": url,
            })

        result: dict = {
            "newsletter": url,
            "posts_indexed": len(posts),
            "coverage": _coverage_note(len(posts)),
            "confidence": round(confidence, 3),
            "low_confidence": confidence < 0.5,
            "results": [
                {
                    "rank":    i + 1,
                    "title":   h["title"],
                    "date":    h["date"],
                    "url":     h["url"],
                    "score":   round(h["score"], 3),
                    "excerpt": h["text"],
                }
                for i, h in enumerate(hits)
            ],
        }

        if confidence < 0.5:
            result["warning"] = (
                f"Retrieval confidence is {confidence:.0%} — the newsletter may not "
                "cover this topic well. Remind the reader to verify with original sources."
            )

        # Save to history — store top excerpt as the answer so it's retrievable
        top_excerpt = hits[0]["text"] if hits else ""
        await _save_history(url, query, top_excerpt, result["results"], confidence, "search")

        return json.dumps(result, indent=2)

    except _fetcher.NotSubstackError as exc:
        return json.dumps({"error": str(exc)})
    except _fetcher.FetchError as exc:
        return json.dumps({"error": f"Failed to fetch newsletter: {exc}"})
    except Exception as exc:
        log.exception("search_substack error")
        return json.dumps({"error": f"Unexpected error: {exc}"})


# ── Tool 2: list_posts ─────────────────────────────────────────────────────────

@mcp.tool()
async def list_posts(url: str) -> str:
    """
    List all publicly available posts or episodes from a newsletter or feed.

    Accepts Substack URLs, Medium URLs, and any RSS or Atom feed URL (including
    podcast feeds). Returns titles, publication dates, and URLs. Useful for
    exploring what topics a source covers before searching.

    Args:
        url: Newsletter or feed URL — Substack, Medium, or any RSS/podcast feed
    """
    try:
        posts = await _fetcher.get_posts(url)
        if not posts:
            return f"No public posts found at {url}."

        lines = [f"{len(posts)} public posts from {url}\n"]
        for p in posts:
            lines.append(f"• [{p.date}] {p.title}\n  {p.url}")
        return "\n".join(lines)

    except _fetcher.NotSubstackError as exc:
        return f"Error: {exc}"
    except _fetcher.FetchError as exc:
        return f"Failed to fetch newsletter: {exc}"
    except Exception as exc:
        log.exception("list_posts error")
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
            posts, hits, confidence = await _retrieve(url, question, 5)
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
            if confidence < 0.5:
                system += (
                    f"\n\nNote: Retrieval confidence is {confidence:.0%}. "
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

            result = {
                "question":       question,
                "answer":         answer,
                "confidence":     round(confidence, 3),
                "low_confidence": confidence < 0.5,
                "sources":        sources,
                "posts_indexed":  len(posts),
            }

            await _save_history(url, question, answer, sources, confidence, "ask")

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
    with _fail_lock:
        _fail_times[:] = [t for t in _fail_times if now - t < _FAIL_WINDOW]
        if len(_fail_times) >= _MAX_FAILS:
            return "Too many failed attempts. Try again later."
        if not token or not hmac.compare_digest(token.encode(), _ADMIN_TOKEN.encode()):
            _fail_times.append(now)
            return "Invalid admin token."
    return None


def _format_history(rows: list[dict], header: str, with_meta: bool = True) -> str:
    lines = [header]
    for r in rows:
        entry = (f"[{r['date']}] {r['label']}\n"
                 f"  Q: {r['question']}\n"
                 f"  A: {r['answer'][:300].strip()}{'…' if len(r['answer']) > 300 else ''}\n")
        if with_meta:
            sources_str = ", ".join(s.get("title", "") for s in r["sources"][:3])
            conf = r["confidence"] if r["confidence"] is not None else 0.0
            entry += f"  Sources: {sources_str or 'none'} | Confidence: {conf:.0%}\n"
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


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [[b"content-type", b"application/json"],
                            [b"cache-control", b"no-store"]]})
    await send({"type": "http.response.body", "body": body})


def build_http_app():
    """ASGI app: /health, /admin/history, and the MCP endpoint at /mcp."""
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host="0.0.0.0",
    )

    async def app(scope, receive, send):
        path = scope.get("path") if scope["type"] == "http" else None
        if path in ("/", "/health"):
            tools = [t.name for t in await mcp.list_tools()]
            await _send_json(send, 200, {
                "name": "Cited",
                "status": "ok",
                "mcp_endpoint": "/mcp",
                "tools": tools,
                "usage": "Add https://<this-host>/mcp in claude.ai → Customize → Connectors",
                "warmed_newsletters": len(_pipeline._EMBED_CACHE),
            })
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
            await mcp_app(scope, receive, send)

    return app


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
            try:
                await asyncio.to_thread(_pipeline.prewarm)
            except Exception as exc:
                log.warning("Model pre-warm failed (will load on first query): %s", exc)
                return
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

        uvicorn.run(build_http_app(), host="0.0.0.0", port=port)
    else:
        asyncio.run(mcp.run_stdio_async())
