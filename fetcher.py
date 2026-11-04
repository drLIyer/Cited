"""
Fetches publicly available posts from Substack newsletters.
Supports both *.substack.com and custom domains (e.g. news.aakashg.com).
Caches per-domain for CACHE_TTL seconds.
"""
import re
import time
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("cited.fetcher")

CACHE_TTL = 3600  # 1 hour

_CACHE: dict[str, tuple[list, float]] = {}


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


def parse_subdomain(url: str) -> str:
    """Return a stable cache/storage key for this newsletter.

    *.substack.com  → subdomain only  (e.g. 'aakashgupta')
    Custom domain   → full hostname   (e.g. 'news.aakashg.com')
    """
    url = url.strip().rstrip("/")
    m = re.match(r"https?://([^./]+)\.substack\.com", url, re.I)
    if m:
        return m.group(1)
    m = re.match(r"https?://([^/]+)", url, re.I)
    if m:
        host = m.group(1).lower()
        if "." in host:
            return host
    raise NotSubstackError(
        f"Provide a full URL like https://author.substack.com or "
        f"https://news.author.com — got: {url}"
    )


def _api_base(key: str) -> str:
    """API base URL for a newsletter key."""
    if "." not in key:
        return f"https://{key}.substack.com"
    return f"https://{key}"


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, ch in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_all(key: str) -> list[Post]:
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

    log.info("Fetched %d posts from %s", len(posts), key)
    return posts


async def get_posts(url: str) -> list[Post]:
    """Return posts for a newsletter URL, using in-process cache."""
    key    = parse_subdomain(url)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[1] < CACHE_TTL:
        log.info("Cache hit for %s", key)
        return cached[0]

    log.info("Fetching %s …", key)
    posts      = await _fetch_all(key)
    _CACHE[key] = (posts, time.time())
    return posts


def invalidate(url: str) -> None:
    try:
        _CACHE.pop(parse_subdomain(url), None)
    except NotSubstackError:
        pass
