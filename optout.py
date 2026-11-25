"""
Respect writers' AI opt-outs.

Before Cited indexes a newsletter (library) or fetches it for a search (live),
it reads the site's robots.txt. If the writer has told AI crawlers to stay away,
Cited doesn't use the site — and the library deletes anything already stored.

Policies (CITED_OPTOUT_POLICY):
  strict (default)  Skip the site if robots.txt blocks ANY major AI crawler —
                    training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot…)
                    or assistant/answer crawlers (ChatGPT-User, Claude-User,
                    PerplexityBot…). This is how a writer would read "block AI".
  retrieval         Skip only if AI assistant/answer crawlers are blocked (Cited
                    is retrieval for AI answers, not model training).

Besides robots.txt, Cited honours the other common AI opt-out signals (any
policy): `noai` / `noimageai` in a <meta name="robots"> tag or X-Robots-Tag
header, and the TDM Reservation Protocol (<meta name="tdm-reservation"
content="1"> or /.well-known/tdmrep.json) — the EU standard for opting out of
text and data mining.

Either way:
  * rules for "Cited" itself, or for everyone ("User-agent: *"), are honoured;
  * Content Signals (Cloudflare): `ai-input=no` blocks; `ai-input=yes` is an
    explicit yes for retrieval; `ai-train=no` blocks only under strict;
  * no robots.txt (404) means no opt-out;
  * a writer's explicit permission ("permission": "granted" in library.json)
    overrides the check.

Parsing follows RFC 9309: groups of User-agent lines, longest-match Allow /
Disallow with `*` and `$` wildcards, Allow wins ties.

Set CITED_RESPECT_AI_OPTOUT=0 to disable (not recommended).
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("cited.optout")

CACHE_SECONDS = 24 * 3600

RETRIEVAL_AGENTS = ["ChatGPT-User", "OAI-SearchBot", "Claude-User", "Claude-SearchBot",
                    "PerplexityBot", "Perplexity-User"]
TRAINING_AGENTS = ["GPTBot", "ClaudeBot", "anthropic-ai", "Claude-Web", "Google-Extended", "CCBot",
                   "Applebot-Extended", "Bytespider", "cohere-ai", "cohere-training-data-crawler",
                   "meta-externalagent", "Diffbot", "Omgilibot", "Timpibot", "ImagesiftBot"]
SELF_AGENT = "Cited"
CONTENT_PATHS = ["/", "/p/example-post", "/feed"]

_cache: dict[str, tuple[float, "Decision"]] = {}


class OptOutCheckError(Exception):
    """robots.txt couldn't be read (network/server error) — decision unknown."""


@dataclass
class Decision:
    allowed: bool
    reason: str
    blocked_agents: list[str] = field(default_factory=list)
    checked_at: float = 0.0


def enabled() -> bool:
    return os.getenv("CITED_RESPECT_AI_OPTOUT", "1") != "0"


def policy() -> str:
    return "retrieval" if os.getenv("CITED_OPTOUT_POLICY", "strict").lower() == "retrieval" else "strict"


# ── robots.txt parsing (RFC 9309) ─────────────────────────────────────────────

@dataclass
class _Group:
    agents: list[str]
    rules: list[tuple[bool, str]]     # (allow?, path pattern)


def parse(text: str) -> tuple[list[_Group], dict[str, str]]:
    """Return (groups, content_signals). Content-Signal lines are collected globally."""
    groups: list[_Group] = []
    signals: dict[str, str] = {}
    current: Optional[_Group] = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not last_was_agent:
                current = _Group(agents=[], rules=[])
                groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if key in ("allow", "disallow") and current is not None:
            if value or key == "allow":
                current.rules.append((key == "allow", value))
        elif key == "content-signal":
            for part in value.split(","):
                if "=" in part:
                    k, v = (s.strip().lower() for s in part.split("=", 1))
                    signals[k] = v
    return groups, signals


def _group_for(groups: list[_Group], agent: str) -> Optional[_Group]:
    agent = agent.lower()
    specific = [g for g in groups if agent in g.agents]
    if specific:   # RFC 9309: merge all groups naming this agent
        return _Group(agents=[agent], rules=[r for g in specific for r in g.rules])
    star = [g for g in groups if "*" in g.agents]
    if star:
        return _Group(agents=["*"], rules=[r for g in star for r in g.rules])
    return None


def _pattern_re(pattern: str) -> re.Pattern:
    end = pattern.endswith("$")
    body = re.escape(pattern[:-1] if end else pattern).replace(r"\*", ".*")
    return re.compile("^" + body + ("$" if end else ""))


def is_allowed(groups: list[_Group], agent: str, path: str) -> bool:
    g = _group_for(groups, agent)
    if g is None:
        return True
    best: Optional[tuple[int, bool]] = None
    for allow, pat in g.rules:
        if _pattern_re(pat).match(path):
            score = (len(pat), allow)                 # longest match; Allow wins ties
            if best is None or score > best:
                best = score
    return True if best is None else best[1]


def evaluate(robots_text: str, feed_path: Optional[str] = None,
             pol: Optional[str] = None) -> Decision:
    """Decide whether Cited may use this site, from its robots.txt text."""
    pol = pol or policy()
    groups, signals = parse(robots_text)
    paths = CONTENT_PATHS + ([feed_path] if feed_path else [])

    def blocked(agent: str) -> bool:
        return any(not is_allowed(groups, agent, p) for p in paths)

    if signals.get("ai-input") == "no":
        return Decision(False, "The site's Content-Signal says no to AI input (ai-input=no).", ["ai-input=no"])
    def named(agent: str) -> bool:       # robots.txt has a group naming this agent
        return any(agent.lower() in g.agents for g in groups)

    if blocked(SELF_AGENT):
        who = "Cited" if named(SELF_AGENT) else "all crawlers"
        return Decision(False, f"robots.txt blocks {who}.", [SELF_AGENT])
    if signals.get("ai-input") == "yes":
        return Decision(True, "The site explicitly allows AI input (ai-input=yes).")

    agents = RETRIEVAL_AGENTS + (TRAINING_AGENTS if pol == "strict" else [])
    hit = [a for a in agents if named(a) and blocked(a)]
    if hit:
        return Decision(False, "robots.txt blocks AI crawlers (" + ", ".join(hit[:4])
                        + (", …" if len(hit) > 4 else "") + ").", hit)
    if pol == "strict" and signals.get("ai-train") == "no":
        return Decision(False, "The site's Content-Signal says no to AI training (ai-train=no).", ["ai-train=no"])
    return Decision(True, "No AI opt-out found in robots.txt.")


# ── Page-level and TDMRep signals ────────────────────────────────────────────

_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "")
            for m in _ATTR_RE.finditer(tag)}


def evaluate_page(headers: dict, html: str) -> Optional[Decision]:
    """noai / tdm-reservation signals on a page (header or meta). None if no opt-out."""
    xrt = ",".join(v for k, v in headers.items() if k.lower() == "x-robots-tag").lower()
    if re.search(r"\bnoai\b|\bnoimageai\b", xrt):
        return Decision(False, "The site's X-Robots-Tag header says noai.", ["noai"])
    for tag in _META_RE.findall(html[:300_000]):
        a = _attrs(tag)
        name, content = a.get("name", "").lower(), a.get("content", "").lower()
        if name in ("robots", "cited") and re.search(r"\bnoai\b|\bnoimageai\b", content):
            return Decision(False, f'The site\'s <meta name="{name}"> tag says noai.', ["noai"])
        if name == "tdm-reservation" and content.strip() == "1":
            return Decision(False, "The site reserves text & data mining rights (tdm-reservation=1).",
                            ["tdm-reservation"])
    return None


def evaluate_tdmrep(data) -> Optional[Decision]:
    """/.well-known/tdmrep.json: a reservation covering the whole site → opt-out."""
    if not isinstance(data, list):
        return None
    for rule in data:
        if not isinstance(rule, dict):
            continue
        loc = str(rule.get("location", "")).strip()
        if str(rule.get("tdm-reservation", "")) == "1" and loc in ("/", "*", "/*", ""):
            return Decision(False, "The site reserves text & data mining rights (tdmrep.json).",
                            ["tdm-reservation"])
    return None


# ── Fetching & caching ────────────────────────────────────────────────────────

async def check(url: str, feed_path: Optional[str] = None, force: bool = False) -> Decision:
    """Fetch and evaluate the site's robots.txt (cached per host for 24 h)."""
    import fetcher as _fetcher            # local import: fetcher imports this module lazily too
    parts = urlsplit(_fetcher.upgrade_to_https(url))
    host = (parts.hostname or "").lower()
    cache_key = f"{host}|{feed_path or ''}|{policy()}"
    hit = _cache.get(cache_key)
    if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    robots_url = f"https://{parts.netloc}/robots.txt"
    try:
        async with _fetcher._client() as client:
            resp = await _fetcher._get(client, robots_url)
        text = resp.content[:512_000].decode("utf-8", "replace")
        decision = evaluate(text, feed_path)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            # RFC 9309 says "allow", but an explicit refusal to show robots.txt is
            # ambiguous; be conservative and treat it as unknown.
            raise OptOutCheckError(f"robots.txt returned {code} for {host}") from exc
        if 400 <= code < 500:
            decision = Decision(True, "The site has no robots.txt.")
        else:
            raise OptOutCheckError(f"robots.txt returned {code} for {host}") from exc
    except (httpx.RequestError, _fetcher.FetchError) as exc:
        raise OptOutCheckError(f"Couldn't read robots.txt for {host}: {exc}") from exc
    if decision.allowed:
        decision = await _page_signals(parts, decision)
    decision.checked_at = time.time()
    _cache[cache_key] = (decision.checked_at, decision)
    if not decision.allowed:
        log.info("AI opt-out for %s: %s", host, decision.reason)
    return decision


async def _page_signals(parts, robots_decision: Decision) -> Decision:
    """Check the home page (noai / tdm-reservation) and tdmrep.json. A page that
    can't be fetched isn't an opt-out — the content fetch will fail on its own."""
    import fetcher as _fetcher
    root = f"https://{parts.netloc}"
    async with _fetcher._client() as client:
        try:
            resp = await _fetcher._get(client, root + "/")
            found = evaluate_page(dict(resp.headers), resp.content[:300_000].decode("utf-8", "replace"))
            if found:
                return found
        except (httpx.HTTPError, _fetcher.FetchError):
            pass
        try:
            resp = await _fetcher._get(client, root + "/.well-known/tdmrep.json")
            found = evaluate_tdmrep(resp.json())
            if found:
                return found
        except (httpx.HTTPError, _fetcher.FetchError, ValueError):
            pass
    return robots_decision


def clear_cache() -> None:
    _cache.clear()
