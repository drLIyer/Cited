"""
In-memory RAG pipeline: BM25 + vector search → RRF → cross-encoder reranking.

No FAISS needed at this scale — direct numpy dot product over <1000 chunks is
fast enough (< 50 ms) and avoids the extra dependency.
"""
import math
import re
import logging
from dataclasses import dataclass

import numpy as np

from fetcher import Post

log = logging.getLogger("pulsebot-mcp.pipeline")

CHUNK_WORDS    = 300
CHUNK_OVERLAP  = 50
RRF_K          = 60
CANDIDATES     = 30   # BM25 + vector each return this many before RRF

# Lazy-loaded — first call takes ~5 s to download models
_embed_model  = None
_reranker     = None


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

def search(query: str, chunks: list[Chunk], top_k: int = 5) -> list[dict]:
    if not chunks:
        return []

    n = len(chunks)
    k = min(CANDIDATES, n)

    # BM25
    from rank_bm25 import BM25Okapi
    corpus     = [_tokenize(c.text) for c in chunks]
    bm25       = BM25Okapi(corpus)
    bm25_scores = bm25.get_scores(_tokenize(query))
    bm25_top   = sorted(range(n), key=lambda i: bm25_scores[i], reverse=True)[:k]

    # Vector (cosine via dot product on L2-normalised vectors)
    model  = _embed()
    q_emb  = model.encode([query], convert_to_numpy=True)[0].astype(np.float32)
    q_emb /= np.linalg.norm(q_emb) + 1e-9

    c_embs  = model.encode([c.text for c in chunks], convert_to_numpy=True).astype(np.float32)
    c_embs /= np.linalg.norm(c_embs, axis=1, keepdims=True) + 1e-9
    sims    = c_embs @ q_emb
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
