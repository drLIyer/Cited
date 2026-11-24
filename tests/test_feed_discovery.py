import httpx
import pytest

import fetcher
import library
import pipeline

SITE = "https://news.example"
HTML = b"""<!doctype html><html><head>
<link rel="alternate" type="application/rss+xml" title="Comments" href="https://news.example/comments/feed/">
<link rel='alternate' type='application/rss+xml' title='Product Talk' href='/feed/'>
</head><body>hi</body></html>"""
BODY = "Opportunity solution trees help teams connect outcomes to experiments and interviews. " * 4


def rss(nums):
    items = "".join(f"<item><title>Post {n}</title><link>{SITE}/post-{n}/</link>"
                    f"<pubDate>Mon, 0{1 + n % 9} Jan 2024 00:00:00 GMT</pubDate>"
                    f"<description>{BODY} (#{n})</description></item>" for n in nums)
    return f'<rss><channel><generator>https://wordpress.org/</generator>{items}</channel></rss>'.encode()


@pytest.fixture(autouse=True)
def clear():
    fetcher._FEED_FOR.clear()


def wordpress(mock_http, pages):
    mock_http[SITE] = httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
    for n, nums in enumerate(pages, start=1):
        url = f"{SITE}/feed/" if n == 1 else f"{SITE}/feed/?paged={n}"
        mock_http[url] = httpx.Response(200, content=rss(nums))


async def test_discovers_feed_from_link_tag_skipping_comments(mock_http):
    wordpress(mock_http, [[1]])
    assert await fetcher.discover_feed(SITE) == f"{SITE}/feed/"


async def test_falls_back_to_common_feed_paths(mock_http):
    mock_http[SITE] = httpx.Response(200, content=b"<html><head></head></html>")
    mock_http[f"{SITE}/feed/"] = httpx.Response(200, content=rss([1]))
    assert await fetcher.discover_feed(SITE) == f"{SITE}/feed/"


async def test_no_feed_found(mock_http):
    mock_http[SITE] = httpx.Response(200, content=b"<html></html>")
    assert await fetcher.discover_feed(SITE) is None


async def test_live_search_of_non_substack_site_uses_feed(mock_http):
    wordpress(mock_http, [list(range(1, 11))])
    posts = await fetcher.get_posts(SITE)                 # Substack API 404 → feed
    assert len(posts) == 10 and posts[0].url == f"{SITE}/post-1/"


async def test_archive_pages_back_through_wordpress_feed(mock_http):
    wordpress(mock_http, [list(range(1, 11)), list(range(11, 21)), list(range(21, 26))])
    posts = await fetcher.fetch_archive(SITE, max_chars=60_000)
    assert len(posts) == 25                               # page 4 → 404 ends paging
    assert f"{SITE}/feed/?paged=4" in mock_http["__seen__"]


async def test_site_that_ignores_paged_does_not_loop(mock_http):
    nums = list(range(1, 11))
    wordpress(mock_http, [nums])
    mock_http[f"{SITE}/feed/?paged=2"] = httpx.Response(200, content=rss(nums))   # same page again
    posts = await fetcher.fetch_archive(SITE, max_chars=60_000)
    assert len(posts) == 10
    assert f"{SITE}/feed/?paged=3" not in mock_http["__seen__"]


async def test_real_substack_outage_is_not_mistaken_for_non_substack(mock_http):
    mock_http["https://good.substack.com/api/v1/posts?limit=25"] = httpx.Response(503)
    with pytest.raises(fetcher.FetchError, match="503"):
        await fetcher.fetch_archive("https://good.substack.com", max_chars=1000)
    assert not any("feed" in u for u in mock_http["__seen__"])


async def test_unreachable_site_with_no_feed_explains(mock_http):
    mock_http[SITE] = httpx.Response(200, content=b"<html></html>")
    with pytest.raises(fetcher.NotSubstackError, match="no RSS/Atom feed"):
        await fetcher.fetch_archive(SITE, max_chars=1000)


async def test_library_builds_and_refreshes_wordpress_site(tmp_path, mock_http):
    pipeline.prewarm()
    L = library.Library(enabled=True, db_path=lambda: tmp_path / "l.db",
                        sources=[{"name": "Product Talk", "url": SITE, "key": "news.example"}])
    wordpress(mock_http, [list(range(1, 11)), list(range(11, 21)), list(range(21, 26))])
    r = await L.refresh("news.example")
    assert r["posts"] == 25 and L.ready("news.example")

    # Next refresh: one new post on page 1 → only page 1 and 2 checked, one post embedded
    wordpress(mock_http, [[0] + list(range(1, 10)), list(range(10, 20)), list(range(20, 26))])
    mock_http["__seen__"].clear()
    r = await L.refresh("news.example")
    assert r["new_or_changed_posts"] == 1 and r["posts"] == 26
    assert f"{SITE}/feed/?paged=3" not in mock_http["__seen__"]
