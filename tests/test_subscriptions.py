""""Bring your subscription": searching creators' MCP servers with the user's own key."""
import json
from types import SimpleNamespace

import httpx
import pytest

import pipeline
import subscriptions as subs
from conftest import FAKE_DNS

MCP_URL = "https://members.example/mcp"
GOOD_KEY = "member-key-123"

SUB_ENTRY = {
    "name": "Pod Members MCP (Host Name)", "type": "mcp", "kind": "podcast", "mcp_url": MCP_URL,
    "key_header": "X-Cited-Key-Pod", "key_env": "CITED_KEY_POD",
    "auth": {"header": "Authorization", "scheme": "Bearer"}, "homepage": "https://pod.example",
    "attribution": {"text": "The Pod with Host Name", "url": "https://pod.example"},
    "aliases": ["Pod Members", "Host Name"],
}
SUB = subs.load([SUB_ENTRY])[0]

TOOLS = [
    {"name": "list_episodes", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "search_episodes", "inputSchema": {"type": "object", "properties": {
        "query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
]
RESULTS = {"results": [
    {"title": "Ep 12: Pricing", "url": "https://youtube.com/watch?v=p&t=90s", "date": "2025-03-01",
     "excerpt": "Members-only insight: anchor your pricing on the outcome customers value most."},
    {"title": "Ep 7: Hiring", "url": "https://youtube.com/watch?v=h&t=30s", "date": "2024-11-02",
     "excerpt": "Hire for slope over intercept when building an early product team."},
]}


class CreatorServer:
    """Minimal MCP server behaviour, served through the httpx mock transport."""

    def __init__(self, sse=False, redirect=False):
        self.sse, self.redirect = sse, redirect
        self.calls, self.auths, self.session_ids = [], [], []

    def __call__(self, request: httpx.Request):
        if self.redirect:
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        self.auths.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {GOOD_KEY}":
            return httpx.Response(401, json={"error": "invalid key"})
        msg = json.loads(request.content)
        self.calls.append(msg["method"])
        self.session_ids.append(request.headers.get("mcp-session-id"))
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "pod", "version": "1"}}
        elif msg["method"] == "tools/list":
            result = {"tools": TOOLS}
        elif msg["method"] == "tools/call":
            assert msg["params"]["name"] == "search_episodes" and "query" in msg["params"]["arguments"]
            result = {"content": [{"type": "text", "text": json.dumps(RESULTS)}]}
        else:
            result = {}
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        headers = {"mcp-session-id": "sess-1"}
        if self.sse:
            return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"},
                                  content=f"event: message\ndata: {json.dumps(body)}\n\n".encode())
        return httpx.Response(200, headers=headers, json=body)


@pytest.fixture
def creator(mock_http, monkeypatch):
    monkeypatch.setitem(FAKE_DNS, "members.example", ["93.184.216.77"])
    subs._tools_cache.clear()
    server = CreatorServer()
    mock_http[MCP_URL] = server
    return server


# ── Keys ─────────────────────────────────────────────────────────────────────

def test_user_key_from_header_or_local_env(monkeypatch):
    assert subs.user_key(SUB, {"x-cited-key-pod": " k1 "}, local_mode=False) == "k1"
    assert subs.user_key(SUB, {"X-Cited-Key-Pod": "k2"}, local_mode=False) == "k2"
    monkeypatch.setenv("CITED_KEY_POD", "k3")
    assert subs.user_key(SUB, {}, local_mode=True) == "k3"
    assert subs.user_key(SUB, {}, local_mode=False) is None        # hosted: env never used


# ── Protocol ─────────────────────────────────────────────────────────────────

async def test_search_with_users_key(creator):
    passages = await subs.search(SUB, GOOD_KEY, "how to price")
    assert [p["title"] for p in passages] == ["Ep 12: Pricing", "Ep 7: Hiring"]
    assert passages[0]["url"].endswith("&t=90s")
    assert creator.calls == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert creator.session_ids[1:] == ["sess-1"] * 3                # session id carried forward
    assert set(creator.auths) == {f"Bearer {GOOD_KEY}"}


async def test_streaming_responses(creator):
    creator.sse = True
    assert len(await subs.search(SUB, GOOD_KEY, "pricing")) == 2


async def test_tool_list_is_cached(creator):
    await subs.search(SUB, GOOD_KEY, "a")
    await subs.search(SUB, GOOD_KEY, "b")
    assert creator.calls.count("tools/list") == 1


async def test_rejected_key_is_explained_and_never_echoed(creator):
    with pytest.raises(subs.SubscriptionError, match="rejected your key") as exc:
        await subs.search(SUB, "wrong-secret-key", "pricing")
    assert "wrong-secret-key" not in str(exc.value)


async def test_redirect_never_follows_with_the_key(creator, mock_http):
    creator.redirect = True
    with pytest.raises(subs.SubscriptionError, match="redirect"):
        await subs.search(SUB, GOOD_KEY, "pricing")
    assert not any("evil.example" in u for u in mock_http["__seen__"])


async def test_non_https_server_refused():
    insecure = {**SUB, "mcp_url": "http://members.example/mcp"}
    with pytest.raises(subs.SubscriptionError, match="isn't HTTPS"):
        await subs.search(insecure, GOOD_KEY, "x")


def test_pick_tool_and_argument():
    assert subs.pick_tool(TOOLS, SUB) == ("search_episodes", "query")
    pinned = {**SUB, "search_tool": "list_episodes", "query_arg": "limit"}
    assert subs.pick_tool(TOOLS, pinned) == ("list_episodes", "limit")
    with pytest.raises(subs.SubscriptionError):
        subs.pick_tool([{"name": "get_stats", "inputSchema": {}}], SUB)
    odd = [{"name": "ask_archive", "inputSchema": {"properties": {"question": {"type": "string"}}}}]
    assert subs.pick_tool(odd, SUB) == ("ask_archive", "question")


def test_plain_text_results_become_passages():
    result = {"content": [{"type": "text", "text": "First useful paragraph about pricing tiers and packaging.\n\n"
                                                   "Second paragraph about annual discounts and churn."}]}
    ps = subs.to_passages(result, SUB)
    assert len(ps) == 2 and ps[0]["url"] == "https://pod.example"


# ── Server integration ───────────────────────────────────────────────────────

def ctx_with(headers):
    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"), headers=headers)
    return SimpleNamespace(request_context=SimpleNamespace(request=req))


@pytest.fixture
def srv(server_factory, tmp_path, monkeypatch, creator, mock_http):
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [
        {"name": "Pricing Weekly", "url": "https://good.substack.com", "aliases": ["Pat"]}, SUB_ENTRY]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    monkeypatch.setenv("CITED_LIBRARY_DB", str(tmp_path / "library.db"))
    posts = [{"title": f"P{i}", "slug": f"p{i}", "post_date": "2024-01-0%dT00:00:00Z" % (i + 1),
              "body_html": "<p>" + "Pricing strategy: anchor on value and test tiers. " * 8 + "</p>",
              "audience": "everyone"} for i in range(2)]
    mock_http["https://good.substack.com/api/v1/posts?limit=25"] = httpx.Response(200, json={"posts": posts})
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    pipeline.prewarm()
    return s


async def test_search_library_merges_users_subscription(srv):
    await srv._LIBRARY.refresh("good")
    out = json.loads(await srv.search_library("how should I anchor pricing?",
                                              ctx=ctx_with({"X-Cited-Key-Pod": GOOD_KEY})))
    assert "Pod Members MCP (Host Name)" in out["routing"]["searched"]
    assert "sent to their server using your key" in out["routing"]["reason"]
    sub_results = [r for r in out["results"] if r.get("via_subscription")]
    assert sub_results and sub_results[0]["attribution"]["text"] == "The Pod with Host Name"
    assert any(not r.get("via_subscription") for r in out["results"])      # library results too


async def test_no_key_means_no_subscription(srv, creator):
    await srv._LIBRARY.refresh("good")
    out = json.loads(await srv.search_library("pricing", ctx=ctx_with({})))
    assert "Pod Members MCP (Host Name)" not in out["routing"]["searched"]
    assert creator.calls == []                                             # never contacted


async def test_named_routing_and_errors_dont_break_search(srv, creator):
    await srv._LIBRARY.refresh("good")
    out = json.loads(await srv.search_library("What does Host Name say about pricing?",
                                              ctx=ctx_with({"X-Cited-Key-Pod": GOOD_KEY})))
    assert out["routing"]["searched"] == ["Pod Members MCP (Host Name)"]   # named → only that source
    out = json.loads(await srv.search_library("pricing", ctx=ctx_with({"X-Cited-Key-Pod": "bad-key-999"})))
    assert "rejected your key" in out["routing"]["subscription_errors"][0]
    assert out["results"]                                                  # library still answers
    assert "bad-key-999" not in json.dumps(out)
    assert "bad-key-999" not in json.dumps(srv._events.query(limit=200))   # never logged


async def test_list_library_shows_connection_state(srv):
    out = json.loads(await srv.list_library(ctx=ctx_with({"x-cited-key-pod": GOOD_KEY})))
    assert out["subscriptions"] == [{"name": "Pod Members MCP (Host Name)", "connected": True,
        "how_to_connect": "Add a request header to your Cited connector: x-cited-key-pod: <your member key>"}]
    out = json.loads(await srv.list_library(ctx=ctx_with({})))
    assert out["subscriptions"][0]["connected"] is False


async def test_local_mode_uses_env_key(server_factory, tmp_path, monkeypatch, creator):
    f = tmp_path / "lib.json"
    f.write_text(json.dumps({"sources": [SUB_ENTRY]}))
    monkeypatch.setenv("CITED_LIBRARY_FILE", str(f))
    monkeypatch.setenv("CITED_KEY_POD", GOOD_KEY)
    s = server_factory()                                                   # local stdio mode
    pipeline.prewarm()
    out = json.loads(await s.search_library("pricing anchor"))
    assert out["results"] and all(r["via_subscription"] for r in out["results"])
