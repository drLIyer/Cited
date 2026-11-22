import json

import httpx
import pytest

import fetcher
from fetcher import FetchError, NotSubstackError, UnsafeURLError

LONG = "This is a long enough body of text about pricing strategy and growth. " * 3


# ── SSRF protection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/feed",
    "http://169.254.169.254/latest/meta-data/feed",
    "http://[::1]/feed",
    "http://[::ffff:127.0.0.1]/feed",
    "http://10.1.2.3/rss",
    "http://0.0.0.0/rss",
    "http://internal.example/feed",
    "http://rebind.example/feed",      # any private answer → reject
    "http://v6local.example/feed",
    "file:///etc/passwd",
    "ftp://public.example/feed.xml",
    "gopher://public.example/",
])
async def test_check_url_rejects_unsafe(url):
    with pytest.raises(UnsafeURLError):
        await fetcher._check_url(url)


async def test_check_url_allows_public():
    await fetcher._check_url("https://public.example/feed.xml")


async def test_check_url_unresolvable_is_fetch_error():
    with pytest.raises(FetchError):
        await fetcher._check_url("https://nope.invalid/feed")


async def test_get_posts_rejects_internal_before_fetch(mock_http):
    with pytest.raises(UnsafeURLError):
        await fetcher.get_posts("http://169.254.169.254/latest/feed")
    assert mock_http["__seen__"] == []


async def test_redirect_to_internal_is_blocked(mock_http):
    mock_http["https://public.example/feed.xml"] = httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(UnsafeURLError):
        await fetcher.get_posts("https://public.example/feed.xml")
    assert mock_http["__seen__"] == ["https://public.example/feed.xml"]


async def test_redirect_to_public_is_followed(mock_http):
    mock_http["https://public.example/feed.xml"] = httpx.Response(
        301, headers={"location": "/new.xml"})
    mock_http["https://public.example/new.xml"] = httpx.Response(
        200, content=_rss([("A", "https://x/a", "Mon, 01 Jan 2024 10:00:00 GMT", LONG)]))
    posts = await fetcher.get_posts("https://public.example/feed.xml")
    assert len(posts) == 1


async def test_too_many_redirects(mock_http):
    mock_http["https://public.example/feed.xml"] = httpx.Response(
        302, headers={"location": "https://public.example/feed.xml"})
    with pytest.raises(FetchError, match="redirects"):
        await fetcher.get_posts("https://public.example/feed.xml")


async def test_response_size_cap(mock_http, monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_FETCH_BYTES", 1000)
    mock_http["https://public.example/feed.xml"] = httpx.Response(200, content=b"x" * 5000)
    with pytest.raises(FetchError, match="too large"):
        await fetcher.get_posts("https://public.example/feed.xml")


async def test_billion_laughs_rejected(mock_http):
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""
    mock_http["https://public.example/feed.xml"] = httpx.Response(200, content=bomb)
    with pytest.raises(FetchError, match="parse"):
        await fetcher.get_posts("https://public.example/feed.xml")


# ── RSS / Atom parsing (the falsy-Element bug) ────────────────────────────────

def _rss(items):
    parts = []
    for title, link, date, body in items:
        parts.append(f"<item><title>{title}</title><link>{link}</link>"
                     f"<pubDate>{date}</pubDate><description>{body}</description></item>")
    return f"<rss><channel>{''.join(parts)}</channel></rss>".encode()


async def test_rss_dates_are_kept(mock_http):
    mock_http["https://feeds.example/podcast.xml"] = httpx.Response(
        200, content=_rss([("Ep 1", "https://x/1", "Mon, 01 Jan 2024 10:00:00 GMT", LONG)]))
    posts = await fetcher.get_posts("https://feeds.example/podcast.xml")
    assert posts[0].date == "2024-01-01"


async def test_rss_dc_date_fallback(mock_http):
    xml = (b'<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>'
           b"<title>T</title><link>https://x/t</link><dc:date>2023-05-06T00:00:00Z</dc:date>"
           b"<description>" + LONG.encode() + b"</description></item></channel></rss>")
    mock_http["https://feeds.example/f.xml"] = httpx.Response(200, content=xml)
    posts = await fetcher.get_posts("https://feeds.example/f.xml")
    assert posts[0].date == "2023-05-06"


async def test_atom_content_and_link_kept(mock_http):
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom post</title>'
           '<link rel="self" href="https://x/self"/>'
           '<link rel="alternate" href="https://x/post"/>'
           f'<updated>2024-02-03T00:00:00Z</updated><content type="html">{LONG}</content>'
           "</entry></feed>").encode()
    mock_http["https://feeds.example/atom.xml"] = httpx.Response(200, content=xml)
    posts = await fetcher.get_posts("https://feeds.example/atom.xml")
    assert len(posts) == 1, "Atom entries with <content> must not be dropped"
    assert posts[0].url == "https://x/post"
    assert posts[0].date == "2024-02-03"
    assert "pricing strategy" in posts[0].text


async def test_atom_summary_fallback(mock_http):
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>S</title>'
           f'<link href="https://x/s"/><summary>{LONG}</summary></entry></feed>').encode()
    mock_http["https://feeds.example/atom.xml"] = httpx.Response(200, content=xml)
    posts = await fetcher.get_posts("https://feeds.example/atom.xml")
    assert len(posts) == 1


async def test_html_page_rejected(mock_http):
    mock_http["https://feeds.example/feed"] = httpx.Response(
        200, content=b"<!DOCTYPE html><html><body>login</body></html>")
    with pytest.raises(NotSubstackError, match="HTML page"):
        await fetcher.get_posts("https://feeds.example/feed")


async def test_post_text_cap(mock_http, monkeypatch):
    monkeypatch.setattr(fetcher, "POST_CHARS", 100)
    mock_http["https://feeds.example/f.xml"] = httpx.Response(
        200, content=_rss([("A", "https://x/a", "", LONG * 10)]))
    posts = await fetcher.get_posts("https://feeds.example/f.xml")
    assert len(posts[0].text) == 100


async def test_medium_dates(mock_http):
    mock_http["https://medium.com/feed/@author"] = httpx.Response(
        200, content=_rss([("M", "https://medium.com/@author/m", "Tue, 02 Jan 2024 00:00:00 GMT", LONG)]))
    posts = await fetcher.get_posts("https://medium.com/@author")
    assert posts[0].date == "2024-01-02"


# ── Substack pagination ──────────────────────────────────────────────────────

def _sub_post(i):
    return {"title": f"P{i}", "slug": f"p{i}", "post_date": "2024-01-01T00:00:00Z",
            "body_html": f"<p>{LONG}</p>"}


async def test_substack_cursor_pagination(mock_http):
    base = "https://good.substack.com/api/v1/posts?limit=25"
    mock_http[base] = httpx.Response(200, json={"posts": [_sub_post(i) for i in range(25)],
                                                "nextCursor": "c1"})
    mock_http[base + "&after=c1"] = httpx.Response(200, json={"posts": [_sub_post(i) for i in range(25, 30)]})
    posts = await fetcher.get_posts("https://good.substack.com")
    assert len(posts) == 30
    assert posts[0].url == "https://good.substack.com/p/p0"


async def test_substack_plain_list_paginates_by_offset(mock_http):
    base = "https://good.substack.com/api/v1/posts?limit=25"
    mock_http[base] = httpx.Response(200, json=[_sub_post(i) for i in range(25)])
    mock_http[base + "&offset=25"] = httpx.Response(200, json=[_sub_post(i) for i in range(25, 40)])
    posts = await fetcher.get_posts("https://good.substack.com")
    assert len(posts) == 40


async def test_substack_max_posts(mock_http, monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_POSTS", 30)
    base = "https://good.substack.com/api/v1/posts?limit=25"
    mock_http[base] = httpx.Response(200, json=[_sub_post(i) for i in range(25)])
    mock_http[base + "&offset=25"] = httpx.Response(200, json=[_sub_post(i) for i in range(25, 50)])
    posts = await fetcher.get_posts("https://good.substack.com")
    assert len(posts) == 30


async def test_substack_404(mock_http):
    with pytest.raises(NotSubstackError):
        await fetcher.get_posts("https://news.example")


async def test_cache_hit(mock_http):
    mock_http["https://feeds.example/f.xml"] = httpx.Response(
        200, content=_rss([("A", "https://x/a", "", LONG)]))
    await fetcher.get_posts("https://feeds.example/f.xml")
    await fetcher.get_posts("https://feeds.example/f.xml")
    assert mock_http["__seen__"].count("https://feeds.example/f.xml") == 1


# ── Source detection / labels ─────────────────────────────────────────────────

def test_detect_source():
    assert fetcher._detect_source("https://medium.com/@x") == "medium"
    assert fetcher._detect_source("https://pub.medium.com") == "medium"
    assert fetcher._detect_source("https://notmedium.com") == "substack"
    assert fetcher._detect_source("https://feeds.example/x.xml") == "rss"
    with pytest.raises(NotSubstackError):
        fetcher._detect_source("https://www.linkedin.com/newsletters/x")


def test_display_name():
    assert fetcher.display_name("aakashgupta") == "aakashgupta.substack.com"
    assert fetcher.display_name("news.aakashg.com") == "news.aakashg.com"
    assert fetcher.display_name("rss:feeds.example/x.xml") == "rss:feeds.example/x.xml"
    assert fetcher.display_name("medium:@x") == "medium:@x"
