"""
Cited auto-fix agent ("healer").

Runs inside the server's background monitor (every 60 s). It reads the error
log and the latest health report, applies SAFE, temporary or undoable fixes,
checks whether each fix worked, and remembers what worked in a playbook so the
same error is fixed immediately if it ever comes back.

It never changes code, configuration, or data. The only actions it can take:

  reload_models     Reload the search models (when model checks / searches break)
  clear_feed_cache  Drop cached posts + embeddings for one feed (bad cached data)
  pause_source      Stop fetching from a host for 15 min (it keeps failing)
  free_memory       Clear in-memory caches (memory near the limit)
  block_ip          Block one client IP for 60 min (repeated attacks / abuse)

Safety rails:
  * every action has an hourly cap (ACTION_CAPS);
  * each fix is verified; a fix that didn't work is logged as `auto_fix_failed`
    and its playbook entry loses confidence (after 3 failures it's not reused);
  * everything is recorded in the error log and shown on the admin dashboard;
  * CITED_AUTOHEAL=0 turns the agent off.

Storage: `playbook` and `heal_log` tables in the history/events SQLite file.
IP addresses are only ever held in memory (for the block), never stored.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Optional

import events as _events
import fetcher as _fetcher
import pipeline as _pipeline

log = logging.getLogger("cited.healer")

ACTIONS = {
    "reload_models":    "Reload the search models",
    "clear_feed_cache": "Clear cached posts and embeddings for a feed",
    "pause_source":     "Pause fetching from a failing feed for 15 minutes",
    "free_memory":      "Clear in-memory caches to free memory",
    "block_ip":         "Block an attacking IP address for 60 minutes",
}
ACTION_CAPS = {"reload_models": 3, "free_memory": 6, "clear_feed_cache": 20,
               "pause_source": 20, "block_ip": 50}            # max uses per hour

PAUSE_SECONDS      = 15 * 60
BLOCK_SECONDS      = 60 * 60
VERIFY_AFTER       = 120      # seconds before a fix is judged
VERIFY_DEADLINE    = 600      # judged as failed if not verified by then
FETCH_FAIL_TRIGGER = 3        # failures of one feed within the window → pause it
OFFENSE_TRIGGER    = {"blocked_url": 3, "blocked_content": 3, "rate_limited": 10, "admin_auth_failed": 5}
WINDOW             = 600      # seconds
MAX_PLAYBOOK_FAILURES = 3


class Healer:
    def __init__(self, *, enabled: bool, db_path: Callable[[], Path],
                 block_ip: Callable[[str, float], None],
                 clock: Callable[[], float] = time.time):
        self.enabled  = enabled
        self._db_path = db_path
        self._block   = block_ip
        self._clock   = clock
        self._lock    = threading.RLock()
        self._pending: list[dict] = []                      # fixes awaiting verification
        self._uses: dict[str, deque] = defaultdict(deque)   # action → timestamps (cap)
        self._offenses: dict[str, deque] = defaultdict(deque)   # ip → (ts, kind)
        self._last_seen: dict[str, float] = {}              # fingerprint → last_seen handled
        self.last_cycle: Optional[float] = None

    # ── Storage ───────────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path(), timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE IF NOT EXISTS playbook (
            fingerprint TEXT PRIMARY KEY, kind TEXT, message TEXT, action TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}', successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, last_applied REAL,
            last_result TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS heal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, fingerprint TEXT,
            problem TEXT, action TEXT NOT NULL, params TEXT, result TEXT NOT NULL,
            detail TEXT, from_playbook INTEGER NOT NULL DEFAULT 0)""")
        return c

    def _log(self, fp, problem, action, params, result, detail="", from_playbook=False) -> int:
        db = self._conn()
        try:
            cur = db.execute(
                "INSERT INTO heal_log (ts, fingerprint, problem, action, params, result, detail, from_playbook) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._clock(), fp, problem[:500], action, json.dumps(_public(params)), result,
                 detail[:1000], int(from_playbook)))
            db.execute("DELETE FROM heal_log WHERE ts < ?", (self._clock() - 90 * 86400,))
            db.commit()
            return cur.lastrowid
        finally:
            db.close()

    def playbook(self) -> list[dict]:
        if not self.enabled:
            return []
        db = self._conn()
        try:
            rows = db.execute("SELECT * FROM playbook ORDER BY COALESCE(last_applied, created) DESC").fetchall()
        finally:
            db.close()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"] or "{}")
            d["action_name"] = ACTIONS.get(d["action"], d["action"])
            d["trusted"] = d["successes"] > 0 and d["failures"] < MAX_PLAYBOOK_FAILURES
            out.append(d)
        return out

    def forget(self, fingerprint: str) -> bool:
        db = self._conn()
        try:
            cur = db.execute("DELETE FROM playbook WHERE fingerprint = ?", (fingerprint,))
            db.commit()
            return cur.rowcount > 0
        finally:
            db.close()

    def recent_actions(self, limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []
        db = self._conn()
        try:
            rows = db.execute("SELECT * FROM heal_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        finally:
            db.close()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"] or "{}")
            d["action_name"] = ACTIONS.get(d["action"], d["action"])
            out.append(d)
        return out

    def stats(self, hours: float = 24) -> dict:
        if not self.enabled:
            return {"enabled": False}
        db = self._conn()
        try:
            since = self._clock() - hours * 3600
            rows = {r["result"]: r["n"] for r in db.execute(
                "SELECT result, COUNT(*) AS n FROM heal_log WHERE ts >= ? GROUP BY result", (since,))}
            learned = db.execute("SELECT COUNT(*) FROM playbook WHERE successes > 0").fetchone()[0]
        finally:
            db.close()
        return {"enabled": True, "hours": hours, "applied": rows.get("applied", 0),
                "worked": rows.get("worked", 0), "failed": rows.get("failed", 0),
                "skipped": rows.get("skipped", 0), "learned_fixes": learned,
                "paused_sources": len(_fetcher.paused_hosts())}

    # ── Signals from the request path ─────────────────────────────────────────
    def note_offense(self, ip: Optional[str], kind: str) -> None:
        """Called (in memory only) when a client trips a security check."""
        if not self.enabled or not ip or ip == "unknown":
            return
        now = self._clock()
        with self._lock:
            q = self._offenses[ip]
            q.append((now, kind))
            while q and q[0][0] < now - WINDOW:
                q.popleft()
            if len(self._offenses) > 50_000:
                for k in [k for k, v in self._offenses.items() if not v or v[-1][0] < now - WINDOW]:
                    del self._offenses[k]

    # ── Main cycle ────────────────────────────────────────────────────────────
    def run_cycle(self, report: Optional[dict]) -> list[dict]:
        """One pass. `report` is the latest /health payload. Returns actions taken."""
        if not self.enabled:
            return []
        taken: list[dict] = []
        try:
            self._verify_pending(report)
            taken += self._from_health(report or {})
            taken += self._from_events()
            taken += self._from_offenders()
        except Exception:
            log.exception("Auto-fix cycle failed")
        self.last_cycle = self._clock()
        return taken

    # Health-driven fixes
    def _from_health(self, report: dict) -> list[dict]:
        checks = report.get("checks", {})
        out = []
        models = checks.get("models", {}).get("status")
        selftest = checks.get("search_selftest", {}).get("status")
        if models == "error" or selftest == "error":
            which = "models" if models == "error" else "search_selftest"
            out += self._apply(f"health:{which}", f"Health check failed: {which}",
                               "reload_models", {}, verify="search_ok", kind="health")
        if checks.get("memory", {}).get("status") == "warn":
            out += self._apply("health:memory", "Memory near the limit", "free_memory", {},
                               verify="memory_ok", kind="health")
        return out

    # Error-log-driven fixes
    def _from_events(self) -> list[dict]:
        out = []
        now = self._clock()
        playbook = {p["fingerprint"]: p for p in self.playbook()}
        for g in _events.groups(hours=WINDOW / 3600, category="error", limit=200):
            fp = g["fingerprint"]
            if g["last_seen"] <= self._last_seen.get(fp, 0):
                continue                                    # nothing new since we last looked
            if g["state"] == "fixed":                       # admin/agent fix still holding
                self._last_seen[fp] = g["last_seen"]
                continue
            self._last_seen[fp] = g["last_seen"]
            problem = f"{g['name']}: {g['message']}"

            remembered = playbook.get(fp)
            if remembered and remembered["successes"] > 0 and remembered["failures"] < MAX_PLAYBOOK_FAILURES:
                out += self._apply(fp, problem, remembered["action"], remembered["params"],
                                   verify="no_recurrence", kind=g["kind"], message=g["message"],
                                   from_playbook=True)
                continue

            action, params = self._diagnose(g, now)
            if action:
                out += self._apply(fp, problem, action, params, verify="no_recurrence",
                                   kind=g["kind"], message=g["message"])
        return out

    def _diagnose(self, g: dict, now: float) -> tuple[Optional[str], dict]:
        """Rule-based diagnosis: which safe action (if any) fits this error?"""
        ctx = g.get("context") or {}
        url = ctx.get("url") or _first_url(g.get("message", ""))
        detail = (g.get("detail") or "") + "\n" + (g.get("message") or "")
        if g["kind"] == "fetch_failed" and url:
            recent = self._count_recent(g["fingerprint"], now)
            if recent >= FETCH_FAIL_TRIGGER:
                return "pause_source", {"host": _fetcher._host(url)}
            return None, {}
        if g["kind"] in ("exception", "uncaught_exception"):
            if re.search(r"sentence_transformers|torch|CrossEncoder|SentenceTransformer|pipeline\.py", detail):
                return "reload_models", {}
            if url and re.search(r"fetcher\.py|ParseError|JSONDecodeError|UnicodeDecodeError", detail):
                return "clear_feed_cache", {"url": url}
        return None, {}

    def _count_recent(self, fp: str, now: float) -> int:
        rows = _events.query(limit=50, since=now - WINDOW)
        return sum(r["count"] for r in rows if r["fingerprint"] == fp)

    # Abuse-driven fixes
    def _from_offenders(self) -> list[dict]:
        out = []
        now = self._clock()
        with self._lock:
            candidates = []
            for ip, q in list(self._offenses.items()):
                while q and q[0][0] < now - WINDOW:
                    q.popleft()
                counts = defaultdict(int)
                for _, kind in q:
                    counts[kind] += 1
                hit = next((k for k, n in counts.items() if n >= OFFENSE_TRIGGER.get(k, 10**9)), None)
                if hit:
                    candidates.append((ip, hit, counts[hit]))
                    q.clear()
        for ip, kind, n in candidates:
            out += self._apply(f"abuse:{kind}", f"Client repeatedly triggered '{kind}' ({n}× in 10 min)",
                               "block_ip", {"ip": ip}, verify="none", kind="security")
        return out

    # ── Applying & verifying ──────────────────────────────────────────────────
    def _capped(self, action: str) -> bool:
        now = self._clock()
        q = self._uses[action]
        while q and q[0] < now - 3600:
            q.popleft()
        return len(q) >= ACTION_CAPS.get(action, 10)

    def _apply(self, fp, problem, action, params, *, verify, kind, message=None,
               from_playbook=False) -> list[dict]:
        if any(p["fingerprint"] == fp and p["action"] == action for p in self._pending):
            return []                                       # already fixing this
        if self._capped(action):
            self._log(fp, problem, action, params, "skipped", "Hourly limit for this action reached.")
            _events.record("auto_fix_skipped", f"Auto-fix '{ACTIONS[action]}' skipped (hourly limit) for: {problem}",
                           level="WARNING", source="healer", group_key=f"skip|{action}")
            return []
        try:
            detail = self._execute(action, params)
        except Exception as exc:
            self._log(fp, problem, action, params, "failed", f"{type(exc).__name__}: {exc}", from_playbook)
            _events.record("auto_fix_failed", f"Auto-fix '{ACTIONS[action]}' crashed: {exc}",
                           level="ERROR", source="healer", group_key=f"crash|{action}")
            return []
        self._uses[action].append(self._clock())
        self._log(fp, problem, action, params, "applied", detail, from_playbook)
        _events.record("auto_fix", f"{'Re-applied remembered fix' if from_playbook else 'Applied fix'}: "
                       f"{ACTIONS[action]} — for: {problem}",
                       level="INFO", source="healer", context={"action": action, **_public(params)},
                       group_key=f"fix|{fp}|{action}")
        self._remember(fp, kind, message or problem, action, params, applied=True)
        item = {"fingerprint": fp, "problem": problem, "action": action, "params": params,
                "verify": verify, "applied_at": self._clock(), "from_playbook": from_playbook}
        if verify == "none":
            self._settle(item, True, "Applied (no verification needed).")
        else:
            self._pending.append(item)
        return [item]

    def _execute(self, action: str, params: dict) -> str:
        if action == "reload_models":
            _pipeline.reload_models()
            return "Search models reloaded."
        if action == "free_memory":
            n = len(_pipeline._EMBED_CACHE)
            _pipeline._EMBED_CACHE.clear()
            _fetcher._CACHE.clear()
            import gc
            gc.collect()
            return f"Cleared caches ({n} newsletters' embeddings)."
        if action == "clear_feed_cache":
            key = _fetcher.parse_subdomain(params["url"])
            _fetcher.invalidate_key(key)
            _pipeline._EMBED_CACHE.pop(key, None)
            return f"Cleared cached data for {key}."
        if action == "pause_source":
            _fetcher.pause_host(params["host"], PAUSE_SECONDS)
            return f"Paused fetching from {params['host']} for {PAUSE_SECONDS // 60} minutes."
        if action == "block_ip":
            self._block(params["ip"], BLOCK_SECONDS)
            return f"Blocked one client IP for {BLOCK_SECONDS // 60} minutes."
        raise ValueError(f"Unknown action {action}")

    def _verify_pending(self, report: Optional[dict]) -> None:
        now = self._clock()
        still = []
        checks = (report or {}).get("checks", {})
        for item in self._pending:
            age = now - item["applied_at"]
            if age < VERIFY_AFTER:
                still.append(item)
                continue
            ok = None
            if item["verify"] == "search_ok":
                if checks.get("search_selftest", {}).get("status") == "ok" and _pipeline.models_loaded():
                    ok = True
            elif item["verify"] == "memory_ok":
                if checks.get("memory", {}).get("status") == "ok":
                    ok = True
            elif item["verify"] == "no_recurrence":
                recurred = any(r["fingerprint"] == item["fingerprint"] and r["last_seen"] > item["applied_at"] + 5
                               for r in _events.query(limit=200, since=item["applied_at"]))
                ok = False if recurred else True
            if ok is None and age < VERIFY_DEADLINE:
                still.append(item)
                continue
            self._settle(item, bool(ok), "Problem stopped after the fix." if ok
                         else "Problem continued after the fix.")
        self._pending = still

    def _settle(self, item: dict, worked: bool, detail: str) -> None:
        fp, action = item["fingerprint"], item["action"]
        self._log(fp, item["problem"], action, item["params"], "worked" if worked else "failed", detail,
                  item["from_playbook"])
        self._remember(fp, None, None, action, item["params"], worked=worked)
        if worked:
            if not fp.startswith(("health:", "abuse:")):
                _events.resolve(fp, f"Auto-fixed: {ACTIONS[action]}", by="auto")
        else:
            _events.record("auto_fix_failed", f"Auto-fix '{ACTIONS[action]}' did not fix: {item['problem']}",
                           level="WARNING", source="healer", group_key=f"failed|{fp}|{action}")

    def _remember(self, fp, kind, message, action, params, *, applied=False, worked=None) -> None:
        db = self._conn()
        try:
            now = self._clock()
            db.execute("""INSERT INTO playbook (fingerprint, kind, message, action, params, created, last_applied)
                          VALUES (?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(fingerprint) DO UPDATE SET
                            action = excluded.action, params = excluded.params,
                            kind = COALESCE(excluded.kind, kind), message = COALESCE(excluded.message, message),
                            last_applied = COALESCE(excluded.last_applied, last_applied)""",
                       (fp, kind, (message or "")[:500] or None, action, json.dumps(_public(params)), now,
                        now if applied else None))
            if worked is True:
                db.execute("UPDATE playbook SET successes = successes + 1, last_result = 'worked' "
                           "WHERE fingerprint = ?", (fp,))
            elif worked is False:
                db.execute("UPDATE playbook SET failures = failures + 1, last_result = 'failed' "
                           "WHERE fingerprint = ?", (fp,))
            db.commit()
        finally:
            db.close()

    def snapshot(self, blocked: dict[str, int]) -> dict:
        """State for the admin dashboard."""
        return {
            "enabled": self.enabled,
            "last_cycle": self.last_cycle,
            "stats_24h": self.stats(24),
            "stats_30d": self.stats(24 * 30),
            "pending": [{**{k: v for k, v in p.items() if k != "params"}, "params": _public(p["params"])}
                        for p in self._pending],
            "paused_sources": [{"host": h, "seconds_left": round(t - time.time())}
                               for h, t in _fetcher.paused_hosts().items()],
            "blocked_clients": len(blocked),
            "playbook": [{**p, "params": _public(p["params"])} for p in self.playbook()],
            "recent": self.recent_actions(30),
        }


def _public(params: dict) -> dict:
    """Params safe to store/show: never keep an IP address."""
    return {k: ("(hidden)" if k == "ip" else v) for k, v in (params or {}).items()}


def _first_url(text: str) -> Optional[str]:
    m = re.search(r"https?://[^\s'\")]+", text or "")
    return m.group(0) if m else None
