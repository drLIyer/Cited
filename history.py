"""
Persistent Q&A history for Cited — two layers:

  1. SQLite (history.db next to this file, or $CITED_HISTORY_DB)
     Full Q&A, sources, confidence, timestamps. Survives restarts.
     Queryable by newsletter URL or keyword.

  2. Claude memory file (~/.claude/projects/.../memory/cited_history.md)
     Brief per-newsletter summaries auto-loaded into every new Claude session.
     New chats already know what you've researched — no need to ask.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────

_HERE    = Path(__file__).parent
DB_PATH  = Path(os.getenv("CITED_HISTORY_DB", str(_HERE / "history.db")))

# Entries older than this are deleted automatically (enforces the retention
# period promised in PRIVACY.md). 0 disables pruning.
RETENTION_DAYS = int(os.getenv("CITED_HISTORY_RETENTION_DAYS", "90"))

# Claude auto-memory: ~/.claude/projects/<home-slug>/memory/
# Slug = home path with / replaced by -  (e.g. /Users/liyer_1 → -Users-liyer-1)
_home        = Path.home()
_slug        = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(_home))
MEMORY_DIR   = Path(os.getenv(
    "CITED_MEMORY_DIR",
    str(_home / ".claude" / "projects" / _slug / "memory"),
))
MEMORY_FILE  = MEMORY_DIR / "cited_history.md"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


# ── SQLite ─────────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL    NOT NULL,
            url        TEXT    NOT NULL,
            subdomain  TEXT    NOT NULL,
            question   TEXT    NOT NULL,
            answer     TEXT    NOT NULL,
            sources    TEXT    NOT NULL DEFAULT '[]',
            confidence REAL,
            tool       TEXT
        )
    """)
    cols = {r[1] for r in c.execute("PRAGMA table_info(history)")}
    if "level" not in cols:   # added with ThinkWithMe-style coverage levels
        c.execute("ALTER TABLE history ADD COLUMN level TEXT")
    c.commit()
    return c


def save(url: str, question: str, answer: str,
         sources: list, confidence: float, tool: str = "search",
         update_memory: bool = True, level: Optional[str] = None) -> int:
    """Save a Q&A. Returns the new row id.

    update_memory=False skips the Claude memory file (used in hosted HTTP mode,
    where writing into the server's ~/.claude makes no sense).
    """
    subdomain = _subdomain(url)
    db = _conn()
    cur = db.execute(
        """INSERT INTO history
             (ts, url, subdomain, question, answer, sources, confidence, tool, level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), url, subdomain, question, answer,
         json.dumps(sources), round(confidence, 3), tool, level),
    )
    row_id = cur.lastrowid
    if RETENTION_DAYS > 0:
        db.execute("DELETE FROM history WHERE ts < ?",
                   (time.time() - RETENTION_DAYS * 86400,))
    db.commit()
    db.close()
    if update_memory:
        _update_memory(subdomain, question)
    return row_id


def get(url: Optional[str] = None, limit: int = 20) -> list[dict]:
    """Return recent Q&As, optionally filtered to one newsletter."""
    db = _conn()
    if url:
        rows = db.execute(
            "SELECT * FROM history WHERE subdomain=? ORDER BY ts DESC LIMIT ?",
            (_subdomain(url), limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    db.close()
    return [_to_dict(r) for r in rows]


def keyword_search(query: str, limit: int = 10) -> list[dict]:
    """Simple keyword search across questions and answers."""
    db   = _conn()
    esc  = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    rows = db.execute(
        """SELECT * FROM history
           WHERE question LIKE ? ESCAPE '\\' OR answer LIKE ? ESCAPE '\\'
           ORDER BY ts DESC LIMIT ?""",
        (like, like, limit),
    ).fetchall()
    db.close()
    return [_to_dict(r) for r in rows]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _subdomain(url: str) -> str:
    try:
        from fetcher import parse_subdomain
        return parse_subdomain(url)
    except Exception:
        return url.rstrip("/")


def label(subdomain: str) -> str:
    """Display label for a stored newsletter key (adds .substack.com only for bare subdomains)."""
    try:
        from fetcher import display_name
        return display_name(subdomain)
    except Exception:
        return subdomain


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["label"]   = label(d["subdomain"])
    d["sources"] = json.loads(d.get("sources") or "[]")
    d["date"]    = time.strftime("%Y-%m-%d", time.localtime(d["ts"]))
    return d


# ── Claude memory ──────────────────────────────────────────────────────────────

_MEMORY_TEMPLATE = """\
---
name: cited-history
description: Newsletters researched via Cited MCP — topics explored per author, auto-updated
metadata:
  type: project
---

Tracks every newsletter researched via Cited and the questions asked.
In any new session, use `get_history` to retrieve full answers.

## Researched newsletters

"""


def _update_memory(subdomain: str, question: str) -> None:
    """Write a one-line entry to Claude's memory after each Q&A."""
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        date  = time.strftime("%Y-%m-%d")
        topic = question.strip()[:70].rstrip("?., ")
        entry = f"- {date}: {topic}"

        content = MEMORY_FILE.read_text() if MEMORY_FILE.exists() else _MEMORY_TEMPLATE
        header  = f"### {label(subdomain)}"

        if header in content:
            # Avoid duplicate entries for the same topic on the same day
            if entry in content:
                return
            content = content.replace(header, f"{header}\n{entry}", 1)
        else:
            content = content.rstrip() + f"\n\n{header}\n{entry}\n"

        MEMORY_FILE.write_text(content)
        _ensure_index()
    except Exception:
        pass  # never let a memory write break a tool call


def _ensure_index() -> None:
    """Add cited_history.md pointer to MEMORY.md if absent."""
    if not MEMORY_INDEX.exists():
        return
    idx   = MEMORY_INDEX.read_text()
    line  = "- [Cited History](cited_history.md) — Newsletters researched via Cited and questions asked"
    if "cited_history.md" not in idx:
        MEMORY_INDEX.write_text(idx.rstrip() + f"\n{line}\n")


def check_writable() -> None:
    """Raise if the history DB can't be opened and written (used by health checks)."""
    db = _conn()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
    finally:
        db.close()


# ── Dashboard statistics ──────────────────────────────────────────────────────

_LEVEL_SUMS = """SUM(CASE WHEN level = 'high'   THEN 1 ELSE 0 END) AS high,
                  SUM(CASE WHEN level = 'medium' THEN 1 ELSE 0 END) AS medium,
                  SUM(CASE WHEN level = 'low'    THEN 1 ELSE 0 END) AS low"""


def stats(hours: float = 24) -> dict:
    """Search volume and coverage (confidence) statistics for the admin dashboard.

    `avg_confidence` is the average best-match similarity (0–1); levels use the
    ThinkWithMe/Distill limits recorded at search time.
    """
    since = time.time() - hours * 3600
    db = _conn()
    try:
        row = db.execute(
            f"""SELECT COUNT(*) AS n, AVG(confidence) AS avg_conf, {_LEVEL_SUMS}
                FROM history WHERE ts >= ? AND tool = 'search'""", (since,)).fetchone()
        per_source = db.execute(
            f"""SELECT subdomain, COUNT(*) AS n, AVG(confidence) AS avg_conf, {_LEVEL_SUMS}
                FROM history WHERE ts >= ? AND tool = 'search'
                GROUP BY subdomain ORDER BY n DESC LIMIT 20""", (since,)).fetchall()
        hourly = db.execute(
            """SELECT CAST((ts - ?) / 3600 AS INTEGER) AS bucket, COUNT(*) AS n,
                      SUM(CASE WHEN level = 'low' THEN 1 ELSE 0 END) AS low
               FROM history WHERE ts >= ? AND tool = 'search'
               GROUP BY bucket ORDER BY bucket""", (since, since)).fetchall()
    finally:
        db.close()
    n = row["n"] or 0
    return {
        "hours": hours,
        "searches": n,
        "avg_confidence": round(row["avg_conf"], 3) if row["avg_conf"] is not None else None,
        "levels": {"high": row["high"] or 0, "medium": row["medium"] or 0, "low": row["low"] or 0},
        "low_confidence": row["low"] or 0,
        "low_confidence_pct": round((row["low"] or 0) / n * 100, 1) if n else 0.0,
        "by_source": [
            {"source": r["subdomain"], "label": label(r["subdomain"]), "searches": r["n"],
             "avg_confidence": round(r["avg_conf"], 3) if r["avg_conf"] is not None else None,
             "levels": {"high": r["high"] or 0, "medium": r["medium"] or 0, "low": r["low"] or 0},
             "low_confidence": r["low"] or 0}
            for r in per_source
        ],
        "hourly": [{"hours_ago": round(hours - r["bucket"] - 1), "searches": r["n"],
                    "low_confidence": r["low"] or 0} for r in hourly],
    }


def recent(limit: int = 50, level: Optional[str] = None) -> list[dict]:
    """Most recent searches, optionally only those at one coverage level (e.g. 'low')."""
    db = _conn()
    try:
        if level:
            rows = db.execute(
                "SELECT * FROM history WHERE level = ? ORDER BY ts DESC LIMIT ?",
                (level, limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM history ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    finally:
        db.close()
    return [_to_dict(r) for r in rows]
