"""
Test setup: no network, no model downloads.

* sentence_transformers is replaced with tiny deterministic fakes
  (bag-of-words hashing embedder, word-overlap cross-encoder).
* DNS resolution is faked via FAKE_DNS so SSRF checks are exercised for real.
* HTTP is served by httpx.MockTransport via the `mock_http` fixture.
"""
import os
import sys
import types
import threading
import importlib

import numpy as np
import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── Fake sentence_transformers ────────────────────────────────────────────────

def _words(text):
    import re
    return re.findall(r"\w+", text.lower())


class FakeSentenceTransformer:
    encode_calls = 0
    threads: list = []

    def __init__(self, name):
        self.name = name

    def encode(self, texts, convert_to_numpy=True, **kw):
        FakeSentenceTransformer.encode_calls += 1
        FakeSentenceTransformer.threads.append(threading.get_ident())
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in _words(t):
                out[i, hash(w) % 64] += 1.0
        return out


class FakeCrossEncoder:
    def __init__(self, name):
        self.name = name

    def predict(self, pairs):
        scores = []
        for q, doc in pairs:
            qw, dw = set(_words(q)), set(_words(doc))
            scores.append(float(len(qw & dw)) - 1.0)
        return np.array(scores)


_st = types.ModuleType("sentence_transformers")
_st.SentenceTransformer = FakeSentenceTransformer
_st.CrossEncoder = FakeCrossEncoder
sys.modules["sentence_transformers"] = _st


# ── Fake DNS ──────────────────────────────────────────────────────────────────

FAKE_DNS = {
    "public.example": ["93.184.216.34"],
    "feeds.example":  ["93.184.216.35"],
    "news.example":   ["93.184.216.36"],
    "good.substack.com": ["104.18.0.1"],
    "medium.com":     ["162.159.152.4"],
    "internal.example": ["10.0.0.5"],
    "rebind.example": ["93.184.216.40", "127.0.0.1"],
    "v6local.example": ["::1"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    import fetcher, socket

    async def _resolve(host, port):
        if host not in FAKE_DNS:
            raise socket.gaierror(f"unknown host {host}")
        return FAKE_DNS[host]

    monkeypatch.setattr(fetcher, "_resolve", _resolve)
    fetcher._CACHE.clear()
    fetcher._FEED_FOR.clear()
    fetcher.PERMITTED_HOSTS.clear()
    import optout
    optout.clear_cache()
    # The AI opt-out check is exercised in test_optout.py; elsewhere it's off so
    # request-sequence assertions only see the requests under test.
    monkeypatch.setenv("CITED_RESPECT_AI_OPTOUT", "0")
    yield


@pytest.fixture
def mock_http(monkeypatch):
    """Register routes: mock_http[url] = httpx.Response | callable(request)."""
    import fetcher
    routes: dict = {}
    seen: list = []

    def handler(request: httpx.Request):
        url = str(request.url)
        seen.append(url)
        r = routes.get(url)
        if r is None:
            return httpx.Response(404, text="not found")
        return r(request) if callable(r) else r

    monkeypatch.setattr(
        fetcher, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  follow_redirects=False))
    routes["__seen__"] = seen
    return routes


@pytest.fixture
def server_factory(monkeypatch, tmp_path):
    """Import a fresh `server` module under the given env vars."""
    def make(**env):
        for k in ("CITED_TRANSPORT", "CITED_ADMIN_TOKEN", "CITED_ADMIN_MCP_TOOL",
                  "CITED_LOG_QUERIES", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import history
        monkeypatch.setattr(history, "DB_PATH", tmp_path / "history.db")
        monkeypatch.setattr(history, "MEMORY_DIR", tmp_path / "memory")
        monkeypatch.setattr(history, "MEMORY_FILE", tmp_path / "memory" / "cited_history.md")
        monkeypatch.setattr(history, "MEMORY_INDEX", tmp_path / "memory" / "MEMORY.md")
        sys.modules.pop("server", None)
        return importlib.import_module("server")
    yield make
    sys.modules.pop("server", None)
