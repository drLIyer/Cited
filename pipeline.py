"""
In-memory RAG pipeline: BM25 + vector search → RRF → cross-encoder reranking.

No FAISS needed at this scale — direct numpy dot product over <1000 chunks is
fast enough (< 50 ms) and avoids the extra dependency.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import time
import logging
from dataclasses import dataclass

import numpy as np

from fetcher import Post

log = logging.getLogger("cited.pipeline")

CHUNK_WORDS    = 300
CHUNK_OVERLAP  = 50
RRF_K          = 60
CANDIDATES     = 30   # BM25 + vector each return this many before RRF
_EMBED_TTL     = 3600  # match fetcher cache TTL

# Coverage ("confidence") limits — the same rules as the ThinkWithMe / Distill
# RAG service, which uses the same embedding model (all-MiniLM-L6-v2) and the
# same cosine-similarity scores, so the numbers mean the same thing here.
#   High:     best match ≥ 0.62 AND average of returned matches ≥ 0.59
#   Moderate: best match ≥ 0.55
#   Limited:  anything lower
CONF_HIGH_TOP   = float(os.getenv("CITED_CONF_HIGH_TOP", "0.62"))
CONF_HIGH_AVG   = float(os.getenv("CITED_CONF_HIGH_AVG", "0.59"))
CONF_MEDIUM_TOP = float(os.getenv("CITED_CONF_MEDIUM_TOP", "0.55"))

COVERAGE_LEVELS = {
    "high":   {"label": "High coverage",     "color": "#27ae60"},
    "medium": {"label": "Moderate coverage", "color": "#e67e22"},
    "low":    {"label": "Limited coverage — draws on general AI knowledge",
               "color": "#e74c3c"},
}

# Lazy-loaded models
_embed_model  = None
_reranker     = None

# Per-newsletter embedding cache: key → (fingerprint, matrix, timestamp)
# The fingerprint is a hash of the chunk texts, so a changed feed never reuses
# embeddings computed for different text (even if the chunk count matches).
_EMBED_CACHE: dict[str, tuple[str, np.ndarray, float]] = {}

# Searches run in worker threads (and pre-warm in its own thread), so guard
# lazy model loading and cache mutation.
_MODEL_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()


def _embed():
    global _embed_model
    if _embed_model is None:
        with _MODEL_LOCK:
            if _embed_model is None:
                from sentence_transformers import SentenceTransformer
                log.info("Loading embedding model …")
                _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


def _rerank():
    global _reranker
    if _reranker is None:
        with _MODEL_LOCK:
            if _reranker is None:
                from sentence_transformers import CrossEncoder
                log.info("Loading cross-encoder …")
                _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    return _reranker


def models_loaded() -> bool:
    """True once both the embedding model and the cross-encoder are in memory."""
    return _embed_model is not None and _reranker is not None


def reload_models() -> None:
    """Drop and reload both models (used by the auto-fix agent)."""
    global _embed_model, _reranker
    with _MODEL_LOCK:
        _embed_model = None
        _reranker = None
    prewarm()


def prewarm() -> None:
    """Load both models into memory. Call at server startup so the first user query is fast."""
    log.info("Pre-warming models …")
    _embed()
    _rerank()
    log.info("Models ready.")


def precompute_embeddings(cache_key: str, chunks: list[Chunk]) -> None:
    """Pre-compute and cache embeddings for a newsletter. Call at startup for popular newsletters."""
    model = _embed()
    _get_cached_embeddings(cache_key, chunks, model)
    log.info("Pre-computed embeddings for %s (%d chunks)", cache_key, len(chunks))


# ── Chunking ───────────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    title: str
    url:   str
    date:  str
    text:  str


def chunk_posts(posts: list[Post]) -> list[Chunk]:
    chunks: list[Chunk] = []
    step = CHUNK_WORDS - CHUNK_OVERLAP
    for post in posts:
        words = post.text.split()
        for start in range(0, max(1, len(words) - CHUNK_OVERLAP), step):
            text = " ".join(words[start : start + CHUNK_WORDS]).strip()
            if len(text) < 40:
                continue
            chunks.append(Chunk(
                title=post.title,
                url=post.url,
                date=post.date,
                text=text,
            ))
    return chunks


# ── Tokenizer ─────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return re.findall(r"\b\w+\b", text.lower())


# ── Search ─────────────────────────────────────────────────────────────────────

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"


def search(query: str, chunks: list[Chunk], top_k: int = 5,
           cache_key: str | None = None) -> list[dict]:
    """Search freshly fetched chunks (embeddings cached in memory per newsletter)."""
    if not chunks:
        return []
    c_embs = _get_cached_embeddings(cache_key, chunks, _embed())
    return search_index(query, chunks, c_embs, build_bm25(chunks), top_k)


def build_bm25(chunks: list[Chunk]):
    from rank_bm25 import BM25Okapi
    return BM25Okapi([_tokenize(c.text) for c in chunks])


def embed_texts(texts: list[str]) -> np.ndarray:
    """L2-normalised float32 embeddings for a batch of texts."""
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    embs = _embed().encode(texts, convert_to_numpy=True, batch_size=64,
                           show_progress_bar=False).astype(np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
    return embs


def _top(scores, k: int) -> list[int]:
    scores = np.asarray(scores, dtype=np.float32)
    if k >= len(scores):
        return list(np.argsort(-scores, kind="stable"))
    part = np.argpartition(-scores, k)[:k]
    return list(part[np.argsort(-scores[part], kind="stable")])


def rerank_scores(query: str, texts: list[str]) -> list[float]:
    """Cross-encoder relevance scores for (query, text) pairs."""
    if not texts:
        return []
    return [float(s) for s in _rerank().predict([(query, t) for t in texts])]


def search_index(query: str, chunks: list[Chunk], c_embs: np.ndarray, bm25,
                 top_k: int = 5) -> list[dict]:
    """Hybrid BM25 + vector search → RRF → cross-encoder rerank over a prepared index."""
    n = len(chunks)
    if n == 0:
        return []
    k = min(CANDIDATES, n)

    bm25_top = _top(bm25.get_scores(_tokenize(query)), k)

    q_emb = embed_texts([query])[0]
    sims  = c_embs @ q_emb
    vec_top = _top(sims, k)

    rrf: dict[int, float] = {}
    for rank, idx in enumerate(bm25_top):
        rrf[int(idx)] = rrf.get(int(idx), 0) + 1 / (RRF_K + rank + 1)
    for rank, idx in enumerate(vec_top):
        rrf[int(idx)] = rrf.get(int(idx), 0) + 1 / (RRF_K + rank + 1)
    rrf_top = sorted(rrf, key=lambda i: rrf[i], reverse=True)[:k]

    scores = _rerank().predict([(query, chunks[i].text) for i in rrf_top])
    ranked = sorted(zip(rrf_top, scores), key=lambda x: x[1], reverse=True)[:top_k]

    return [
        {
            "title": chunks[i].title,
            "url":   chunks[i].url,
            "date":  chunks[i].date,
            "text":  chunks[i].text,
            "score": float(s),
            "similarity": float(sims[i]),
        }
        for i, s in ranked
    ]


def _get_cached_embeddings(cache_key: str | None, chunks: list[Chunk],
                            model) -> np.ndarray:
    """Return L2-normalised embeddings for chunks, using cache when possible."""
    now = time.time()
    fp  = _fingerprint(chunks)
    if cache_key:
        cached = _EMBED_CACHE.get(cache_key)
        if cached and now - cached[2] < _EMBED_TTL and cached[0] == fp:
            log.info("Embedding cache hit for %s (%d chunks)", cache_key, len(chunks))
            return cached[1]

    log.info("Computing embeddings for %d chunks (key=%s)…", len(chunks), cache_key)
    c_embs = model.encode([c.text for c in chunks], convert_to_numpy=True,
                          batch_size=64, show_progress_bar=False).astype(np.float32)
    c_embs /= np.linalg.norm(c_embs, axis=1, keepdims=True) + 1e-9

    if cache_key:
        with _CACHE_LOCK:
            _EMBED_CACHE[cache_key] = (fp, c_embs, now)
            # Evict oldest entry if cache grows beyond 20 newsletters
            if len(_EMBED_CACHE) > 20:
                oldest = min(_EMBED_CACHE, key=lambda k: _EMBED_CACHE[k][2])
                del _EMBED_CACHE[oldest]

    return c_embs


def _fingerprint(chunks: list[Chunk]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.url.encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(c.text.encode("utf-8", "replace"))
        h.update(b"\x01")
    return h.hexdigest()


def coverage(hits: list[dict]) -> dict:
    """Coverage level for a result set, using the ThinkWithMe/Distill limits.

    Returns level ('high' | 'medium' | 'low'), label, color, the best and
    average cosine similarity of the returned excerpts, and `percent` — the
    best match as a whole-number percentage, for showing to users.
    """
    if not hits:
        return {"level": "low", "label": "No sources matched",
                "color": COVERAGE_LEVELS["low"]["color"],
                "top_similarity": 0.0, "avg_similarity": 0.0, "percent": 0}
    sims = [h["similarity"] for h in hits]
    top, avg = max(sims), sum(sims) / len(sims)
    if top >= CONF_HIGH_TOP and avg >= CONF_HIGH_AVG:
        level = "high"
    elif top >= CONF_MEDIUM_TOP:
        level = "medium"
    else:
        level = "low"
    return {"level": level, **COVERAGE_LEVELS[level],
            "top_similarity": round(top, 3), "avg_similarity": round(avg, 3),
            "percent": max(0, min(100, round(top * 100)))}


def retrieval_confidence(hits: list[dict]) -> float:
    """Sigmoid of top reranker logit → [0, 1] probability."""
    if not hits:
        return 0.0
    return float(1 / (1 + math.exp(-hits[0]["score"])))


def build_context(hits: list[dict]) -> str:
    parts = [
        f"[{h['title']} — {h['date']}]\n{h['text']}"
        for h in hits
    ]
    return "\n\n---\n\n".join(parts)
