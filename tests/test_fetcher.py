import json

import httpx
import pytest

import fetcher
from fetcher import FetchError, NotSubstackError, UnsafeURLError

LONG = "This is a long enough body of text about pricing strategy and growth. " * 3


# ── SSRF protection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://127.0.0.1/feed",
    "https://169.254.169.254/latest/meta-data/feed",
    "https://[::1]/feed",
    "https://[::ffff:127.0.0.1]/feed",
    "https://10.1.2.3/rss",
    "https://0.0.0.0/rss",
    "https://internal.example/feed",
    "https://rebind.example/feed",      # any private answer → reject
    "https://v6local.example/feed",
    "http://public.example/feed.xml",   # plain HTTP never allowed
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
        302, headers={"location": "https://169.254.169.254/latest/meta-data/"})
    with pytest.raises(UnsafeURLError):
        await fetcher.get_posts("https://public.example/feed.xml")
    assert mock_http["__seen__"] == ["https://public.example/feed.xml"]


async def test_redirect_downgrade_to_http_is_blocked(mock_http):
    mock_http["https://public.example/feed.xml"] = httpx.Response(
        302, headers={"location": "http://public.example/feed.xml"})
    with pytest.raises(UnsafeURLError, match="https"):
        await fetcher.get_posts("https://public.example/feed.xml")


async def test_pasted_http_url_is_upgraded(mock_http):
    mock_http["https://feeds.example/f.xml"] = httpx.Response(
        200, content=_rss([("A", "https://x/a", "", LONG)]))
    posts = await fetcher.get_posts("http://feeds.example/f.xml")
    assert len(posts) == 1
    assert mock_http["__seen__"] == ["https://feeds.example/f.xml"]


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
    with pytest.raises(fetcher.UnsafeContentError, match="unsafe XML"):
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


# ── DNS rebinding: connection is pinned to an address validated at connect time ──

import asyncio
from conftest import FAKE_DNS


async def _tcp_server():
    hits = []

    async def handle(reader, writer):
        hits.append(await reader.read(100))
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], hits


async def test_real_client_uses_pinned_backend():
    async with fetcher._client() as client:
        pool = client._transport._pool
        assert isinstance(pool._network_backend, fetcher._PinnedBackend)


async def test_dns_rebinding_blocked_at_connect(monkeypatch):
    """Pre-check sees a public IP; by connect time DNS answers 127.0.0.1.
    The real client must refuse to connect (and never reach the local server)."""
    srv, port, hits = await _tcp_server()
    answers = iter([["93.184.216.50"], ["127.0.0.1"]])

    async def flipping_resolve(host, p):
        if host == "rebinder.example":
            return next(answers)
        return FAKE_DNS[host]

    monkeypatch.setattr(fetcher, "_resolve", flipping_resolve)
    async with srv:
        async with fetcher._client() as client:
            with pytest.raises(UnsafeURLError):
                await fetcher._get(client, f"https://rebinder.example:{port}/feed")
    assert hits == []


async def test_pinned_backend_connects_to_resolved_ip(monkeypatch):
    """The backend dials the validated IP, not whatever the OS resolves later."""
    srv, port, hits = await _tcp_server()
    monkeypatch.setitem(FAKE_DNS, "pinned.example", ["127.0.0.1"])
    monkeypatch.setattr(fetcher, "_ip_is_public", lambda ip: True)  # allow loopback for this test
    async with srv:
        stream = await fetcher._PinnedBackend().connect_tcp("pinned.example", port, timeout=5)
        await stream.write(b"ping")
        await stream.aclose()
        await asyncio.sleep(0.05)
    assert hits == [b"ping"]


async def test_pinned_backend_rejects_private_answer():
    with pytest.raises(UnsafeURLError):
        await fetcher._PinnedBackend().connect_tcp("internal.example", 443, timeout=5)


async def test_pinned_tls_still_verifies_hostname(monkeypatch):
    """Connecting to a pinned IP must not weaken TLS: the cert is checked
    against the URL's hostname, not the IP."""
    import ssl
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("tls.example").configure_cert(server_ctx)
    client_ctx = ssl.create_default_context()
    ca.configure_trust(client_ctx)

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = srv.sockets[0].getsockname()[1]
    monkeypatch.setitem(FAKE_DNS, "tls.example", ["127.0.0.1"])
    monkeypatch.setitem(FAKE_DNS, "wrong.example", ["127.0.0.1"])
    monkeypatch.setattr(fetcher, "_ip_is_public", lambda ip: True)

    def client():
        t = httpx.AsyncHTTPTransport(verify=client_ctx, trust_env=False)
        t._pool._network_backend = fetcher._PinnedBackend()
        return httpx.AsyncClient(transport=t, trust_env=False)

    async with srv:
        async with client() as c:
            r = await c.get(f"https://tls.example:{port}/")
            assert r.text == "ok"
        async with client() as c:
            with pytest.raises(httpx.ConnectError, match="(?i)certificate|hostname"):
                await c.get(f"https://wrong.example:{port}/")
