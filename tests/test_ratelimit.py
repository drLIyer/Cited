import json
import socket
import threading
import time

import httpx
import pytest

from ratelimit import ClientRateLimit, RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── Unit: sliding window ──────────────────────────────────────────────────────

def test_allows_limit_then_blocks():
    clock = FakeClock()
    rl = RateLimiter(15, clock=clock)
    for i in range(15):
        ok, remaining, _ = rl.hit("a")
        assert ok and remaining == 14 - i
    ok, remaining, retry = rl.hit("a")
    assert not ok and remaining == 0 and 1 <= retry <= 60


def test_window_slides():
    clock = FakeClock()
    rl = RateLimiter(2, clock=clock)
    rl.hit("a"); clock.t += 30; rl.hit("a")
    assert not rl.hit("a")[0]
    clock.t += 30.5          # first hit is now > 60s old
    assert rl.hit("a")[0]
    assert not rl.hit("a")[0]


def test_retry_after_is_accurate():
    clock = FakeClock()
    rl = RateLimiter(1, clock=clock)
    rl.hit("a"); clock.t += 45
    assert rl.hit("a") == (False, 0, 15)


def test_keys_are_independent():
    rl = RateLimiter(1, clock=FakeClock())
    assert rl.hit("a")[0] and rl.hit("b")[0]
    assert not rl.hit("a")[0]


def test_blocked_attempts_do_not_extend_the_window():
    clock = FakeClock()
    rl = RateLimiter(1, clock=clock)
    rl.hit("a")
    for _ in range(10):
        clock.t += 5
        rl.hit("a")
    clock.t += 11            # 61s after the only *allowed* hit
    assert rl.hit("a")[0]


def test_zero_disables():
    rl = RateLimiter(0, clock=FakeClock())
    assert all(rl.hit("a")[0] for _ in range(1000))


def test_stale_clients_are_swept():
    clock = FakeClock()
    rl = RateLimiter(5, clock=clock)
    for i in range(50):
        rl.hit(f"ip{i}")
    assert rl.tracked() == 50
    clock.t += 61
    rl.hit("new")
    assert rl.tracked() == 1


# ── Unit: client IP extraction ───────────────────────────────────────────────

def test_client_ip_uses_rightmost_trusted_hop():
    c = ClientRateLimit(15, trusted_hops=1)
    # A client can prepend anything; only the entry the proxy appended counts.
    assert c.client_ip("10.0.0.1", "6.6.6.6, 203.0.113.7") == "203.0.113.7"
    assert c.client_ip("10.0.0.1", "203.0.113.7") == "203.0.113.7"


def test_client_ip_two_hops():
    c = ClientRateLimit(15, trusted_hops=2)
    assert c.client_ip("10.0.0.1", "6.6.6.6, 203.0.113.7, 10.1.1.1") == "203.0.113.7"


def test_client_ip_fallbacks():
    c = ClientRateLimit(15, trusted_hops=1)
    assert c.client_ip("198.51.100.2", None) == "198.51.100.2"
    assert c.client_ip("198.51.100.2", "not-an-ip") == "198.51.100.2"
    assert c.client_ip(None, None) == "unknown"
    assert ClientRateLimit(15, trusted_hops=0).client_ip("198.51.100.2", "1.2.3.4") == "198.51.100.2"


def test_shared_cidrs_get_their_own_limit():
    c = ClientRateLimit(2, shared_cidrs="160.79.104.0/23", shared_per_min=5, trusted_hops=1)
    assert [c.check("160.79.104.10")[0] for _ in range(6)] == [True] * 5 + [False]
    assert c.check("160.79.104.10")[1] == 5
    assert [c.check("203.0.113.7")[0] for _ in range(3)] == [True, True, False]


# ── Integration: real HTTP server + MCP protocol ──────────────────────────────

LONG = "Pricing strategy for SaaS: charge for value, not cost, and test tiers. " * 10
FEED = (f"<rss><channel><item><title>Pricing</title><link>https://x/p</link>"
        f"<description>{LONG}</description></item></channel></rss>").encode()


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


@pytest.fixture
def live_server(server_factory, mock_http):
    import uvicorn
    s = server_factory(CITED_TRANSPORT="http", CITED_RATE_LIMIT_PER_MIN="3")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=FEED)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(s.build_http_app(), host="127.0.0.1", port=port,
                                        log_level="warning", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def _call(base, tool, args, ip):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    r = httpx.post(f"{base}/mcp", json=body, trust_env=False, timeout=30, headers={
        "accept": "application/json, text/event-stream",
        "x-forwarded-for": ip})
    assert r.status_code == 200, r.text
    text = r.text
    if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        text = next(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
    return json.loads(text)["result"]["content"][0]["text"]


def test_rate_limit_end_to_end(live_server):
    url = "https://feeds.example/feed.xml"
    for _ in range(3):
        out = json.loads(_call(live_server, "search_newsletter", {"url": url, "query": "pricing"}, "203.0.113.7"))
        assert "results" in out
    blocked = json.loads(_call(live_server, "search_newsletter", {"url": url, "query": "pricing"}, "203.0.113.7"))
    assert blocked["rate_limited"] is True
    assert "3 searches per minute" in blocked["error"]

    # list_posts shares the same allowance
    assert "Rate limit reached" in _call(live_server, "list_posts", {"url": url}, "203.0.113.7")

    # A different client is unaffected
    out = json.loads(_call(live_server, "search_newsletter", {"url": url, "query": "pricing"}, "198.51.100.9"))
    assert "results" in out


def test_spoofed_forwarded_for_does_not_bypass(live_server):
    url = "https://feeds.example/feed.xml"
    for i in range(3):
        _call(live_server, "list_posts", {"url": url}, f"10.9.9.{i}, 203.0.113.50")
    # Rotating the client-supplied part doesn't help: the proxy-added IP is the key.
    assert "Rate limit reached" in _call(live_server, "list_posts", {"url": url}, "1.1.1.1, 203.0.113.50")


async def test_stdio_mode_is_never_limited(server_factory, mock_http):
    s = server_factory(CITED_RATE_LIMIT_PER_MIN="1")
    mock_http["https://feeds.example/feed.xml"] = httpx.Response(200, content=FEED)
    for _ in range(3):
        out = await s.list_posts("https://feeds.example/feed.xml")
        assert "Rate limit" not in out
