"""
Fetches posts from Substack (API) and Medium (RSS).
Supports *.substack.com, custom Substack domains, and medium.com publications.
Caches per-source key for CACHE_TTL seconds.

LinkedIn newsletters require authentication and are not supported.
"""
import re
import time
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

log = logging.getLogger("cited.fetcher")

CACHE_TTL = 3600  # 1 hour

_CACHE: dict[str, tuple[list, float]] = {}

# XML namespaces used in RSS feeds
_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "atom":    "http://www.w3.org/2005/Atom",
}


class NotSubstackError(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass
class Post:
    title: str
    url: str
    date: str
    text: str


# ── Source detection ───────────────────────────────────────────────────────────

def _detect_source(url: str) -> str:
    """Return 'substack', 'medium', or raise for unsupported sources."""
    low = url.lower()
    if "linkedin.com" in low:
        raise NotSubstackError(
            "LinkedIn newsletters require authentication and aren't publicly "
            "accessible via any API. Try Substack or Medium instead."
        )
    if "medium.com" in low:
        return "medium"
    # Substack API works on substack.com subdomains and custom domains
    return "substack"


def parse_subdomain(url: str) -> str:
    """Return a stable cache/storage key for this newsletter.

    *.substack.com  → subdomain only  (e.g. 'aakashgupta')
    Custom domain   → full hostname   (e.g. 'news.aakashg.com')
    Medium          → 'medium:<path>' (e.g. 'medium:@username')
    """
    url = url.strip().rstrip("/")
    low = url.lower()

    if "medium.com" in low:
        m = re.match(r"https?://(?:www\.)?medium\.com(/[^?#]*)?", url, re.I)
        path = (m.group(1) or "").strip("/")
        return f"medium:{path}" if path else "medium:home"

    m = re.match(r"https?://([^./]+)\.substack\.com", url, re.I)
    if m:
        return m.group(1)

    m = re.match(r"https?://([^/]+)", url, re.I)
    if m:
        host = m.group(1).lower()
        if "." in host:
            return host

    raise NotSubstackError(
        f"Provide a URL like https://author.substack.com, "
        f"https://medium.com/@author, or https://news.custom.com — got: {url}"
    )


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
    offset = 0
    limit  = 25

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        while True:
            api = f"{base}/api/v1/posts?limit={limit}&offset={offset}"
            try:
                resp = await client.get(api)
                resp.raise_for_status()
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
                batch = resp.json()
            except Exception as exc:
                raise NotSubstackError(
                    f"'{key}' doesn't appear to be a Substack newsletter "
                    "(API returned non-JSON)."
                ) from exc

            if not isinstance(batch, list) or not batch:
                break

            for p in batch:
                body = p.get("body_html") or p.get("truncated_body_text") or ""
                text = _strip_html(body)
                if len(text) < 60:
                    continue
                posts.append(Post(
                    title=p.get("title") or "Untitled",
                    url=(p.get("canonical_url") or f"{base}/p/{p.get('slug', '')}"),
                    date=(p.get("post_date") or "")[:10],
                    text=text[:5000],
                ))

            if len(batch) < limit or len(posts) >= 200:
                break
            offset += limit

    log.info("Fetched %d posts from %s (Substack)", len(posts), key)
    return posts


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
        return f"{base}/feed"
    # Custom Medium domain — try /feed
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


async def _fetch_medium(url: str) -> list[Post]:
    rss_url = _medium_rss_url(url)
    log.info("Fetching Medium RSS: %s", rss_url)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(rss_url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FetchError(
                f"Medium RSS {exc.response.status_code} for {rss_url}. "
                "Make sure the URL is a valid Medium profile or publication."
            ) from exc
        except httpx.RequestError as exc:
            raise FetchError(f"Network error fetching Medium RSS: {exc}") from exc

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise FetchError(f"Could not parse Medium RSS feed: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        # Atom feed fallback
        channel = root

    posts: list[Post] = []
    for item in channel.findall("item")[:200]:
        title_el = item.find("title")
        link_el  = item.find("link")
        date_el  = item.find("pubDate") or item.find("dc:date", _NS)

        title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
        link  = link_el.text.strip()  if link_el  is not None and link_el.text  else ""
        date  = (date_el.text or "")[:10] if date_el is not None else ""
        text  = _text_from_rss_item(item)

        if len(text) < 60 or not link:
            continue
        posts.append(Post(title=title, url=link, date=date, text=text[:5000]))

    log.info("Fetched %d posts from %s (Medium RSS)", len(posts), url)
    return posts


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_posts(url: str) -> list[Post]:
    """Return posts for a newsletter URL, using in-process cache."""
    source = _detect_source(url)   # raises NotSubstackError for LinkedIn etc.
    key    = parse_subdomain(url)

    cached = _CACHE.get(key)
    if cached and time.time() - cached[1] < CACHE_TTL:
        log.info("Cache hit for %s", key)
        return cached[0]

    if source == "medium":
        posts = await _fetch_medium(url)
    else:
        posts = await _fetch_substack(key)

    _CACHE[key] = (posts, time.time())
    return posts


def invalidate(url: str) -> None:
    try:
        _CACHE.pop(parse_subdomain(url), None)
    except NotSubstackError:
        pass
