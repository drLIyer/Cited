"""
In-memory RAG pipeline: BM25 + vector search → RRF → cross-encoder reranking.

No FAISS needed at this scale — direct numpy dot product over <1000 chunks is
fast enough (< 50 ms) and avoids the extra dependency.
"""
from __future__ import annotations

import math
import re
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

# Lazy-loaded models
_embed_model  = None
_reranker     = None

# Per-newsletter embedding cache: key → (chunks, matrix, timestamp)
_EMBED_CACHE: dict[str, tuple[list, np.ndarray, float]] = {}


def _embed():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        log.info("Loading embedding model …")
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model


def _rerank():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        log.info("Loading cross-encoder …")
        _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    return _reranker


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

def search(query: str, chunks: list[Chunk], top_k: int = 5,
           cache_key: str | None = None) -> list[dict]:
    if not chunks:
        return []

    n = len(chunks)
    k = min(CANDIDATES, n)

    # BM25 (fast, no model needed)
    from rank_bm25 import BM25Okapi
    corpus      = [_tokenize(c.text) for c in chunks]
    bm25        = BM25Okapi(corpus)
    bm25_scores = bm25.get_scores(_tokenize(query))
    bm25_top    = sorted(range(n), key=lambda i: bm25_scores[i], reverse=True)[:k]

    # Vector — use cached embeddings when available
    model = _embed()
    q_emb = model.encode([query], convert_to_numpy=True)[0].astype(np.float32)
    q_emb /= np.linalg.norm(q_emb) + 1e-9

    c_embs = _get_cached_embeddings(cache_key, chunks, model)
    sims   = c_embs @ q_emb
    vec_top = sorted(range(n), key=lambda i: float(sims[i]), reverse=True)[:k]

    # RRF
    rrf: dict[int, float] = {}
    for rank, idx in enumerate(bm25_top):
        rrf[idx] = rrf.get(idx, 0) + 1 / (RRF_K + rank + 1)
    for rank, idx in enumerate(vec_top):
        rrf[idx] = rrf.get(idx, 0) + 1 / (RRF_K + rank + 1)
    rrf_top = sorted(rrf, key=lambda i: rrf[i], reverse=True)[:k]

    # Cross-encoder reranking
    reranker = _rerank()
    pairs    = [(query, chunks[i].text) for i in rrf_top]
    scores   = reranker.predict(pairs)

    ranked = sorted(zip(rrf_top, scores), key=lambda x: x[1], reverse=True)[:top_k]

    return [
        {
            "title": chunks[i].title,
            "url":   chunks[i].url,
            "date":  chunks[i].date,
            "text":  chunks[i].text,
            "score": float(s),
        }
        for i, s in ranked
    ]


def _get_cached_embeddings(cache_key: str | None, chunks: list[Chunk],
                            model) -> np.ndarray:
    """Return L2-normalised embeddings for chunks, using cache when possible."""
    now = time.time()
    if cache_key:
        cached = _EMBED_CACHE.get(cache_key)
        if cached and now - cached[2] < _EMBED_TTL and len(cached[0]) == len(chunks):
            log.info("Embedding cache hit for %s (%d chunks)", cache_key, len(chunks))
            return cached[1]

    log.info("Computing embeddings for %d chunks (key=%s)…", len(chunks), cache_key)
    c_embs = model.encode([c.text for c in chunks], convert_to_numpy=True,
                          batch_size=64, show_progress_bar=False).astype(np.float32)
    c_embs /= np.linalg.norm(c_embs, axis=1, keepdims=True) + 1e-9

    if cache_key:
        _EMBED_CACHE[cache_key] = (chunks, c_embs, now)
        # Evict oldest entry if cache grows beyond 20 newsletters
        if len(_EMBED_CACHE) > 20:
            oldest = min(_EMBED_CACHE, key=lambda k: _EMBED_CACHE[k][2])
            del _EMBED_CACHE[oldest]

    return c_embs


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
