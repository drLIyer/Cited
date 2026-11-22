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


# ── Coverage levels (ThinkWithMe / Distill limits) ───────────────────────────

def _hits(*sims):
    return [{"similarity": s} for s in sims]


def test_coverage_levels_match_distill_rules():
    assert pipeline.coverage(_hits(0.62, 0.59, 0.56))["level"] == "high"     # top≥.62, avg≥.59
    assert pipeline.coverage(_hits(0.70, 0.50, 0.50))["level"] == "medium"   # avg too low for high
    assert pipeline.coverage(_hits(0.55))["level"] == "medium"
    assert pipeline.coverage(_hits(0.549))["level"] == "low"
    assert pipeline.coverage([])["level"] == "low"


def test_coverage_uses_best_match_regardless_of_rerank_order():
    # hits are in cross-encoder order; the best cosine match may not be first
    cov = pipeline.coverage(_hits(0.40, 0.66, 0.60))
    assert cov["top_similarity"] == 0.66 and cov["percent"] == 66


def test_coverage_labels_and_colors():
    low = pipeline.coverage(_hits(0.2))
    assert low["label"] == "Limited coverage — draws on general AI knowledge"
    assert low["color"] == "#e74c3c"
    assert pipeline.coverage(_hits(0.7, 0.7))["color"] == "#27ae60"


def test_search_hits_include_similarity():
    chunks = pipeline.chunk_posts(_posts([LONG]))
    hit = pipeline.search("pricing strategy", chunks, cache_key=None)[0]
    assert -1.0 <= hit["similarity"] <= 1.0


def test_history_stats_by_level(monkeypatch, tmp_path):
    h = _history(monkeypatch, tmp_path)
    for lvl, conf in [("high", .7), ("medium", .57), ("low", .3), ("low", .2)]:
        h.save("https://a.substack.com", f"q-{lvl}", "x", [], conf, update_memory=False, level=lvl)
    st = h.stats(hours=24)
    assert st["searches"] == 4
    assert st["levels"] == {"high": 1, "medium": 1, "low": 2}
    assert st["low_confidence_pct"] == 50.0
    assert st["by_source"][0]["label"] == "a.substack.com"
    assert [r["question"] for r in h.recent(level="low")] == ["q-low", "q-low"]


def test_history_migrates_old_db_without_level(monkeypatch, tmp_path):
    import sqlite3
    h = _history(monkeypatch, tmp_path)
    db = sqlite3.connect(tmp_path / "h.db")
    db.execute("""CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        url TEXT NOT NULL, subdomain TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
        sources TEXT NOT NULL DEFAULT '[]', confidence REAL, tool TEXT)""")
    db.commit(); db.close()
    h.save("https://a.substack.com", "q", "a", [], 0.6, update_memory=False, level="medium")
    assert h.get()[0]["level"] == "medium"
