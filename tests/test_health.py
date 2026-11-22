import httpx
import pytest

import pipeline


@pytest.fixture(autouse=True)
def models_unloaded(monkeypatch):
    """Each test starts with models not yet loaded."""
    monkeypatch.setattr(pipeline, "_embed_model", None)
    monkeypatch.setattr(pipeline, "_reranker", None)


def _client(server):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.build_http_app()),
                             base_url="http://t")


@pytest.fixture
def http_server(server_factory):
    return server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")


async def test_live_always_ok(http_server):
    async with _client(http_server) as c:
        r = await c.get("/health/live")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_not_ready_while_models_load(http_server):
    http_server._HEALTH.prewarm_started()
    async with _client(http_server) as c:
        ready = await c.get("/health/ready")
        full = await c.get("/health")
    assert ready.status_code == 503
    assert ready.json()["checks"]["models"] == "starting"
    assert full.status_code == 503 and full.json()["status"] == "unavailable"


async def test_ready_after_models_load(http_server):
    pipeline.prewarm()
    async with _client(http_server) as c:
        r = await c.get("/health/ready")
    assert r.status_code == 200, r.json()
    assert r.json() == {"status": "ok", "checks": {
        "models": "ok", "search_selftest": "ok", "mcp_tools": "ok", "history_db": "ok"}}


async def test_full_report_contents(http_server):
    pipeline.prewarm()
    http_server._HEALTH.prewarm_finished()
    async with _client(http_server) as c:
        r = await c.get("/health")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] in ("ok", "degraded")
    assert body["version"] == "1.0.0"
    assert body["mcp_endpoint"] == "/mcp"
    assert sorted(body["tools"]) == ["list_posts", "search_newsletter"]
    assert body["rate_limit_per_minute"] == 15
    assert body["prewarm"]["state"] == "ready"
    for key in ("models", "search_selftest", "mcp_tools", "history_db", "memory", "disk", "event_loop"):
        assert key in body["checks"]
    assert body["checks"]["search_selftest"]["latency_ms"] >= 0
    assert isinstance(body["uptime_seconds"], int)


async def test_prewarm_failure_reported(http_server):
    http_server._HEALTH.prewarm_started()
    http_server._HEALTH.prewarm_finished(RuntimeError("download failed"))
    async with _client(http_server) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert "download failed" in r.json()["checks"]["models"]["detail"]


async def test_selftest_detects_broken_search(http_server, monkeypatch):
    pipeline.prewarm()
    monkeypatch.setattr(pipeline, "search", lambda *a, **k: [])
    async with _client(http_server) as c:
        r = await c.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["search_selftest"] == "error"


async def test_selftest_exception_is_503_not_crash(http_server, monkeypatch):
    pipeline.prewarm()

    def boom(*a, **k):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(pipeline, "search", boom)
    async with _client(http_server) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert "model exploded" in r.json()["checks"]["search_selftest"]["detail"]


async def test_selftest_result_is_cached(http_server, monkeypatch):
    pipeline.prewarm()
    calls = []
    real = pipeline.search
    monkeypatch.setattr(pipeline, "search", lambda *a, **k: calls.append(1) or real(*a, **k))
    async with _client(http_server) as c:
        first = (await c.get("/health")).json()["checks"]["search_selftest"]
        second = (await c.get("/health")).json()["checks"]["search_selftest"]
    assert len(calls) == 1
    assert first["cached"] is False and second["cached"] is True


async def test_selftest_does_not_pollute_embedding_cache(http_server):
    pipeline.prewarm()
    pipeline._EMBED_CACHE.clear()
    async with _client(http_server) as c:
        await c.get("/health/ready")
    assert pipeline._EMBED_CACHE == {}


async def test_unwritable_history_db_is_unavailable(http_server, monkeypatch, tmp_path):
    import history
    pipeline.prewarm()
    monkeypatch.setattr(history, "DB_PATH", tmp_path / "missing-dir" / "h.db")
    async with _client(http_server) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["history_db"]["status"] == "error"


async def test_history_checks_disabled_without_logging(server_factory):
    s = server_factory(CITED_TRANSPORT="http")   # no admin token → no logging
    pipeline.prewarm()
    async with _client(s) as c:
        body = (await c.get("/health")).json()
    assert body["checks"]["history_db"]["status"] == "disabled"
    assert body["checks"]["disk"]["status"] == "disabled"
    assert body["status"] in ("ok", "degraded")


async def test_memory_warning_degrades_but_stays_200(http_server, monkeypatch):
    monkeypatch.setenv("CITED_MEMORY_WARN_MB", "1")
    pipeline.prewarm()
    async with _client(http_server) as c:
        r = await c.get("/health")
        ready = await c.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["memory"]["status"] == "warn"
    assert r.json()["checks"]["memory"]["rss_mb"] > 0
    assert ready.status_code == 200    # warnings never fail readiness


async def test_head_trailing_slash_and_method(http_server):
    async with _client(http_server) as c:
        head = await c.head("/health/live")
        slash = await c.get("/health/live/")
        post = await c.post("/health")
        root = await c.get("/")
    assert head.status_code == 200 and head.content == b""
    assert slash.status_code == 200
    assert post.status_code == 405
    assert root.status_code == 200 and root.json()["health"] == "/health"


async def test_health_handler_crash_returns_503(http_server, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(http_server._HEALTH, "report", broken)
    async with _client(http_server) as c:
        r = await c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"
