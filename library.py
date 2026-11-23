"""
Cited library: permanent, incrementally-updated indexes for popular newsletters.

For each newsletter in library.json, Cited keeps the FULL archive of free posts
— their text chunks and embeddings — in SQLite on the persistent disk. Searches
on those newsletters use the stored index, so they're fast from the first query
and survive restarts and deploys.

Refreshing is incremental:
  * posts are fetched newest-first; paging stops at the first page whose posts
    are all already stored and unchanged (content fingerprint);
  * only new or changed posts are chunked and embedded;
  * a full rescan (to drop deleted / newly-paywalled posts) runs weekly;
  * if the embedding model changes, the source is rebuilt from scratch.

Feeds that only list recent items (Medium, most RSS/podcast feeds) still grow
over time: posts are never dropped just because they scrolled off the feed.

Env:
  CITED_LIBRARY=0                 disable the library
  CITED_LIBRARY_FILE              path to the source list (default library.json)
  CITED_LIBRARY_REFRESH_MINUTES   refresh interval (default 60)
  CITED_LIBRARY_POST_CHARS        max characters kept per post (default 60000)
  CITED_LIBRARY_DB                database file (default: library.db next to the history DB)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import fetcher as _fetcher
import pipeline as _pipeline

log = logging.getLogger("cited.library")

FULL_RESCAN_SECONDS = 7 * 86400
DEFAULT_FILE = Path(__file__).parent / "library.json"


@dataclass
class Index:
    chunks: list
    embs: np.ndarray
    bm25: object
    posts: int
    built_at: float


def _post_hash(p) -> str:
    return hashlib.sha256(f"{p.title}\x00{p.date}\x00{p.text}".encode("utf-8", "replace")).hexdigest()[:24]


def load_sources(path: Optional[Path] = None) -> tuple[list[dict], list[dict]]:
    """Return (sources with URLs, entries still waiting for a URL)."""
    path = Path(os.getenv("CITED_LIBRARY_FILE") or path or DEFAULT_FILE)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return [], []
    ready, pending = [], []
    for entry in raw.get("sources", []):
        url = (entry.get("url") or "").strip()
        if not url:
            pending.append({"name": entry.get("name", "?"), "note": entry.get("note", "")})
            continue
        url = _fetcher.upgrade_to_https(url).rstrip("/")
        ready.append({"name": entry.get("name") or url, "url": url,
                      "key": _fetcher.parse_subdomain(url)})
    return ready, pending


class Library:
    def __init__(self, *, enabled: bool, db_path: Callable[[], Path],
                 sources: list[dict], pending: Optional[list[dict]] = None,
                 max_chars: int = 60_000):
        self.enabled   = enabled
        self._db_path  = db_path
        self.sources   = {s["key"]: s for s in sources}
        self.pending   = pending or []
        self.max_chars = max_chars
        self._index: dict[str, Index] = {}
        self._lock     = threading.Lock()
        self._refreshing: set[str] = set()
        self._errors: dict[str, str] = {}

    # ── Storage ───────────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path(), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE IF NOT EXISTS lib_sources (
            key TEXT PRIMARY KEY, url TEXT, name TEXT, model TEXT,
            last_refresh REAL, last_full REAL, last_ok REAL, last_error TEXT,
            posts INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0, last_added INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS lib_posts (
            key TEXT NOT NULL, url TEXT NOT NULL, title TEXT, date TEXT, audience TEXT,
            hash TEXT NOT NULL, fetched REAL, PRIMARY KEY (key, url))""")
        c.execute("""CREATE TABLE IF NOT EXISTS lib_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, post_url TEXT NOT NULL,
            idx INTEGER NOT NULL, title TEXT, date TEXT, text TEXT NOT NULL, emb BLOB NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS lib_chunks_key ON lib_chunks(key, post_url)")
        return c

    def _source_row(self, key: str) -> Optional[sqlite3.Row]:
        db = self._conn()
        try:
            return db.execute("SELECT * FROM lib_sources WHERE key = ?", (key,)).fetchone()
        finally:
            db.close()

    # ── Lookup / search ───────────────────────────────────────────────────────
    def resolve(self, url: str) -> Optional[str]:
        """Library key for a user-supplied URL (ignores 'www.' and any post path)."""
        if not self.enabled:
            return None
        try:
            key = _fetcher.parse_subdomain(_fetcher.upgrade_to_https(url))
        except _fetcher.NotSubstackError:
            return None
        bare = key[4:] if key.startswith("www.") else key
        for candidate in (key, bare, f"www.{bare}"):
            if candidate in self.sources:
                return candidate
        return None

    def has(self, key: str) -> bool:
        return self.enabled and key in self.sources

    def ready(self, key: str) -> bool:
        return self.enabled and key in self._index

    def search(self, key: str, query: str, top_k: int) -> tuple[list[dict], int]:
        idx = self._index[key]
        return _pipeline.search_index(query, idx.chunks, idx.embs, idx.bm25, top_k), idx.posts

    def posts(self, key: str, limit: int = 300) -> list[dict]:
        db = self._conn()
        try:
            rows = db.execute("SELECT title, url, date FROM lib_posts WHERE key = ? "
                              "ORDER BY date DESC LIMIT ?", (key, limit)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM lib_posts WHERE key = ?", (key,)).fetchone()[0]
        finally:
            db.close()
        return [dict(r) for r in rows] + ([{"_total": total}] if total > limit else [])

    def updated_at(self, key: str) -> Optional[float]:
        idx = self._index.get(key)
        return idx.built_at if idx else None

    # ── Loading ───────────────────────────────────────────────────────────────
    def load_index(self, key: str) -> None:
        """(Re)build the in-memory index for a source from the database."""
        db = self._conn()
        try:
            rows = db.execute("SELECT post_url, title, date, text, emb FROM lib_chunks WHERE key = ? "
                              "ORDER BY date DESC, post_url, idx", (key,)).fetchall()
            n_posts = db.execute("SELECT COUNT(*) FROM lib_posts WHERE key = ?", (key,)).fetchone()[0]
        finally:
            db.close()
        if not rows:
            with self._lock:
                self._index.pop(key, None)
            return
        chunks = [_pipeline.Chunk(title=r["title"], url=r["post_url"], date=r["date"], text=r["text"])
                  for r in rows]
        embs = np.vstack([np.frombuffer(r["emb"], dtype=np.float16) for r in rows]).astype(np.float32)
        index = Index(chunks=chunks, embs=embs, bm25=_pipeline.build_bm25(chunks),
                      posts=n_posts, built_at=time.time())
        with self._lock:
            self._index[key] = index
        log.info("Library index loaded for %s: %d posts, %d chunks", key, n_posts, len(chunks))

    def load_all(self) -> None:
        for key in self.sources:
            try:
                self.load_index(key)
            except Exception:
                log.exception("Could not load library index for %s", key)

    # ── Refreshing ────────────────────────────────────────────────────────────
    async def refresh(self, key: str, full: bool = False) -> dict:
        """Fetch new/changed posts, embed only those, store them, reload the index."""
        src = self.sources[key]
        if key in self._refreshing:
            return {"key": key, "skipped": "already refreshing"}
        self._refreshing.add(key)
        started = time.time()
        try:
            row = await asyncio.to_thread(self._source_row, key)
            model_changed = bool(row and row["model"] and row["model"] != _pipeline.EMBED_MODEL_NAME)
            full = full or model_changed or not row or not row["last_full"] \
                or started - (row["last_full"] or 0) > FULL_RESCAN_SECONDS

            known = {} if model_changed else await asyncio.to_thread(self._known_hashes, key)

            def stop(page) -> bool:
                return (not full) and bool(page) and all(known.get(p.url) == _post_hash(p) for p in page)

            posts = await _fetcher.fetch_archive(src["url"], max_chars=self.max_chars,
                                                 free_only=True, stop=stop)
            changed = [p for p in posts if known.get(p.url) != _post_hash(p)]
            fetched_urls = {p.url for p in posts}
            removed = (set(known) - fetched_urls) if (full and _fetcher._detect_source(src["url"]) == "substack") else set()

            result = await asyncio.to_thread(self._store, key, src, changed, removed, full, model_changed)
            await asyncio.to_thread(self.load_index, key)
            self._errors.pop(key, None)
            result.update(key=key, seconds=round(time.time() - started, 1), full=full)
            log.info("Library refresh %s: %s", key, result)
            return result
        except Exception as exc:
            self._errors[key] = f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(self._note_error, key, src, self._errors[key])
            log.warning("Library refresh failed for %s: %s", key, exc)
            return {"key": key, "error": self._errors[key]}
        finally:
            self._refreshing.discard(key)

    def _known_hashes(self, key: str) -> dict[str, str]:
        db = self._conn()
        try:
            return {r["url"]: r["hash"] for r in db.execute("SELECT url, hash FROM lib_posts WHERE key = ?", (key,))}
        finally:
            db.close()

    def _store(self, key, src, changed, removed, full, model_changed) -> dict:
        chunks = _pipeline.chunk_posts(changed)
        embs = _pipeline.embed_texts([c.text for c in chunks]) if chunks else np.zeros((0, 384), np.float32)
        now = time.time()
        db = self._conn()
        try:
            if model_changed:
                db.execute("DELETE FROM lib_chunks WHERE key = ?", (key,))
                db.execute("DELETE FROM lib_posts WHERE key = ?", (key,))
            for url in removed:
                db.execute("DELETE FROM lib_chunks WHERE key = ? AND post_url = ?", (key, url))
                db.execute("DELETE FROM lib_posts WHERE key = ? AND url = ?", (key, url))
            for p in changed:
                db.execute("DELETE FROM lib_chunks WHERE key = ? AND post_url = ?", (key, p.url))
                db.execute("""INSERT INTO lib_posts (key, url, title, date, audience, hash, fetched)
                              VALUES (?, ?, ?, ?, ?, ?, ?)
                              ON CONFLICT(key, url) DO UPDATE SET title=excluded.title, date=excluded.date,
                                audience=excluded.audience, hash=excluded.hash, fetched=excluded.fetched""",
                           (key, p.url, p.title, p.date, p.audience, _post_hash(p), now))
            per_post: dict[str, int] = {}
            for c, e in zip(chunks, embs):
                i = per_post.get(c.url, 0)
                per_post[c.url] = i + 1
                db.execute("INSERT INTO lib_chunks (key, post_url, idx, title, date, text, emb) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (key, c.url, i, c.title, c.date, c.text, e.astype(np.float16).tobytes()))
            n_posts = db.execute("SELECT COUNT(*) FROM lib_posts WHERE key = ?", (key,)).fetchone()[0]
            n_chunks = db.execute("SELECT COUNT(*) FROM lib_chunks WHERE key = ?", (key,)).fetchone()[0]
            db.execute("""INSERT INTO lib_sources (key, url, name, model, last_refresh, last_full, last_ok,
                            last_error, posts, chunks, last_added)
                          VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                          ON CONFLICT(key) DO UPDATE SET url=excluded.url, name=excluded.name,
                            model=excluded.model, last_refresh=excluded.last_refresh,
                            last_full=COALESCE(excluded.last_full, last_full), last_ok=excluded.last_ok,
                            last_error=NULL, posts=excluded.posts, chunks=excluded.chunks,
                            last_added=excluded.last_added""",
                       (key, src["url"], src["name"], _pipeline.EMBED_MODEL_NAME, now,
                        now if full else None, now, n_posts, n_chunks, len(changed)))
            db.commit()
        finally:
            db.close()
        return {"new_or_changed_posts": len(changed), "removed_posts": len(removed),
                "chunks_embedded": len(chunks), "posts": n_posts}

    def _note_error(self, key, src, error) -> None:
        db = self._conn()
        try:
            db.execute("""INSERT INTO lib_sources (key, url, name, last_refresh, last_error)
                          VALUES (?, ?, ?, ?, ?)
                          ON CONFLICT(key) DO UPDATE SET last_refresh=excluded.last_refresh,
                            last_error=excluded.last_error""",
                       (key, src["url"], src["name"], time.time(), error[:500]))
            db.commit()
        finally:
            db.close()

    async def run_forever(self, interval: float, models_ready: Callable[[], bool]) -> None:
        """Load stored indexes, wait for the models, then refresh every source periodically."""
        await asyncio.to_thread(self.load_all)
        while not models_ready():
            await asyncio.sleep(5)
        while True:
            for key in list(self.sources):
                await self.refresh(key)
            await asyncio.sleep(interval)

    # ── Status ────────────────────────────────────────────────────────────────
    def status(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "sources": [], "pending": self.pending}
        db = self._conn()
        try:
            rows = {r["key"]: dict(r) for r in db.execute("SELECT * FROM lib_sources")}
        finally:
            db.close()
        out = []
        for key, src in self.sources.items():
            r = rows.get(key, {})
            idx = self._index.get(key)
            failed = bool(self._errors.get(key) or r.get("last_error"))
            state = ("refreshing" if key in self._refreshing else
                     "stale" if failed and idx else      # still searchable; last refresh failed
                     "error" if failed else
                     "ready" if idx else "building")
            out.append({"key": key, "name": src["name"], "url": src["url"], "state": state,
                        "label": _fetcher.display_name(key),
                        "posts": r.get("posts") or (idx.posts if idx else 0),
                        "chunks": r.get("chunks") or (len(idx.chunks) if idx else 0),
                        "last_refresh": r.get("last_refresh"), "last_ok": r.get("last_ok"),
                        "last_full": r.get("last_full"), "last_added": r.get("last_added") or 0,
                        "error": self._errors.get(key) or r.get("last_error")})
        return {"enabled": True, "sources": out, "pending": self.pending}
