"""
Error & event log for the hosted server, shown in the admin dashboard.

What gets recorded:
  * every WARNING/ERROR/CRITICAL log record from any logger (our code, the MCP
    library, uvicorn, ...) — including full tracebacks;
  * uncaught exceptions in threads, the main thread, and the asyncio event loop;
  * categorised tool problems (failed fetches, blocked URLs, rate-limit hits);
  * health status changes (e.g. ok → unavailable).

Repeated identical events are grouped: the same event within GROUP_WINDOW
seconds bumps a counter instead of adding a row, which also protects the
database from floods. Events older than CITED_EVENT_RETENTION_DAYS (default
30) are pruned, and the table is capped at MAX_ROWS.

Privacy: events never store users' questions or IP addresses. They may store
the newsletter/feed URL involved and the error message.

Storage: the same SQLite file as history (CITED_HISTORY_DB). The store is only
enabled when an admin can read it (hosted mode with CITED_ADMIN_TOKEN set).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("cited.events")

GROUP_WINDOW   = 60.0
MAX_ROWS       = 20_000
RETENTION_DAYS = 30

_lock    = threading.RLock()
_enabled = False
_db_path: Callable[[], Path] = lambda: Path("events.db")
_recent: dict[str, tuple[int, float]] = {}   # fingerprint → (row id, last_seen)
_writing = threading.local()                  # re-entrancy guard
started  = time.time()

# In-memory counters since process start (cheap, always on)
counters: Counter = Counter()

# What each event kind is, for the dashboard. category ∈ error | security | usage | health
KINDS = {
    "exception":          ("error",    "Code error",                 "An exception was raised while handling a request."),
    "uncaught_exception": ("error",    "Crash",                      "An exception escaped all handlers (thread, event loop, or main)."),
    "log":                ("error",    "Logged warning/error",       "Something logged a warning or error without an exception."),
    "fetch_failed":       ("error",    "Couldn't fetch a feed",      "A newsletter or feed couldn't be downloaded or parsed."),
    "invalid_source":     ("usage",    "Unsupported URL",            "A user gave a URL that isn't a supported newsletter or feed."),
    "health_change":      ("health",   "Health status change",       "Overall health moved between ok / degraded / unavailable."),
    "blocked_url":        ("security", "Blocked unsafe URL",         "A request tried to make Cited fetch a private/internal address, "
                                                                     "a non-HTTPS URL, or redirect to one (SSRF attempt)."),
    "blocked_content":    ("security", "Blocked malicious content",  "A feed returned an XML attack (e.g. entity bomb) or an oversized response."),
    "rate_limited":       ("security", "Rate limit hit",             "A client exceeded the searches-per-minute limit (possible abuse)."),
    "admin_auth_failed":  ("security", "Failed admin sign-in",       "Someone used a wrong admin token."),
    "admin_lockout":      ("security", "Admin brute-force lockout",  "Too many wrong admin tokens; admin access was locked temporarily."),
    "blocked_ip":         ("security", "Blocked client",             "A client temporarily blocked by the auto-fix agent tried again."),
    "opted_out":          ("usage",    "Writer opted out of AI",     "A request for a site whose writer opted out of AI tools was declined."),
    "source_paused":      ("usage",    "Feed temporarily paused",    "A request hit a feed the auto-fix agent paused after repeated failures."),
    "auto_fix":           ("autofix",  "Auto-fix applied",           "The auto-fix agent applied a safe fix."),
    "auto_fix_failed":    ("autofix",  "Auto-fix didn't work",       "A fix was applied but the problem continued (or the fix itself failed)."),
    "auto_fix_skipped":   ("autofix",  "Auto-fix skipped",           "A fix was needed but its hourly safety limit was reached."),
}


def kind_info(kind: str) -> dict:
    cat, name, desc = KINDS.get(kind, ("error", kind, ""))
    return {"category": cat, "name": name, "description": desc}


def kinds_in(category: str) -> list[str]:
    return [k for k, (c, _, _) in KINDS.items() if c == category]


def configure(enabled: bool, db_path: Callable[[], Path], retention_days: int = RETENTION_DAYS) -> None:
    global _enabled, _db_path, RETENTION_DAYS
    _enabled, _db_path, RETENTION_DAYS = enabled, db_path, retention_days
    _recent.clear()


def enabled() -> bool:
    return _enabled


# ── Storage ───────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            first_seen  REAL NOT NULL,
            last_seen   REAL NOT NULL,
            count       INTEGER NOT NULL DEFAULT 1,
            level       TEXT NOT NULL,
            kind        TEXT NOT NULL,
            source      TEXT,
            message     TEXT NOT NULL,
            detail      TEXT,
            context     TEXT NOT NULL DEFAULT '{}',
            fingerprint TEXT NOT NULL
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS events_last_seen ON events(last_seen)")
    c.execute("CREATE INDEX IF NOT EXISTS events_fp ON events(fingerprint)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS resolutions (
            fingerprint  TEXT PRIMARY KEY,
            kind         TEXT,
            message      TEXT,
            resolved_at  REAL NOT NULL,
            resolved_by  TEXT NOT NULL,          -- 'admin' or 'auto' (self-healing agent)
            note         TEXT,
            regressed_at REAL,                   -- set if the error happens again after the fix
            regressions  INTEGER NOT NULL DEFAULT 0
        )""")
    return c


def record(kind: str, message: str, *, level: str = "ERROR", source: str = "cited",
           detail: Optional[str] = None, context: Optional[dict] = None,
           group_key: Optional[str] = None) -> None:
    """Record an event. Never raises."""
    counters[f"{level.lower()}:{kind}"] += 1
    if not _enabled or getattr(_writing, "active", False):
        return
    _writing.active = True
    try:
        now = time.time()
        fp = hashlib.sha1(f"{level}|{kind}|{source}|{group_key or message}".encode(
            "utf-8", "replace")).hexdigest()
        with _lock:
            db = _conn()
            try:
                hit = _recent.get(fp)
                if hit and now - hit[1] < GROUP_WINDOW:
                    cur = db.execute(
                        "UPDATE events SET count = count + 1, last_seen = ?, detail = COALESCE(?, detail) "
                        "WHERE id = ?", (now, detail, hit[0]))
                    if cur.rowcount:
                        _recent[fp] = (hit[0], now)
                        db.commit()
                        return
                db.execute(
                    "UPDATE resolutions SET regressed_at = ?, regressions = regressions + 1 "
                    "WHERE fingerprint = ? AND resolved_at < ? AND regressed_at IS NULL",
                    (now, fp, now))
                cur = db.execute(
                    "INSERT INTO events (first_seen, last_seen, count, level, kind, source, "
                    "message, detail, context, fingerprint) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (now, now, level, kind, source, message[:2000], (detail or "")[:20000] or None,
                     json.dumps(context or {}, default=str)[:4000], fp))
                _recent[fp] = (cur.lastrowid, now)
                if len(_recent) > 5000:
                    for k in [k for k, (_, t) in _recent.items() if now - t >= GROUP_WINDOW]:
                        del _recent[k]
                if cur.lastrowid % 200 == 0:
                    _prune(db, now)
                db.commit()
            finally:
                db.close()
    except Exception as exc:  # the error log must never break the app
        try:
            sys.stderr.write(f"cited.events: could not record event: {exc}\n")
        except Exception:
            pass
    finally:
        _writing.active = False


def _prune(db: sqlite3.Connection, now: float) -> None:
    if RETENTION_DAYS > 0:
        db.execute("DELETE FROM events WHERE last_seen < ?", (now - RETENTION_DAYS * 86400,))
    db.execute("DELETE FROM events WHERE id NOT IN "
               "(SELECT id FROM events ORDER BY last_seen DESC LIMIT ?)", (MAX_ROWS,))


def query(limit: int = 100, level: Optional[str] = None, kind: Optional[str] = None,
          since: Optional[float] = None) -> list[dict]:
    if not _enabled:
        return []
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if level:
        levels = (["WARNING", "ERROR", "CRITICAL"] if level.lower() == "problems"
                  else [lv.strip().upper() for lv in level.split(",") if lv.strip()])
        sql += f" AND level IN ({','.join('?' * len(levels))})"; args.extend(levels)
    if kind:
        sql += " AND kind = ?"; args.append(kind)
    if since:
        sql += " AND last_seen >= ?"; args.append(since)
    sql += " ORDER BY last_seen DESC LIMIT ?"; args.append(max(1, min(int(limit), 1000)))
    with _lock:
        db = _conn()
        try:
            rows = db.execute(sql, args).fetchall()
        finally:
            db.close()
    out = []
    for r in rows:
        d = dict(r)
        d["context"] = json.loads(d.get("context") or "{}")
        out.append(d)
    return out


def groups(hours: float = 24, category: Optional[str] = None, level: Optional[str] = None,
           limit: int = 200) -> list[dict]:
    """Events aggregated by type (fingerprint) — one row per distinct problem."""
    if not _enabled:
        return []
    since = time.time() - hours * 3600
    sql = """SELECT e.fingerprint, e.kind, e.level, e.source,
                    SUM(e.count) AS count, MIN(e.first_seen) AS first_seen, MAX(e.last_seen) AS last_seen,
                    (SELECT message FROM events m WHERE m.fingerprint = e.fingerprint
                       ORDER BY m.last_seen DESC LIMIT 1) AS message,
                    (SELECT detail FROM events d WHERE d.fingerprint = e.fingerprint AND d.detail IS NOT NULL
                       ORDER BY d.last_seen DESC LIMIT 1) AS detail,
                    (SELECT context FROM events x WHERE x.fingerprint = e.fingerprint
                       ORDER BY x.last_seen DESC LIMIT 1) AS context,
                    r.resolved_at, r.resolved_by, r.note, r.regressed_at, r.regressions
             FROM events e LEFT JOIN resolutions r ON r.fingerprint = e.fingerprint
             WHERE e.last_seen >= ?"""
    args: list = [since]
    if category:
        kinds = kinds_in(category)
        if category == "error":   # unknown kinds count as errors
            sql += f" AND (e.kind IN ({','.join('?' * len(kinds))}) OR e.kind NOT IN ({','.join('?' * len(KINDS))}))"
            args += kinds + list(KINDS)
        else:
            sql += f" AND e.kind IN ({','.join('?' * len(kinds))})"
            args += kinds
    if level:
        levels = (["WARNING", "ERROR", "CRITICAL"] if level.lower() == "problems"
                  else [lv.strip().upper() for lv in level.split(",") if lv.strip()])
        sql += f" AND e.level IN ({','.join('?' * len(levels))})"
        args += levels
    sql += " GROUP BY e.fingerprint ORDER BY last_seen DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _lock:
        db = _conn()
        try:
            rows = db.execute(sql, args).fetchall()
        finally:
            db.close()
    out = []
    for r in rows:
        d = dict(r)
        d["context"] = json.loads(d.get("context") or "{}")
        d.update(kind_info(d["kind"]))
        if d["resolved_at"] is None:
            d["state"] = "open"
        elif d["regressed_at"] is not None:
            d["state"] = "regressed"
        else:
            d["state"] = "fixed"
        out.append(d)
    return out


def resolve(fingerprint: str, note: str = "", by: str = "admin") -> bool:
    """Mark an error type as fixed. Returns False if no such error exists."""
    if not _enabled:
        return False
    with _lock:
        db = _conn()
        try:
            row = db.execute("SELECT kind, message FROM events WHERE fingerprint = ? "
                             "ORDER BY last_seen DESC LIMIT 1", (fingerprint,)).fetchone()
            if not row:
                return False
            db.execute(
                """INSERT INTO resolutions (fingerprint, kind, message, resolved_at, resolved_by, note)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET resolved_at = excluded.resolved_at,
                     resolved_by = excluded.resolved_by, note = excluded.note, regressed_at = NULL""",
                (fingerprint, row["kind"], row["message"], time.time(), by, (note or "")[:2000]))
            db.commit()
            # a fresh occurrence must not instantly count as a regression of this fix
            _recent.pop(fingerprint, None)
            return True
        finally:
            db.close()


def reopen(fingerprint: str) -> bool:
    if not _enabled:
        return False
    with _lock:
        db = _conn()
        try:
            cur = db.execute("DELETE FROM resolutions WHERE fingerprint = ?", (fingerprint,))
            db.commit()
            return cur.rowcount > 0
        finally:
            db.close()


def fix_stats(days: float = 30) -> dict:
    if not _enabled:
        return {"enabled": False}
    since = time.time() - days * 86400
    with _lock:
        db = _conn()
        try:
            r = db.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN regressed_at IS NULL THEN 1 ELSE 0 END) AS holding,
                          SUM(CASE WHEN regressed_at IS NOT NULL THEN 1 ELSE 0 END) AS regressed,
                          SUM(CASE WHEN resolved_by = 'auto' THEN 1 ELSE 0 END) AS auto
                   FROM resolutions WHERE resolved_at >= ?""", (since,)).fetchone()
            open_errors = db.execute(
                f"""SELECT COUNT(DISTINCT e.fingerprint) FROM events e
                    LEFT JOIN resolutions r ON r.fingerprint = e.fingerprint
                    WHERE e.last_seen >= ? AND e.level IN ('ERROR', 'CRITICAL')
                      AND e.kind NOT IN ({','.join('?' * len(kinds_in('security')))})
                      AND (r.fingerprint IS NULL OR r.regressed_at IS NOT NULL)""",
                [since] + kinds_in("security")).fetchone()[0]
        finally:
            db.close()
    return {"enabled": True, "days": days, "fixed": r["total"] or 0, "holding": r["holding"] or 0,
            "regressed": r["regressed"] or 0, "auto_fixed": r["auto"] or 0, "open_errors": open_errors}


def security_summary() -> dict:
    """Threats blocked, per type, over 24 h / 7 d / 30 d."""
    kinds = kinds_in("security")
    if not _enabled:
        return {"enabled": False}
    now = time.time()
    out: dict = {"enabled": True, "types": []}
    with _lock:
        db = _conn()
        try:
            for k in kinds:
                row = {"kind": k, **kind_info(k)}
                for label, secs in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)):
                    row[label] = db.execute(
                        "SELECT COALESCE(SUM(count), 0) FROM events WHERE kind = ? AND last_seen >= ?",
                        (k, now - secs)).fetchone()[0]
                out["types"].append(row)
        finally:
            db.close()
    for label in ("24h", "7d", "30d"):
        out[f"total_{label}"] = sum(t[label] for t in out["types"])
    return out


def summary(hours: float = 24) -> dict:
    """Counts per level and kind over the last `hours` (counting grouped repeats)."""
    if not _enabled:
        return {"enabled": False, "since_start": dict(counters)}
    since = time.time() - hours * 3600
    with _lock:
        db = _conn()
        try:
            by_level = {r["level"]: r["n"] for r in db.execute(
                "SELECT level, SUM(count) AS n FROM events WHERE last_seen >= ? GROUP BY level", (since,))}
            by_kind = {r["kind"]: r["n"] for r in db.execute(
                "SELECT kind, SUM(count) AS n FROM events WHERE last_seen >= ? GROUP BY kind "
                "ORDER BY n DESC", (since,))}
        finally:
            db.close()
    return {"enabled": True, "hours": hours, "by_level": by_level, "by_kind": by_kind,
            "since_start": dict(counters)}


# ── Capture hooks ─────────────────────────────────────────────────────────────

class _DBLogHandler(logging.Handler):
    """Stores WARNING+ log records from every logger."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("cited.events"):
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        detail = None
        if record.exc_info:
            detail = "".join(traceback.format_exception(*record.exc_info))
        elif record.stack_info:
            detail = record.stack_info
        exc_name = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
        # Group by the message template, not the formatted text, so the same
        # problem with different arguments lands in one group.
        globals()["record"](
            "exception" if record.exc_info else "log", message,
            level=record.levelname, source=record.name, detail=detail,
            context={"where": f"{record.pathname}:{record.lineno}", "function": record.funcName},
            group_key=f"{record.name}|{record.msg}|{exc_name}")


_handler: Optional[_DBLogHandler] = None


def install_capture() -> None:
    """Attach the log handler and uncaught-exception hooks (idempotent)."""
    global _handler
    root = logging.getLogger()
    if _handler is None:
        _handler = _DBLogHandler(level=logging.WARNING)
        root.addHandler(_handler)

    prev_thread_hook = threading.excepthook

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            record("uncaught_exception", f"Uncaught {args.exc_type.__name__} in thread "
                   f"{getattr(args.thread, 'name', '?')}: {args.exc_value}",
                   level="CRITICAL", source="thread",
                   detail="".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                             args.exc_traceback)))
        prev_thread_hook(args)

    if getattr(threading.excepthook, "__name__", "") != "thread_hook":
        threading.excepthook = thread_hook

    prev_sys_hook = sys.excepthook

    def sys_hook(exc_type, exc, tb):
        record("uncaught_exception", f"Uncaught {exc_type.__name__}: {exc}", level="CRITICAL",
               source="main", detail="".join(traceback.format_exception(exc_type, exc, tb)))
        prev_sys_hook(exc_type, exc, tb)

    if getattr(sys.excepthook, "__name__", "") != "sys_hook":
        sys.excepthook = sys_hook


def install_loop_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Record exceptions the event loop would otherwise only print."""
    if getattr(loop, "_cited_handler", False):
        return
    prev = loop.get_exception_handler()

    def handler(lp, ctx):
        exc = ctx.get("exception")
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else None
        record("uncaught_exception", ctx.get("message") or repr(exc), level="ERROR",
               source="event_loop", detail=detail)
        if prev is not None:
            prev(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)
    loop._cited_handler = True
