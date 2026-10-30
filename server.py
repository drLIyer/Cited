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

from mcp.server.mcpserver import MCPServer

import fetcher as _fetcher
import pipeline as _pipeline

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("cited")

mcp = MCPServer(
    "Cited",
    instructions=(
        "Cited gives Claude precise, sourced answers from any Substack newsletter. "
        "When a user pastes a Substack URL and asks a question, call search_substack "
        "with that URL and the question. Compose your answer using the returned excerpts, "
        "citing article titles for every claim. If confidence is low (< 0.5), say so and "
        "remind the reader to verify with the original source."
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

if os.getenv("ANTHROPIC_API_KEY"):

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

            return json.dumps({
                "question":       question,
                "answer":         answer,
                "confidence":     round(confidence, 3),
                "low_confidence": confidence < 0.5,
                "sources":        sources,
                "posts_indexed":  len(posts),
            }, indent=2)

        except _fetcher.NotSubstackError as exc:
            return json.dumps({"error": str(exc)})
        except _fetcher.FetchError as exc:
            return json.dumps({"error": f"Failed to fetch newsletter: {exc}"})
        except Exception as exc:
            log.exception("ask_substack error")
            return json.dumps({"error": f"Unexpected error: {exc}"})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(mcp.run_stdio_async())
