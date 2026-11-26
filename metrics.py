"""
Traction metrics for the admin dashboard: searches and newsletters per day,
plus an approximate count of distinct clients.

Distinct clients, privately:
  * each client IP is turned into HMAC(daily_salt, ip); the salt is random,
    lives ONLY in memory, and is replaced every UTC day — so stored values
    can't be traced back to an IP, or linked across days;
  * per-day hashes are rolled up into a plain daily count and deleted once the
    day is over; only counts are kept long-term.

Caveat shown on the dashboard: claude.ai connector traffic arrives from
Anthropic's servers, not users' devices, so "distinct clients" undercounts
claude.ai users. Claude Code users connect directly and are counted.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

_lock = threading.Lock()
_enabled = False
_db_path: Callable[[], Path] = lambda: Path("metrics.db")
_salt: tuple[str, bytes] = ("", b"")
_seen_today: set[str] = set()


def configure(enabled: bool, db_path: Callable[[], Path]) -> None:
    global _enabled, _db_path, _salt
    _enabled, _db_path, _salt = enabled, db_path, ("", b"")
    _seen_today.clear()


def _today(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now or time.time(), timezone.utc).date().isoformat()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS client_days (day TEXT NOT NULL, h TEXT NOT NULL, "
              "PRIMARY KEY (day, h))")
    c.execute("CREATE TABLE IF NOT EXISTS client_daily (day TEXT PRIMARY KEY, n INTEGER NOT NULL)")
    return c


def _rollup(db: sqlite3.Connection, today: str) -> None:
    """Turn finished days' hashes into counts, then delete the hashes."""
    db.execute("""INSERT INTO client_daily (day, n)
                  SELECT day, COUNT(*) FROM client_days WHERE day < ? GROUP BY day
                  ON CONFLICT(day) DO UPDATE SET n = n + excluded.n""", (today,))
    db.execute("DELETE FROM client_days WHERE day < ?", (today,))
    db.execute("DELETE FROM client_daily WHERE day < ?",
               ((datetime.fromisoformat(today) - timedelta(days=400)).date().isoformat(),))


def note_client(ip: Optional[str], now: Optional[float] = None) -> None:
    """Count this client once per day (never raises, never stores the IP)."""
    global _salt
    if not _enabled or not ip or ip == "unknown":
        return
    try:
        day = _today(now)
        with _lock:
            if _salt[0] != day:
                _salt = (day, os.urandom(32))
                _seen_today.clear()
                db = _conn()
                try:
                    _rollup(db, day)
                    db.commit()
                finally:
                    db.close()
            h = hmac.new(_salt[1], ip.encode(), hashlib.sha256).hexdigest()[:20]
            if h in _seen_today:
                return
            _seen_today.add(h)
            db = _conn()
            try:
                db.execute("INSERT OR IGNORE INTO client_days (day, h) VALUES (?, ?)", (day, h))
                db.commit()
            finally:
                db.close()
    except Exception:
        pass


def growth(history_db: Path, days: int = 30, now: Optional[float] = None) -> dict:
    """Per-day searches, distinct newsletters and approximate clients, plus week-over-week."""
    end = datetime.fromtimestamp(now or time.time(), timezone.utc).date()
    start = end - timedelta(days=days - 1)
    series = {(start + timedelta(days=i)).isoformat(): {"searches": 0, "newsletters": 0, "clients": 0}
              for i in range(days)}

    t_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()
    wk = 7 * 86400
    nl_this = nl_prev = 0
    hdb = sqlite3.connect(history_db, timeout=5)
    try:
        if hdb.execute("SELECT name FROM sqlite_master WHERE name='history'").fetchone():
            for day, n, nl in hdb.execute(
                    """SELECT date(ts, 'unixepoch') AS d, COUNT(*), COUNT(DISTINCT subdomain)
                       FROM history WHERE tool = 'search' AND ts >= ? GROUP BY d""",
                    (datetime.combine(start, datetime.min.time(), timezone.utc).timestamp(),)):
                if day in series:
                    series[day]["searches"], series[day]["newsletters"] = n, nl
            q = ("SELECT COUNT(DISTINCT subdomain) FROM history WHERE tool='search' "
                 "AND ts >= ? AND ts < ?")
            nl_this = hdb.execute(q, (t_end - wk, t_end)).fetchone()[0]
            nl_prev = hdb.execute(q, (t_end - 2 * wk, t_end - wk)).fetchone()[0]
    finally:
        hdb.close()

    if _enabled:
        with _lock:
            db = _conn()
            try:
                for r in db.execute("SELECT day, n FROM client_daily WHERE day >= ?", (start.isoformat(),)):
                    if r["day"] in series:
                        series[r["day"]]["clients"] = r["n"]
                for r in db.execute("SELECT day, COUNT(*) AS n FROM client_days WHERE day >= ? GROUP BY day",
                                    (start.isoformat(),)):
                    if r["day"] in series:
                        series[r["day"]]["clients"] += r["n"]
            finally:
                db.close()

    rows = [{"day": d, **v} for d, v in series.items()]
    last7, prev7 = rows[-7:], rows[-14:-7]

    def change(a, b):
        return None if b == 0 else round((a - b) / b * 100)

    s_this, s_prev = sum(r["searches"] for r in last7), sum(r["searches"] for r in prev7)
    c_this = round(sum(r["clients"] for r in last7) / 7, 1)
    c_prev = round(sum(r["clients"] for r in prev7) / 7, 1)
    return {
        "days": rows,
        "week": {
            "searches": s_this, "searches_prev": s_prev, "searches_change_pct": change(s_this, s_prev),
            "newsletters": nl_this, "newsletters_prev": nl_prev, "newsletters_change_pct": change(nl_this, nl_prev),
            "avg_daily_clients": c_this, "avg_daily_clients_prev": c_prev,
            "clients_change_pct": change(c_this, c_prev),
        },
        "clients_enabled": _enabled,
    }
