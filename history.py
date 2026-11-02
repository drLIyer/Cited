"""
Persistent Q&A history for Cited — two layers:

  1. SQLite (~/cited/history.db)
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
DB_PATH  = _HERE / "history.db"

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
    c.commit()
    return c


def save(url: str, question: str, answer: str,
         sources: list, confidence: float, tool: str = "search") -> int:
    """Save a Q&A. Returns the new row id."""
    subdomain = _subdomain(url)
    db = _conn()
    cur = db.execute(
        """INSERT INTO history
             (ts, url, subdomain, question, answer, sources, confidence, tool)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), url, subdomain, question, answer,
         json.dumps(sources), round(confidence, 3), tool),
    )
    row_id = cur.lastrowid
    db.commit()
    db.close()
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
    like = f"%{query}%"
    rows = db.execute(
        """SELECT * FROM history
           WHERE question LIKE ? OR answer LIKE ?
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


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
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
        header  = f"### {subdomain}.substack.com"

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
