"""
Fetches publicly available posts from Substack newsletters.
Caches per-subdomain for CACHE_TTL seconds.
"""
import re
import sys
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
    m = re.match(r"https?://([^./]+)\.substack\.com", url.strip())
    if m:
        return m.group(1)
    raise NotSubstackError(
        f"Expected a Substack URL like https://author.substack.com — got: {url}"
    )


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    for ent, ch in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_all(subdomain: str) -> list[Post]:
    posts: list[Post] = []
    offset = 0
    limit  = 25

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        while True:
            api = (
                f"https://{subdomain}.substack.com/api/v1/posts"
                f"?limit={limit}&offset={offset}"
            )
            try:
                resp = await client.get(api)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise FetchError(
                    f"Substack API {exc.response.status_code} for '{subdomain}'"
                ) from exc
            except httpx.RequestError as exc:
                raise FetchError(f"Network error: {exc}") from exc

            batch = resp.json()
            if not batch:
                break

            for p in batch:
                body = p.get("body_html") or p.get("truncated_body_text") or ""
                text = _strip_html(body)
                if len(text) < 60:
                    continue  # paywalled or empty
                posts.append(Post(
                    title=p.get("title") or "Untitled",
                    url=(p.get("canonical_url")
                         or f"https://{subdomain}.substack.com/p/{p.get('slug','')}"),
                    date=(p.get("post_date") or "")[:10],
                    text=text[:5000],
                ))

            if len(batch) < limit or len(posts) >= 200:
                break
            offset += limit

    log.info(f"Fetched {len(posts)} posts from {subdomain}.substack.com")
    return posts


async def get_posts(url: str) -> list[Post]:
    """Return posts, using in-process cache."""
    subdomain = parse_subdomain(url)
    cached = _CACHE.get(subdomain)
    if cached and time.time() - cached[1] < CACHE_TTL:
        log.info(f"Cache hit for {subdomain}")
        return cached[0]

    log.info(f"Fetching {subdomain}.substack.com …")
    posts = await _fetch_all(subdomain)
    _CACHE[subdomain] = (posts, time.time())
    return posts


def invalidate(url: str) -> None:
    try:
        subdomain = parse_subdomain(url)
        _CACHE.pop(subdomain, None)
    except NotSubstackError:
        pass
