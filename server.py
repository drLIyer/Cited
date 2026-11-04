#!/usr/bin/env python3
"""
Cited — Precise, sourced answers from any Substack newsletter via MCP.

Exposes three tools to any MCP-capable Claude client
(Claude Desktop, Claude Code, Claude Cowork):

  search_substack   — hybrid search, returns ranked excerpts for Claude to cite
  list_posts        — list all public post titles + dates from a newsletter
  ask_substack      — full RAG + server-side answer via Haiku (needs API key)

Usage — Claude Code (~/.claude/settings.json):

  "mcpServers": {
    "cited": {
      "command": "/Users/liyer_1/cited/.venv/bin/python3",
      "args": ["/Users/liyer_1/cited/server.py"]
    }
  }

Usage — Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):
  Same mcpServers block.

Env vars:
  ANTHROPIC_API_KEY   — enables ask_substack tool (server-side answer generation)
"""

import asyncio
import json
import logging
import os
import sys
from typing import Optional

from mcp.server.mcpserver import MCPServer

import fetcher as _fetcher
import pipeline as _pipeline
import history as _history

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("cited")

_HTTP_MODE   = os.getenv("CITED_TRANSPORT", "stdio") == "http"
_ADMIN_TOKEN = os.getenv("CITED_ADMIN_TOKEN", "")

mcp = MCPServer(
    "Cited",
    instructions=(
        "Cited retrieves and searches Substack and Medium newsletters to give precise, sourced answers.\n\n"

        "## When the user gives a URL\n"
        "Call search_substack directly with that URL and their question.\n\n"

        "## When the user gives only an author name (no URL)\n"
        "1. Use your web search to find the author's newsletter URL. Search for: "
        '"{author name} substack" or "{author name} newsletter medium". '
        "Pick the most likely result — prefer a custom domain (e.g. news.author.com) "
        "or substack.com URL over a social media page.\n"
        "2. Tell the user which URL you found, then call search_substack with it.\n\n"

        "## Composing answers\n"
        "Always cite the article title for every claim. "
        "If confidence is below 0.5, say so and recommend the user verify with the original source. "
        "If the newsletter is paywalled or returns few results, say so honestly."
    ),
)


# ── Tool 1: search_substack ────────────────────────────────────────────────────

@mcp.tool()
async def search_substack(url: str, query: str, top_k: int = 5) -> str:
    """
    Search a Substack newsletter for content relevant to a query.

    Fetches all publicly available posts (cached for 1 hour), builds a
    hybrid BM25 + vector index, and reranks with a cross-encoder. Returns
    ranked excerpts with titles, dates, URLs, and a retrieval confidence score.

    Use this whenever a user pastes a Substack URL and asks a question about it.
    Low confidence (< 0.5) means the newsletter probably doesn't cover the topic.

    Args:
        url:   Substack newsletter URL, e.g. https://aakashgupta.substack.com
        query: The question or search query
        top_k: Number of excerpts to return (default 5, max 10)
    """
    top_k = min(int(top_k), 10)
    try:
        posts = await _fetcher.get_posts(url)
        if not posts:
            return json.dumps({
                "error": "No public posts found. The newsletter may be empty or fully paywalled.",
                "url": url,
            })

        chunks     = _pipeline.chunk_posts(posts)
        hits       = _pipeline.search(query, chunks, top_k=top_k)
        confidence = _pipeline.retrieval_confidence(hits)

        result: dict = {
            "newsletter": url,
            "posts_indexed": len(posts),
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
        _history.save(url, query, top_excerpt, result["results"], confidence, tool="search")

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
    List all publicly available posts from a Substack newsletter.

    Returns post titles, publication dates, and URLs. Useful for exploring
    what topics a newsletter covers before searching, or when a user asks
    "what has this newsletter written about?".

    Args:
        url: Substack newsletter URL, e.g. https://aakashgupta.substack.com
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
            posts      = await _fetcher.get_posts(url)
            chunks     = _pipeline.chunk_posts(posts)
            hits       = _pipeline.search(question, chunks, top_k=5)
            confidence = _pipeline.retrieval_confidence(hits)
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

            client = anthropic.Anthropic()
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=f"{system}\n\nARTICLE EXCERPTS:\n{context}",
                messages=[{"role": "user", "content": question}],
            )
            answer = msg.content[0].text

            result = {
                "question":       question,
                "answer":         answer,
                "confidence":     round(confidence, 3),
                "low_confidence": confidence < 0.5,
                "sources":        sources,
                "posts_indexed":  len(posts),
            }

            _history.save(url, question, answer, sources, confidence, tool="ask")

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

def _check_admin(token: str) -> Optional[str]:
    """Return an error string if the token is wrong, None if it's correct."""
    if not _ADMIN_TOKEN:
        return "CITED_ADMIN_TOKEN is not set on the server. Set it as an env var to enable admin history access."
    if token != _ADMIN_TOKEN:
        return "Invalid admin token."
    return None


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
        rows = _history.get(url=url, limit=min(int(limit), 100))
        if not rows:
            return f"No history for {url} yet." if url else "No history yet."

        lines = [f"{len(rows)} past Q&As" + (f" for {url}" if url else "") + ":\n"]
        for r in rows:
            sources_str = ", ".join(s.get("title", "") for s in r["sources"][:3])
            lines.append(
                f"[{r['date']}] {r['subdomain']}.substack.com\n"
                f"  Q: {r['question']}\n"
                f"  A: {r['answer'][:300].strip()}{'…' if len(r['answer']) > 300 else ''}\n"
                f"  Sources: {sources_str or 'none'}\n"
                f"  Confidence: {r['confidence']:.0%}\n"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def search_my_history(query: str) -> str:
        """
        Search your Cited history by keyword across all past questions and answers.

        Useful when you remember asking about a topic but can't recall which
        newsletter or session it was in.

        Args:
            query: Keyword or phrase to search for in past questions and answers.
        """
        rows = _history.keyword_search(query, limit=10)
        if not rows:
            return f"No history found matching '{query}'."

        lines = [f"{len(rows)} result(s) for '{query}':\n"]
        for r in rows:
            lines.append(
                f"[{r['date']}] {r['subdomain']}.substack.com\n"
                f"  Q: {r['question']}\n"
                f"  A: {r['answer'][:300].strip()}{'…' if len(r['answer']) > 300 else ''}\n"
            )
        return "\n".join(lines)

else:
    # ── Admin-only history tool (HTTP mode) ────────────────────────────────────
    # Queries from all users are saved automatically; only the admin can read them.
    # Set CITED_ADMIN_TOKEN on the server and pass it here to authenticate.

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

        limit = min(int(limit), 500)
        if query:
            rows = _history.keyword_search(query, limit=limit)
        else:
            rows = _history.get(url=url, limit=limit)

        if not rows:
            return "No history found."

        lines = [f"{len(rows)} entries" + (f" matching '{query}'" if query else "") + ":\n"]
        for r in rows:
            sources_str = ", ".join(s.get("title", "") for s in r["sources"][:3])
            lines.append(
                f"[{r['date']}] {r['subdomain']}.substack.com\n"
                f"  Q: {r['question']}\n"
                f"  A: {r['answer'][:300].strip()}{'…' if len(r['answer']) > 300 else ''}\n"
                f"  Sources: {sources_str or 'none'} | Confidence: {r['confidence']:.0%}\n"
            )
        return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────────
#
# CITED_TRANSPORT=stdio (default) — local MCP for Claude Code / Claude Desktop
# CITED_TRANSPORT=http           — hosted MCP for claude.ai Integrations
#
# When hosted, set PORT (default 8000) and ANTHROPIC_API_KEY as env vars.
# Users connect by pasting the server URL into claude.ai → Settings → Integrations.

if __name__ == "__main__":
    transport = os.getenv("CITED_TRANSPORT", "stdio")
    if transport == "http":
        import json as _json
        import uvicorn

        port = int(os.getenv("PORT", "8000"))
        log.info("Starting Cited HTTP server on port %d (path /mcp)", port)

        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            host="0.0.0.0",
        )

        # ── Live API spot-check (cached 5 min) ────────────────────────────────
        _SPOT_CHECK_URL  = "https://aakashgupta.substack.com"
        _spot_cache: dict = {"result": None, "ts": 0.0}

        async def _run_spot_check() -> dict:
            import time
            now = time.time()
            if _spot_cache["result"] and now - _spot_cache["ts"] < 300:
                return _spot_cache["result"]
            try:
                posts = await _fetcher.get_posts(_SPOT_CHECK_URL)
                result = {"posts_fetched": len(posts), "ok": len(posts) > 0}
                if len(posts) == 0:
                    result["warning"] = (
                        "Substack API spot-check returned 0 posts — "
                        "the API response format may have changed again. "
                        "Check fetcher._fetch_substack."
                    )
            except Exception as exc:
                result = {"posts_fetched": 0, "ok": False, "error": str(exc)}
            _spot_cache.update({"result": result, "ts": now})
            return result

        async def app(scope, receive, send):
            if scope["type"] == "http" and scope.get("path") in ("/", "/health"):
                import time as _time
                spot = await _run_spot_check()
                status = "ok" if spot.get("ok") else "degraded"
                body = _json.dumps({
                    "name": "Cited",
                    "status": status,
                    "mcp_endpoint": "/mcp",
                    "tools": ["search_substack", "list_posts", "admin_get_history"],
                    "usage": "Add this server in claude.ai → Settings → Connectors",
                    "api_check": spot,
                }).encode()
                http_status = 200 if status == "ok" else 503
                await send({"type": "http.response.start", "status": http_status,
                            "headers": [[b"content-type", b"application/json"]]})
                await send({"type": "http.response.body", "body": body})
            else:
                await mcp_app(scope, receive, send)

        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        asyncio.run(mcp.run_stdio_async())
