"""
"Bring your subscription": search creators' own MCP servers with the user's key.

Some creators give paying members a key to their own MCP server (e.g. The
Growth Podcast members MCP). Cited can include those servers in a search —
using the *user's* key, for *that request only*:

  * The user adds their key to their Cited connector as a request header
    (claude.ai: Customize → Connectors → Cited → Request headers; Claude Code:
    `claude mcp add … --header "X-Cited-Key-Growth-Podcast: <key>"`).
    In local (stdio) mode it can come from an environment variable instead.
  * Cited reads the header for the current request, calls the creator's MCP
    server with it, and merges those passages with the library's.
  * Keys are never stored, logged, or sent anywhere except that creator's
    configured server (HTTPS only, no redirects). They're scrubbed from errors.
  * The user's question is sent to the creator's server — results say so.

Each subscription is a library.json entry with "type": "mcp":
  {
    "name": "The Growth Podcast — members MCP",
    "type": "mcp",
    "mcp_url": "https://growth-podcast-mcp.aakashg.workers.dev/mcp",
    "key_header": "X-Cited-Key-Growth-Podcast",       # header the user sets on Cited
    "key_env": "CITED_KEY_GROWTH_PODCAST",            # local-mode alternative
    "auth": {"header": "Authorization", "scheme": "Bearer"},   # how the creator wants it
    "search_tool": null, "query_arg": null,           # optional; auto-detected if null
    "aliases": [...], "attribution": {...}
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from urllib.parse import urlsplit

import httpx

import fetcher as _fetcher

log = logging.getLogger("cited.subscriptions")

PROTOCOL_VERSION = "2025-06-18"
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 25.0
TOOLS_CACHE_SECONDS = 3600
_tools_cache: dict[str, tuple[float, list[dict]]] = {}


class SubscriptionError(Exception):
    """A subscription source couldn't be searched (message is safe to show the user)."""


def load(sources: list[dict]) -> list[dict]:
    """Normalise "type": "mcp" entries from library.json."""
    subs = []
    for s in sources:
        url = (s.get("mcp_url") or "").strip()
        header = (s.get("key_header") or "").strip()
        if not url or not header:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", s["name"].lower()).strip("-")
        auth = s.get("auth") or {}
        subs.append({
            "key": "sub:" + slug, "name": s["name"], "url": url, "mcp_url": url,
            "key_header": header.lower(), "key_env": s.get("key_env"),
            "auth_header": auth.get("header", "Authorization"), "auth_scheme": auth.get("scheme", "Bearer"),
            "search_tool": s.get("search_tool"), "query_arg": s.get("query_arg"),
            "aliases": s.get("aliases", []), "attribution": s.get("attribution"),
            "kind": s.get("kind", "newsletter"), "homepage": s.get("homepage") or "",
        })
    return subs


def user_key(sub: dict, headers: Optional[dict], local_mode: bool) -> Optional[str]:
    """The user's key for this subscription: request header, or env var in local mode."""
    if headers:
        for k, v in headers.items():
            if k.lower() == sub["key_header"] and v.strip():
                return v.strip()
    if local_mode and sub.get("key_env"):
        v = os.getenv(sub["key_env"], "").strip()
        return v or None
    return None


# ── Minimal MCP client (Streamable HTTP, JSON-RPC) ────────────────────────────

class _Session:
    def __init__(self, sub: dict, key: str):
        self.sub, self.key = sub, key
        self.session_id: Optional[str] = None
        self._id = 0

    def _scrub(self, text: str) -> str:
        return text.replace(self.key, "[key]") if self.key else text

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL_VERSION,
             self.sub["auth_header"]: (f"{self.sub['auth_scheme']} {self.key}".strip()
                                       if self.sub["auth_scheme"] else self.key)}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _post(self, client: httpx.AsyncClient, payload: dict) -> Optional[dict]:
        try:
            resp = await client.post(self.sub["mcp_url"], json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SubscriptionError(self._scrub(f"Couldn't reach {self.sub['name']}: {type(exc).__name__}")) from None
        if resp.status_code in (401, 403):
            raise SubscriptionError(f"{self.sub['name']} rejected your key (HTTP {resp.status_code}). "
                                    "Check the key in your Cited connector's request headers.")
        if resp.is_redirect:
            raise SubscriptionError(f"{self.sub['name']} tried to redirect; Cited won't send your key elsewhere.")
        if resp.status_code >= 400:
            raise SubscriptionError(f"{self.sub['name']} returned HTTP {resp.status_code}.")
        if len(resp.content) > MAX_BYTES:
            raise SubscriptionError(f"{self.sub['name']} returned too much data.")
        self.session_id = resp.headers.get("mcp-session-id") or self.session_id
        if "id" not in payload:
            return None                                # notification
        return _parse_rpc(resp, payload["id"])

    async def request(self, client, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        msg = await self._post(client, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if not msg:
            raise SubscriptionError(f"{self.sub['name']} sent no response to {method}.")
        if "error" in msg:
            raise SubscriptionError(self._scrub(f"{self.sub['name']}: {msg['error'].get('message', 'error')}"))
        return msg.get("result")

    async def open(self, client) -> None:
        await self.request(client, "initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "Cited", "version": "1.0"}})
        await self._post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def _parse_rpc(resp: httpx.Response, want_id: int) -> Optional[dict]:
    ctype = resp.headers.get("content-type", "")
    text = resp.content.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        for block in text.split("\n\n"):
            data = "\n".join(l[5:].strip() for l in block.splitlines() if l.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        return None
    try:
        msg = json.loads(text) if text.strip() else None
    except ValueError:
        raise SubscriptionError("The creator's server sent an unreadable response.") from None
    return msg if isinstance(msg, dict) else None


def pick_tool(tools: list[dict], sub: dict) -> tuple[str, str]:
    """Choose the remote search tool and the argument that takes the question."""
    by_name = {t.get("name"): t for t in tools}
    tool = by_name.get(sub.get("search_tool")) if sub.get("search_tool") else None
    if tool is None:
        candidates = [t for t in tools if "search" in (t.get("name") or "").lower()] or \
                     [t for t in tools if re.search(r"ask|query|find", t.get("name") or "", re.I)]
        if not candidates:
            raise SubscriptionError(f"{sub['name']} has no search tool Cited can use.")
        tool = sorted(candidates, key=lambda t: len(t.get("name") or ""))[0]
    if sub.get("query_arg"):
        return tool["name"], sub["query_arg"]
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    strings = [k for k, v in props.items() if (v or {}).get("type") == "string"]
    for pref in ("query", "q", "question", "search", "text", "topic", "prompt"):
        if pref in strings:
            return tool["name"], pref
    if strings:
        return tool["name"], strings[0]
    raise SubscriptionError(f"Couldn't tell how to pass a question to {sub['name']}'s {tool['name']} tool.")


async def search(sub: dict, key: str, question: str, max_passages: int = 10) -> list[dict]:
    """Search one subscription with the user's key → passages [{title, url, date, text}]."""
    parts = urlsplit(sub["mcp_url"])
    if parts.scheme != "https":
        raise SubscriptionError(f"{sub['name']}'s server isn't HTTPS; Cited won't send your key to it.")
    await _fetcher._check_url(sub["mcp_url"])
    session = _Session(sub, key)
    async with _fetcher._client() as client:
        client.timeout = httpx.Timeout(TIMEOUT)
        await session.open(client)
        cached = _tools_cache.get(sub["key"])
        if cached and time.time() - cached[0] < TOOLS_CACHE_SECONDS:
            tools = cached[1]
        else:
            tools = (await session.request(client, "tools/list") or {}).get("tools") or []
            _tools_cache[sub["key"]] = (time.time(), tools)
        tool, arg = pick_tool(tools, sub)
        result = await session.request(client, "tools/call", {"name": tool, "arguments": {arg: question}})
    if (result or {}).get("isError"):
        raise SubscriptionError(f"{sub['name']}'s search returned an error.")
    return to_passages(result or {}, sub)[:max_passages]


_TEXT_KEYS  = ("excerpt", "text", "snippet", "content", "passage", "quote", "summary", "body")
_TITLE_KEYS = ("title", "episode_title", "post_title", "name", "episode")
_URL_KEYS   = ("url", "link", "youtube_url", "source_url", "permalink", "substack_url")


def _first(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _items(obj) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in ("results", "hits", "matches", "items", "passages", "episodes", "data"):
            if isinstance(obj.get(k), list):
                return _items(obj[k])
        return [obj]
    return []


def to_passages(result: dict, sub: dict) -> list[dict]:
    """Turn a remote tool result (structured or text) into passages."""
    payloads: list = []
    if isinstance(result.get("structuredContent"), (dict, list)):
        payloads.append(result["structuredContent"])
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text", "")
            try:
                payloads.append(json.loads(txt))
            except ValueError:
                payloads.append(txt)
    out = []
    for p in payloads:
        if isinstance(p, str):
            for para in [x.strip() for x in re.split(r"\n\s*\n", p) if len(x.strip()) >= 40]:
                out.append({"title": sub["name"], "url": sub.get("homepage") or "", "date": "", "text": para[:2000]})
            continue
        for item in _items(p):
            text = _first(item, _TEXT_KEYS)
            if len(text) < 20:
                continue
            out.append({"title": _first(item, _TITLE_KEYS) or sub["name"],
                        "url": _first(item, _URL_KEYS) or sub.get("homepage") or "",
                        "date": str(item.get("date") or "")[:10], "text": text[:2000]})
    return out
