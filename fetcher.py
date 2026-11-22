"""
Fetches posts from Substack (API), Medium (RSS), and any RSS/Atom/podcast feed.
Supports *.substack.com, custom Substack domains, and medium.com publications.
Caches per-source key for CACHE_TTL seconds.

All outbound requests go through `_get`, which:
  * only allows http/https,
  * refuses hosts that resolve to private, loopback, link-local or otherwise
    non-public addresses (SSRF protection; override with CITED_ALLOW_PRIVATE_URLS=1
    for local development),
  * re-validates every redirect hop,
  * caps the response size (CITED_MAX_FETCH_BYTES, default 10 MB).

Feeds are parsed with defusedxml to block entity-expansion attacks.

LinkedIn newsletters require authentication and are not supported.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as _XMLParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring

log = logging.getLogger("cited.fetcher")

CACHE_TTL      = 3600  # 1 hour
MAX_POSTS      = int(os.getenv("CITED_MAX_POSTS", "200"))
POST_CHARS     = int(os.getenv("CITED_POST_CHARS", "15000"))
MAX_FETCH_BYTES = int(os.getenv("CITED_MAX_FETCH_BYTES", str(10 * 1024 * 1024)))
MAX_REDIRECTS  = 5
ALLOW_PRIVATE  = os.getenv("CITED_ALLOW_PRIVATE_URLS") == "1"
USER_AGENT     = "Mozilla/5.0 (compatible; Cited/1.0; +https://github.com/drliyer/cited)"

_CACHE: dict[str, tuple[list, float]] = {}

# XML namespaces used in RSS feeds
_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "atom":    "http://www.w3.org/2005/Atom",
}


class NotSubstackError(Exception):
    """The URL is not a supported or acceptable source (user-facing message)."""


class UnsafeURLError(NotSubstackError):
    """The URL points somewhere the server refuses to fetch."""


class FetchError(Exception):
    pass


@dataclass
class Post:
    title: str
    url: str
    date: str
    text: str


# ── Safe HTTP ──────────────────────────────────────────────────────────────────

def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _check_url(url: str) -> None:
    """Raise UnsafeURLError unless url is http(s) and its host resolves to public IPs only."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Only http(s) URLs are supported — got: {url}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}")
    if ALLOW_PRIVATE:
        return

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _ip_is_public(literal):
            raise UnsafeURLError("URLs pointing at private or local addresses are not allowed.")
        return

    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError(f"Invalid port in URL: {url}") from exc
    try:
        addrs = await _resolve(host, port)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host '{host}'") from exc
    if not addrs:
        raise FetchError(f"Could not resolve host '{host}'")
    for addr in addrs:
        if not _ip_is_public(ipaddress.ip_address(addr.split("%", 1)[0])):
            raise UnsafeURLError("URLs pointing at private or local addresses are not allowed.")


async def _get(client: httpx.AsyncClient, url: str,
               headers: dict | None = None) -> httpx.Response:
    """GET with SSRF checks on every hop and a response-size cap.

    Raises httpx.HTTPStatusError / httpx.RequestError like client.get would,
    plus UnsafeURLError and FetchError.
    """
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    for _ in range(MAX_REDIRECTS + 1):
        await _check_url(url)
        async with client.stream("GET", url, headers=hdrs) as resp:
            if resp.is_redirect:
                loc = resp.headers.get("location")
                if not loc:
                    raise FetchError(f"Redirect without a Location header from {url}")
                url = urljoin(str(resp.url), loc)
                continue
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_FETCH_BYTES:
                raise FetchError(f"Response from {url} is too large.")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_FETCH_BYTES:
                    raise FetchError(f"Response from {url} is too large.")
            return httpx.Response(resp.status_code, headers=resp.headers,
                                  content=bytes(buf), request=resp.request)
    raise FetchError(f"Too many redirects fetching {url}")


def _client() -> httpx.AsyncClient:
    # Redirects are followed manually in _get so every hop is validated.
    return httpx.AsyncClient(timeout=30, follow_redirects=False)


def _parse_xml(content: bytes, url: str) -> ET.Element:
    try:
        return _xml_fromstring(content)
    except (_XMLParseError, DefusedXmlException) as exc:
        raise FetchError(f"Could not parse feed at {url}: {exc}") from exc


def _first(*elements):
    """Return the first element that is not None.

    Note: `a or b` is wrong for ElementTree elements — an element with no
    children is falsy, so `item.find("pubDate") or ...` silently drops it.
    """
    for el in elements:
        if el is not None:
            return el
    return None


# ── Source detection ───────────────────────────────────────────────────────────

_RSS_URL_RE = re.compile(
    r"\.xml$|\.rss$|\.atom$|/feed/?$|/rss/?$|/atom/?$|/feed/|"
    r"feeds\.|feedburner\.|simplecast\.com|buzzsprout\.com|"
    r"transistor\.fm|libsyn\.com|podbean\.com|megaphone\.fm|"
    r"captivate\.fm|castos\.com|redcircle\.com|anchor\.fm|"
    r"rss\.com/podcasts|pod\.link",
    re.I,
)


def _host(url: str) -> str:
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def _host_is(url: str, domain: str) -> bool:
    h = _host(url)
    return h == domain or h.endswith("." + domain)


def _detect_source(url: str) -> str:
    """Return 'substack', 'medium', 'rss', or raise for unsupported sources."""
    if _host_is(url, "linkedin.com"):
        raise NotSubstackError(
            "LinkedIn newsletters require authentication and aren't publicly "
            "accessible via any API. Try Substack or Medium instead."
        )
    if _host_is(url, "medium.com"):
        return "medium"
    if _RSS_URL_RE.search(url.lower()):
        return "rss"
    # Substack API works on substack.com subdomains and custom domains
    return "substack"


def parse_subdomain(url: str) -> str:
    """Return a stable cache/storage key for this newsletter.

    *.substack.com  → subdomain only  (e.g. 'aakashgupta')
    Custom domain   → full hostname   (e.g. 'news.aakashg.com')
    Medium          → 'medium:<path>' (e.g. 'medium:@username')
    RSS feed        → 'rss:<host>/<path>'
    """
    url = url.strip().rstrip("/")

    if _host_is(url, "medium.com"):
        m = re.match(r"https?://(?:www\.)?medium\.com(/[^?#]*)?", url, re.I)
        path = ((m.group(1) if m else "") or "").strip("/")
        return f"medium:{path}" if path else "medium:home"

    m = re.match(r"https?://([^./]+)\.substack\.com", url, re.I)
    if m:
        return m.group(1).lower()

    m = re.match(r"https?://([^/?#]+)(/[^?#]*)?", url, re.I)
    if m:
        host = m.group(1).lower()
        path = (m.group(2) or "").strip("/")
        if "." in host:
            # RSS/generic feed → use full host+path as key to avoid collisions
            if _RSS_URL_RE.search(url):
                return f"rss:{host}/{path}".rstrip("/")
            return host

    raise NotSubstackError(
        f"Provide a URL like https://author.substack.com, "
        f"https://medium.com/@author, https://news.custom.com, "
        f"or any RSS/podcast feed URL — got: {url}"
    )


def display_name(key: str) -> str:
    """Human-readable label for a cache key (inverse-ish of parse_subdomain)."""
    if ":" in key or "." in key:
        return key
    return f"{key}.substack.com"


def _api_base(key: str) -> str:
    """API base URL for a Substack newsletter key."""
    if "." not in key:
        return f"https://{key}.substack.com"
    return f"https://{key}"


# ── HTML / text helpers ────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, ch in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


# ── Substack fetcher ───────────────────────────────────────────────────────────

async def _fetch_substack(key: str) -> list[Post]:
    base   = _api_base(key)
    posts: list[Post] = []
    cursor: str | None = None
    offset = 0
    limit  = 25

    async with _client() as client:
        while True:
            api = f"{base}/api/v1/posts?limit={limit}"
            if cursor:
                api += f"&after={cursor}"
            elif offset:
                api += f"&offset={offset}"
            try:
                resp = await _get(client, api)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise NotSubstackError(
                        f"'{key}' doesn't appear to be a Substack newsletter "
                        "(API returned 404). Try the .substack.com URL directly."
                    ) from exc
                raise FetchError(
                    f"Substack API {exc.response.status_code} for '{key}'"
                ) from exc
            except httpx.RequestError as exc:
                raise FetchError(f"Network error: {exc}") from exc

            try:
                data = resp.json()
            except Exception as exc:
                raise NotSubstackError(
                    f"'{key}' doesn't appear to be a Substack newsletter "
                    "(API returned non-JSON)."
                ) from exc

            # API returns {"posts": [...], "nextCursor": "..."} or a plain list
            # (the plain-list form paginates by offset).
            if isinstance(data, dict):
                batch  = data.get("posts") or []
                cursor = data.get("nextCursor") or None
                more   = cursor is not None
            elif isinstance(data, list):
                batch  = data
                cursor = None
                offset += len(batch)
                more   = len(batch) == limit
            else:
                break

            if not batch:
                break

            for p in batch:
                if not isinstance(p, dict):
                    continue
                body = p.get("body_html") or p.get("truncated_body_text") or ""
                text = _strip_html(body)
                if len(text) < 60:
                    continue
                posts.append(Post(
                    title=p.get("title") or "Untitled",
                    url=(p.get("canonical_url") or f"{base}/p/{p.get('slug', '')}"),
                    date=(p.get("post_date") or "")[:10],
                    text=text[:POST_CHARS],
                ))

            if not more or len(posts) >= MAX_POSTS:
                break

    log.info("Fetched %d posts from %s (Substack)", len(posts), key)
    return posts[:MAX_POSTS]


# ── Medium fetcher (RSS) ───────────────────────────────────────────────────────

def _medium_rss_url(url: str) -> str:
    """Convert a Medium profile/publication URL to its RSS feed URL."""
    url = url.strip().rstrip("/")
    m = re.match(r"(https?://(?:www\.)?medium\.com)(/[^?#]*)?", url, re.I)
    if m:
        base = m.group(1)
        path = (m.group(2) or "").rstrip("/")
        if path and not path.startswith("/feed"):
            return f"{base}/feed{path}"
        return f"{base}{path}" if path else f"{base}/feed"
    # Custom Medium domain (e.g. publication.medium.com) — try /feed
    m = re.match(r"(https?://[^/]+)", url, re.I)
    if m:
        return f"{m.group(1)}/feed"
    return f"{url}/feed"


def _text_from_rss_item(item: ET.Element) -> str:
    """Extract best available text from an RSS <item>."""
    # content:encoded is the full HTML body
    encoded = item.find("content:encoded", _NS)
    if encoded is not None and encoded.text:
        return _strip_html(encoded.text)
    # Fall back to description
    desc = item.find("description")
    if desc is not None and desc.text:
        return _strip_html(desc.text)
    return ""


async def _fetch_feed_bytes(url: str, label: str) -> bytes:
    async with _client() as client:
        try:
            resp = await _get(client, url)
        except httpx.HTTPStatusError as exc:
            raise FetchError(
                f"{label} returned {exc.response.status_code} for {url}."
            ) from exc
        except httpx.RequestError as exc:
            raise FetchError(f"Network error fetching {label}: {exc}") from exc
    return resp.content


def _parse_rss_items(channel: ET.Element) -> list[Post]:
    posts: list[Post] = []
    for item in channel.findall("item")[:MAX_POSTS]:
        title_el = item.find("title")
        link_el  = item.find("link")
        date_el  = _first(item.find("pubDate"), item.find("dc:date", _NS))
        guid_el  = item.find("guid")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link  = (link_el.text or "").strip() if link_el is not None else ""
        # Podcast feeds often omit <link>; use guid when it's a permalink
        if not link and guid_el is not None:
            if guid_el.get("isPermaLink", "true").lower() != "false":
                link = (guid_el.text or "").strip()
        date = (date_el.text or "").strip() if date_el is not None else ""

        text = _text_from_rss_item(item)
        # Fallback: itunes:summary for sparse podcast feeds
        if len(text) < 60:
            itunes_summary = item.find(f"{{{_ITUNES_NS}}}summary")
            if itunes_summary is not None and itunes_summary.text:
                text = _strip_html(itunes_summary.text)

        if len(text) >= 60:
            posts.append(Post(title=title or "Untitled", url=link,
                              date=_normalize_date(date), text=text[:POST_CHARS]))
    return posts


def _normalize_date(raw: str) -> str:
    """ISO dates → YYYY-MM-DD; RFC 822 (RSS pubDate) → YYYY-MM-DD; else as-is."""
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    if raw:
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(raw).date().isoformat()
        except (TypeError, ValueError, IndexError):
            pass
    return raw


async def _fetch_medium(url: str) -> list[Post]:
    rss_url = _medium_rss_url(url)
    log.info("Fetching Medium RSS: %s", rss_url)
    content = await _fetch_feed_bytes(rss_url, "Medium RSS")
    root = _parse_xml(content, rss_url)

    channel = root.find("channel")
    if channel is None:
        channel = root
    posts = [p for p in _parse_rss_items(channel) if p.url]

    log.info("Fetched %d posts from %s (Medium RSS)", len(posts), url)
    return posts


# ── Generic RSS / Atom / podcast fetcher ──────────────────────────────────────

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM_NS   = "http://www.w3.org/2005/Atom"


def _atom_link(entry: ET.Element, tag) -> str:
    links = entry.findall(tag("link"))
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href").strip()
    for link in links:
        if link.get("href"):
            return link.get("href").strip()
    return ""


async def _fetch_rss(url: str) -> list[Post]:
    """Fetch posts/episodes from any RSS 2.0, Atom, or podcast feed URL."""
    log.info("Fetching RSS feed: %s", url)
    content = await _fetch_feed_bytes(url, "RSS feed")

    if content.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html")):
        raise NotSubstackError(
            "The URL returned an HTML page, not a feed. "
            "Try the direct RSS feed URL (often /feed or /rss)."
        )
    root = _parse_xml(content, url)

    # Detect HTML redirect/login page that happens to be well-formed XML
    if root.tag.lower() in ("html", "{http://www.w3.org/1999/xhtml}html"):
        raise NotSubstackError(
            "The URL returned an HTML page, not a feed. "
            "Try the direct RSS feed URL (often /feed or /rss)."
        )

    posts: list[Post] = []

    # ── Atom 1.0 ──
    if root.tag in (f"{{{_ATOM_NS}}}feed", "feed"):
        ns = _ATOM_NS if root.tag.startswith("{") else ""
        tag = (lambda t: f"{{{ns}}}{t}") if ns else (lambda t: t)
        for entry in root.findall(tag("entry"))[:MAX_POSTS]:
            title_el   = entry.find(tag("title"))
            updated_el = _first(entry.find(tag("published")), entry.find(tag("updated")))
            content_el = _first(entry.find(tag("content")), entry.find(tag("summary")))

            title = (title_el.text or "").strip() if title_el is not None else ""
            link  = _atom_link(entry, tag)
            date  = _normalize_date(updated_el.text or "") if updated_el is not None else ""
            text  = _strip_html(content_el.text or "") if content_el is not None else ""

            if len(text) >= 60:
                posts.append(Post(title=title or "Untitled", url=link, date=date,
                                  text=text[:POST_CHARS]))

    else:
        # ── RSS 2.0 (includes podcast feeds) ──
        channel = root.find("channel")
        if channel is None:
            channel = root
        posts = _parse_rss_items(channel)

    log.info("Fetched %d posts/episodes from %s (RSS)", len(posts), url)
    return posts


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_posts(url: str) -> list[Post]:
    """Return posts for a newsletter URL, using in-process cache."""
    url    = url.strip()
    source = _detect_source(url)   # raises NotSubstackError for LinkedIn etc.
    key    = parse_subdomain(url)
    await _check_url(url)          # reject unsafe URLs before touching the cache

    cached = _CACHE.get(key)
    if cached and time.time() - cached[1] < CACHE_TTL:
        log.info("Cache hit for %s", key)
        return cached[0]

    if source == "medium":
        posts = await _fetch_medium(url)
    elif source == "rss":
        posts = await _fetch_rss(url)
    else:
        posts = await _fetch_substack(key)

    _CACHE[key] = (posts, time.time())
    return posts


def invalidate(url: str) -> None:
    try:
        _CACHE.pop(parse_subdomain(url), None)
    except NotSubstackError:
        pass
