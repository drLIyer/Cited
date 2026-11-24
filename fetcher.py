"""
Fetches posts from Substack (API), Medium (RSS), and any RSS/Atom/podcast feed.
Supports *.substack.com, custom Substack domains, and medium.com publications.
Caches per-source key for CACHE_TTL seconds.

All outbound requests go through `_get`, which:
  * HTTPS only — a pasted http:// URL is upgraded to https:// before any
    request, and redirects to plain http:// are refused,
  * refuses hosts that resolve to private, loopback, link-local or otherwise
    non-public addresses (SSRF protection; override with CITED_ALLOW_PRIVATE_URLS=1
    for local development),
  * re-validates every redirect hop,
  * pins each TCP connection to an address that was validated at connect
    time (closes the DNS-rebinding gap between "check" and "connect"),
  * caps the response size (CITED_MAX_FETCH_BYTES, default 10 MB).

Outbound proxies from the environment (HTTPS_PROXY etc.) are ignored by
default, because a proxy resolves DNS itself and would defeat pinning. Set
CITED_USE_PROXY=1 to use them; then only the pre-request checks apply.

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
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpcore
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
USE_PROXY      = os.getenv("CITED_USE_PROXY") == "1"
USER_AGENT     = "Mozilla/5.0 (compatible; Cited/1.0; +https://github.com/drLIyer/Cited)"

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


class UnsafeContentError(FetchError):
    """The source returned content we refuse to process (XML attack, oversized body)."""


class SourcePausedError(FetchError):
    """Fetching from this host is paused for a while after repeated failures."""


# host → unix time until which fetching is paused (set by the auto-fix agent)
_PAUSED: dict[str, float] = {}


def pause_host(host: str, seconds: float) -> None:
    _PAUSED[host.lower()] = time.time() + seconds


def unpause_host(host: str) -> None:
    _PAUSED.pop(host.lower(), None)


def paused_hosts() -> dict[str, float]:
    now = time.time()
    for h in [h for h, t in _PAUSED.items() if t <= now]:
        del _PAUSED[h]
    return dict(_PAUSED)


def invalidate_key(key: str) -> None:
    _CACHE.pop(key, None)


@dataclass
class Post:
    title: str
    url: str
    date: str
    text: str
    audience: str = ""      # Substack: everyone / only_paid / founding / only_free


_PAID_AUDIENCES = {"only_paid", "founding"}


# ── Safe HTTP ──────────────────────────────────────────────────────────────────

def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


_PRIVATE_MSG = "URLs pointing at private or local addresses are not allowed."


async def _resolve_public(host: str, port: int) -> list[str]:
    """Resolve host and return its addresses, raising UnsafeURLError if ANY
    of them is non-public (a mixed answer is a classic rebinding setup)."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not ALLOW_PRIVATE and not _ip_is_public(literal):
            raise UnsafeURLError(_PRIVATE_MSG)
        return [str(literal)]

    try:
        addrs = await _resolve(host, port)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host '{host}'") from exc
    if not addrs:
        raise FetchError(f"Could not resolve host '{host}'")
    addrs = [a.split("%", 1)[0] for a in addrs]
    if not ALLOW_PRIVATE:
        for addr in addrs:
            if not _ip_is_public(ipaddress.ip_address(addr)):
                raise UnsafeURLError(_PRIVATE_MSG)
    return list(dict.fromkeys(addrs))


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that resolves + validates the host at connect time and
    connects to the validated IP. TLS still verifies the certificate against
    the original hostname (httpcore takes SNI from the request URL)."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        addrs = await _resolve_public(host, port)
        last_exc: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(
                    addr, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_exc = exc
        raise last_exc or httpcore.ConnectError(f"Could not connect to {host}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise UnsafeURLError("Unix sockets are not allowed.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        # httpx/httpcore internals changed — fail loudly rather than silently unpinned.
        raise RuntimeError("Cannot install pinned network backend on this httpx version")
    pool._network_backend = _PinnedBackend()
    return transport


async def _check_url(url: str) -> None:
    """Raise UnsafeURLError unless url is http(s) and its host resolves to public IPs only."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeURLError(f"Only https:// URLs are supported — got: {url}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}")
    if ALLOW_PRIVATE:
        return
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeURLError(f"Invalid port in URL: {url}") from exc
    await _resolve_public(host, port)


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
                raise UnsafeContentError(f"Response from {url} is too large.")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_FETCH_BYTES:
                    raise UnsafeContentError(f"Response from {url} is too large.")
            return httpx.Response(resp.status_code, headers=resp.headers,
                                  content=bytes(buf), request=resp.request)
    raise FetchError(f"Too many redirects fetching {url}")


def _client() -> httpx.AsyncClient:
    # Redirects are followed manually in _get so every hop is validated.
    if USE_PROXY:
        return httpx.AsyncClient(timeout=30, follow_redirects=False)
    return httpx.AsyncClient(timeout=30, follow_redirects=False,
                             transport=_pinned_transport(), trust_env=False)


def _parse_xml(content: bytes, url: str) -> ET.Element:
    try:
        return _xml_fromstring(content)
    except DefusedXmlException as exc:
        raise UnsafeContentError(
            f"Blocked unsafe XML from {url} ({type(exc).__name__}): {exc}") from exc
    except _XMLParseError as exc:
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


def upgrade_to_https(url: str) -> str:
    """Rewrite a pasted http:// URL to https:// (Cited never fetches plain HTTP)."""
    url = url.strip()
    if url[:7].lower() == "http://":
        return "https://" + url[7:]
    return url


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

async def _fetch_substack(key: str, max_posts: Optional[int] = None,
                          max_chars: Optional[int] = None, free_only: bool = False,
                          stop: Optional[Callable[[list[Post]], bool]] = None) -> list[Post]:
    """Fetch posts newest-first.

    max_posts / max_chars default to MAX_POSTS / POST_CHARS (None in the library
    means the full archive). free_only skips paid-only posts (their API body is
    just a preview). `stop(page_posts)` is called after each page; returning
    True stops paging — used for incremental refreshes.
    """
    max_posts = MAX_POSTS if max_posts is None else max_posts
    max_chars = POST_CHARS if max_chars is None else max_chars
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
                err = FetchError(f"Substack API {exc.response.status_code} for '{key}'")
                err.status = exc.response.status_code
                raise err from exc
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

            page: list[Post] = []
            for p in batch:
                if not isinstance(p, dict):
                    continue
                audience = str(p.get("audience") or "")
                if free_only and audience in _PAID_AUDIENCES:
                    continue
                body = p.get("body_html") or p.get("truncated_body_text") or ""
                text = _strip_html(body)
                if len(text) < 60:
                    continue
                page.append(Post(
                    title=p.get("title") or "Untitled",
                    url=(p.get("canonical_url") or f"{base}/p/{p.get('slug', '')}"),
                    date=(p.get("post_date") or "")[:10],
                    text=text[:max_chars],
                    audience=audience,
                ))
            posts.extend(page)

            if not more or (max_posts and len(posts) >= max_posts):
                break
            if stop is not None and stop(page):
                break

    log.info("Fetched %d posts from %s (Substack)", len(posts), key)
    return posts[:max_posts] if max_posts else posts


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


def _parse_rss_items(channel: ET.Element, max_posts: Optional[int] = None,
                     max_chars: Optional[int] = None) -> list[Post]:
    max_posts = MAX_POSTS if max_posts is None else max_posts
    max_chars = POST_CHARS if max_chars is None else max_chars
    posts: list[Post] = []
    for item in channel.findall("item")[:max_posts or None]:
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
                              date=_normalize_date(date), text=text[:max_chars]))
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


async def _fetch_medium(url: str, max_posts: Optional[int] = None,
                        max_chars: Optional[int] = None) -> list[Post]:
    rss_url = _medium_rss_url(url)
    log.info("Fetching Medium RSS: %s", rss_url)
    content = await _fetch_feed_bytes(rss_url, "Medium RSS")
    root = _parse_xml(content, rss_url)

    channel = root.find("channel")
    if channel is None:
        channel = root
    posts = [p for p in _parse_rss_items(channel, max_posts, max_chars) if p.url]

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


async def _fetch_rss(url: str, max_posts: Optional[int] = None,
                     max_chars: Optional[int] = None) -> list[Post]:
    """Fetch posts/episodes from any RSS 2.0, Atom, or podcast feed URL."""
    max_posts = MAX_POSTS if max_posts is None else max_posts
    max_chars = POST_CHARS if max_chars is None else max_chars
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
        for entry in root.findall(tag("entry"))[:max_posts or None]:
            title_el   = entry.find(tag("title"))
            updated_el = _first(entry.find(tag("published")), entry.find(tag("updated")))
            content_el = _first(entry.find(tag("content")), entry.find(tag("summary")))

            title = (title_el.text or "").strip() if title_el is not None else ""
            link  = _atom_link(entry, tag)
            date  = _normalize_date(updated_el.text or "") if updated_el is not None else ""
            text  = _strip_html(content_el.text or "") if content_el is not None else ""

            if len(text) >= 60:
                posts.append(Post(title=title or "Untitled", url=link, date=date,
                                  text=text[:max_chars]))

    else:
        # ── RSS 2.0 (includes podcast feeds) ──
        channel = root.find("channel")
        if channel is None:
            channel = root
        posts = _parse_rss_items(channel, max_posts, max_chars)

    log.info("Fetched %d posts/episodes from %s (RSS)", len(posts), url)
    return posts


# ── Feed discovery (non-Substack sites) & paged feeds ─────────────────────────

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RE     = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")
_FEED_TYPES  = ("application/rss+xml", "application/atom+xml", "application/feed+xml")
_FEED_GUESSES = ("/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")
_FEED_FOR: dict[str, str] = {}          # site key → discovered feed URL
MAX_FEED_PAGES = 200


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "")
            for m in _ATTR_RE.finditer(tag)}


def _looks_like_feed(content: bytes) -> bool:
    head = content.lstrip()[:600].lower()
    return b"<rss" in head or b"<feed" in head or (head.startswith(b"<?xml") and b"<html" not in head)


async def discover_feed(url: str) -> Optional[str]:
    """Find a site's RSS/Atom feed: <link rel="alternate"> on the page, then common paths."""
    url = upgrade_to_https(url)
    key = parse_subdomain(url)
    if key in _FEED_FOR:
        return _FEED_FOR[key]
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    async with _client() as client:
        for page in dict.fromkeys([url, root + "/"]):
            try:
                resp = await _get(client, page)
            except (httpx.HTTPError, FetchError):
                continue
            text = resp.content[:300_000].decode("utf-8", "replace")
            for tag in _LINK_TAG_RE.findall(text):
                a = _attrs(tag)
                if "alternate" in a.get("rel", "").lower().split() and \
                        a.get("type", "").lower() in _FEED_TYPES and a.get("href"):
                    feed = upgrade_to_https(urljoin(page, a["href"]))
                    if "comments" in feed.lower():
                        continue          # WordPress also advertises a comments feed
                    _FEED_FOR[key] = feed
                    return feed
        for path in _FEED_GUESSES:
            try:
                resp = await _get(client, root + path)
            except (httpx.HTTPError, FetchError):
                continue
            if _looks_like_feed(resp.content):
                _FEED_FOR[key] = root + path
                return root + path
    return None


def _should_try_feed(url: str, exc: Exception) -> bool:
    """After a failed Substack attempt: is this site plausibly not Substack at all?"""
    if _host_is(url, "substack.com"):
        return False                                   # real Substack: keep the real error
    if isinstance(exc, (UnsafeURLError, UnsafeContentError, SourcePausedError)):
        return False
    if isinstance(exc, NotSubstackError):
        return True                                    # 404 or non-JSON from the Substack API
    return getattr(exc, "status", None) in (401, 403, 405)   # API refused; site may still have a feed


def _with_page(feed_url: str, n: int) -> str:
    parts = urlsplit(feed_url)
    q = [kv for kv in parts.query.split("&") if kv and not kv.startswith("paged=")]
    q.append(f"paged={n}")
    return parts._replace(query="&".join(q)).geturl()


async def _fetch_feed_archive(feed_url: str, max_chars: int,
                              stop: Optional[Callable[[list[Post]], bool]] = None) -> list[Post]:
    """Fetch a feed, then older pages WordPress-style (?paged=2, 3, …) when the site supports it.

    Stops at the first page that errors, is empty, repeats posts already seen
    (the site ignores ?paged), or — for incremental refreshes — when `stop(page)`
    says everything on it is already known.
    """
    posts = await _fetch_rss(feed_url, max_posts=0, max_chars=max_chars)
    seen = {p.url for p in posts}
    if len(posts) < 5 or (stop is not None and stop(posts)):
        return posts
    for n in range(2, MAX_FEED_PAGES + 1):
        try:
            page = await _fetch_rss(_with_page(feed_url, n), max_posts=0, max_chars=max_chars)
        except (FetchError, NotSubstackError):
            break
        new = [p for p in page if p.url not in seen]
        if not new:
            break
        posts.extend(new)
        seen.update(p.url for p in new)
        if stop is not None and stop(new):
            break
    log.info("Fetched %d posts from %s (feed archive)", len(posts), feed_url)
    return posts


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_posts(url: str) -> list[Post]:
    """Return posts for a newsletter URL, using in-process cache."""
    url    = upgrade_to_https(url)
    source = _detect_source(url)   # raises NotSubstackError for LinkedIn etc.
    key    = parse_subdomain(url)
    await _check_url(url)          # reject unsafe URLs before touching the cache

    host = _host(url)
    until = paused_hosts().get(host)
    if until:
        mins = max(1, round((until - time.time()) / 60))
        raise SourcePausedError(
            f"Fetching from {host} is paused for about {mins} more minute(s) after repeated "
            "failures. Please try again later.")

    cached = _CACHE.get(key)
    if cached and time.time() - cached[1] < CACHE_TTL:
        log.info("Cache hit for %s", key)
        return cached[0]

    if source == "medium":
        posts = await _fetch_medium(url)
    elif source == "rss":
        posts = await _fetch_rss(url)
    else:
        try:
            posts = await _fetch_substack(key)
        except (NotSubstackError, FetchError) as exc:
            if not _should_try_feed(url, exc):
                raise
            feed = await discover_feed(url)          # not Substack → try the site's RSS/Atom feed
            if not feed:
                raise
            posts = await _fetch_rss(feed)

    _CACHE[key] = (posts, time.time())
    return posts


async def fetch_archive(url: str, *, max_chars: int, free_only: bool = True,
                        stop: Optional[Callable[[list[Post]], bool]] = None) -> list[Post]:
    """Fetch as much of a source as it exposes (used by the library; no cache).

    Substack: every public post (paged, newest first; `stop` can end early).
    Other sites: their RSS/Atom feed (discovered automatically), paged back
    WordPress-style (?paged=N) when the site supports it.
    Medium: everything its feed currently lists.
    """
    url = upgrade_to_https(url)
    source = _detect_source(url)
    await _check_url(url)
    if source == "medium":
        return await _fetch_medium(url, max_posts=0, max_chars=max_chars)
    if source == "rss":
        return await _fetch_feed_archive(url, max_chars, stop)
    try:
        return await _fetch_substack(parse_subdomain(url), max_posts=0, max_chars=max_chars,
                                     free_only=free_only, stop=stop)
    except (NotSubstackError, FetchError) as exc:
        if not _should_try_feed(url, exc):
            raise
        feed = await discover_feed(url)
        if not feed:
            raise NotSubstackError(
                f"{url} isn't a Substack and no RSS/Atom feed was found on it. "
                "Put the site's feed URL in library.json instead.") from exc
        return await _fetch_feed_archive(feed, max_chars, stop)


def invalidate(url: str) -> None:
    try:
        _CACHE.pop(parse_subdomain(url), None)
    except NotSubstackError:
        pass
