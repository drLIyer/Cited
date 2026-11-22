import pipeline
from fetcher import Post
from conftest import FakeSentenceTransformer

LONG = "pricing strategy for b2b saas companies and growth loops " * 10


def _posts(texts):
    return [Post(title=f"T{i}", url=f"https://x/{i}", date="2024-01-01", text=t)
            for i, t in enumerate(texts)]


def test_search_ranks_relevant_chunk():
    chunks = pipeline.chunk_posts(_posts([
        "how to hire engineers and run interviews " * 20,
        LONG,
        "podcast recording equipment and microphones " * 20,
    ]))
    hits = pipeline.search("pricing strategy saas", chunks, top_k=2, cache_key=None)
    assert hits[0]["title"] == "T1"
    assert 0 <= pipeline.retrieval_confidence(hits) <= 1


def test_embedding_cache_hits_for_same_content():
    pipeline._EMBED_CACHE.clear()
    chunks = pipeline.chunk_posts(_posts([LONG]))
    before = FakeSentenceTransformer.encode_calls
    pipeline.search("pricing", chunks, cache_key="k")
    pipeline.search("growth", chunks, cache_key="k")
    # 2 query encodes + 1 corpus encode
    assert FakeSentenceTransformer.encode_calls - before == 3


def test_embedding_cache_invalidated_when_text_changes_with_same_count():
    pipeline._EMBED_CACHE.clear()
    a = pipeline.chunk_posts(_posts(["alpha beta gamma delta " * 20]))
    b = pipeline.chunk_posts(_posts(["pricing strategy saas growth " * 20]))
    assert len(a) == len(b)
    pipeline.search("alpha", a, cache_key="same")
    hits = pipeline.search("pricing strategy", b, cache_key="same")
    # With the old len()-only check, b would be scored against a's vectors.
    fp, _, _ = pipeline._EMBED_CACHE["same"]
    assert fp == pipeline._fingerprint(b)
    assert "pricing" in hits[0]["text"]


def test_search_empty():
    assert pipeline.search("x", [], cache_key=None) == []
    assert pipeline.retrieval_confidence([]) == 0.0


# ── history ───────────────────────────────────────────────────────────────────

def _history(monkeypatch, tmp_path):
    import history
    monkeypatch.setattr(history, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(history, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.setattr(history, "MEMORY_FILE", tmp_path / "mem" / "cited_history.md")
    monkeypatch.setattr(history, "MEMORY_INDEX", tmp_path / "mem" / "MEMORY.md")
    return history


def test_history_labels(monkeypatch, tmp_path):
    h = _history(monkeypatch, tmp_path)
    h.save("https://news.aakashg.com", "q1", "a", [], 0.9)
    h.save("https://aakashgupta.substack.com", "q2", "a", [], 0.9)
    labels = {r["label"] for r in h.get()}
    assert labels == {"news.aakashg.com", "aakashgupta.substack.com"}
    mem = (tmp_path / "mem" / "cited_history.md").read_text()
    assert "### news.aakashg.com\n" in mem
    assert "news.aakashg.com.substack.com" not in mem


def test_history_no_memory_write(monkeypatch, tmp_path):
    h = _history(monkeypatch, tmp_path)
    h.save("https://x.substack.com", "q", "a", [], 0.5, update_memory=False)
    assert not (tmp_path / "mem").exists()
    assert len(h.get()) == 1


def test_history_like_wildcards_escaped(monkeypatch, tmp_path):
    h = _history(monkeypatch, tmp_path)
    h.save("https://x.substack.com", "what is 100% retention", "a", [], 0.5)
    h.save("https://x.substack.com", "unrelated", "b", [], 0.5)
    assert len(h.keyword_search("%")) == 1
    assert len(h.keyword_search("_")) == 0
    assert len(h.keyword_search("100%")) == 1


def test_history_retention_prunes_old_rows(monkeypatch, tmp_path):
    import time as _t
    h = _history(monkeypatch, tmp_path)
    monkeypatch.setattr(h, "RETENTION_DAYS", 90)
    real = _t.time()
    monkeypatch.setattr(h.time, "time", lambda: real - 91 * 86400)
    h.save("https://x.substack.com", "old question", "a", [], 0.5, update_memory=False)
    monkeypatch.setattr(h.time, "time", lambda: real)
    h.save("https://x.substack.com", "new question", "a", [], 0.5, update_memory=False)
    assert [r["question"] for r in h.get()] == ["new question"]
