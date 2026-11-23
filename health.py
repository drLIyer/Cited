"""
Health checks for the hosted (HTTP) server.

Endpoints (wired up in server.build_http_app):

  GET /health/live   Liveness. 200 whenever the process can answer HTTP.
                     Use for "is it hung / should it be restarted?" probes.
  GET /health/ready  Readiness. 200 only when Cited can actually serve searches
                     (models loaded, end-to-end search self-test passing, history
                     DB writable when logging is on); 503 otherwise. Point the
                     platform health check here (render.yaml / railway.toml) so
                     deploys only receive traffic once they work.
  GET /health        Full status report (same pass/fail as /ready, plus details:
                     version, commit, uptime, memory, disk, event-loop lag, ...).

Status values:  ok        → everything healthy                     (HTTP 200)
                degraded  → serving, but a warning needs attention  (HTTP 200)
                unavailable → cannot serve searches right now       (HTTP 503)

Every check is local. Nothing here calls Substack, Medium, or any other
third party, so their outages never mark Cited unhealthy (and the endpoint
can't be used to make the server hammer them).

Env vars:
  CITED_MEMORY_WARN_PCT   warn when RSS exceeds this % of the container limit (default 90)
  CITED_MEMORY_WARN_MB    warn when RSS exceeds this many MB (default: off)
  CITED_DISK_WARN_MB      warn when free disk next to the history DB is below this (default 200)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import pipeline as _pipeline

log = logging.getLogger("cited.health")

_HERE = Path(__file__).parent

OK, WARN, ERROR, STARTING, DISABLED = "ok", "warn", "error", "starting", "disabled"

SELFTEST_TTL     = 60.0   # seconds a self-test result is reused (health can't be used to burn CPU)
SELFTEST_TIMEOUT = 20.0
LOOP_LAG_WARN    = 0.5    # seconds


def _read_version() -> str:
    try:
        return json.loads((_HERE / ".claude-plugin" / "plugin.json").read_text())["version"]
    except Exception:
        return "unknown"


VERSION = _read_version()
COMMIT  = (os.getenv("RENDER_GIT_COMMIT") or os.getenv("RAILWAY_GIT_COMMIT_SHA")
           or os.getenv("CITED_COMMIT") or "")[:12] or None


# ── System metrics ────────────────────────────────────────────────────────────

def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import resource, sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    except Exception:
        return None


def _memory_limit_mb() -> float | None:
    """Container memory limit from cgroup v2 / v1, if any."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        if val >= 1 << 60:   # "unlimited" sentinel on cgroup v1
            return None
        return val / (1024 * 1024)
    return None


# ── Plain-English explanations ────────────────────────────────────────────────

CHECK_LABELS = {
    "models":          "Search models",
    "search_selftest": "Search self-test",
    "mcp_tools":       "MCP tools",
    "history_db":      "History database",
    "memory":          "Memory",
    "disk":            "Disk space",
    "event_loop":      "Responsiveness",
    "search_quality":  "Search quality (confidence)",
    "db_size":         "Database size",
    "library":         "Newsletter library",
    "recent_errors":   "Recent errors",
}

# (check, status) → (what it means, what to do)
EXPLAIN = {
    ("models", STARTING): (
        "The AI search models are still loading. This normally takes 30–90 seconds after a deploy or restart.",
        "Wait a minute. If it lasts more than ~5 minutes, check the Render logs for download errors."),
    ("models", ERROR): (
        "The search models could not be loaded, so searches will fail.",
        "Check the Render logs. Common causes: out of memory during loading, or the model download "
        "(huggingface.co) failing. Restart the service; upgrade the plan if memory is the cause."),
    ("search_selftest", STARTING): (
        "The self-test runs once the models are loaded.",
        "Nothing to do — wait for the models."),
    ("search_selftest", ERROR): (
        "A built-in test search failed, so real searches are probably failing too.",
        "Open the Errors section below for the exception, then check the Render logs. Restarting often clears it."),
    ("mcp_tools", ERROR): (
        "The MCP tools Claude calls are not registered, so Claude can't use Cited.",
        "This points to a code or deploy problem. Redeploy the last working version."),
    ("history_db", ERROR): (
        "Searches can't be saved to the history database (the dashboard and logs will be incomplete).",
        "Check that CITED_HISTORY_DB points to a writable location (e.g. a Render persistent disk) "
        "and that the disk isn't full."),
    ("memory", WARN): (
        "The server is close to its memory limit. If it runs out, Render restarts it and searches fail meanwhile.",
        "Upgrade to a plan with more RAM, or lower CITED_MAX_POSTS / CITED_POST_CHARS."),
    ("disk", WARN): (
        "Free disk space is low; the history and error logs may soon stop saving.",
        "Increase the persistent disk size or lower CITED_HISTORY_RETENTION_DAYS."),
    ("library", WARN): (
        "Some library newsletters couldn't be refreshed. Searches still use their last stored index, "
        "but new posts aren't being added.",
        "Open the Library panel to see the error. A changed URL or a feed that blocks requests is the "
        "usual cause — fix the URL in library.json, then press Refresh."),
    ("db_size", WARN): (
        "The history/error database is getting large, which slows the dashboard and uses disk.",
        "Lower CITED_HISTORY_RETENTION_DAYS or CITED_EVENT_RETENTION_DAYS, or raise "
        "CITED_DB_SIZE_WARN_MB if you have plenty of disk."),
    ("search_quality", WARN): (
        "Most recent searches had Limited coverage — users are asking about topics their "
        "newsletters barely cover, so answers lean on general AI knowledge.",
        "Look at 'Low-confidence searches' below: if one newsletter dominates, its feed may be "
        "truncated or failing; otherwise it's a coverage gap in the sources users pick."),
    ("recent_errors", WARN): (
        "Several errors happened in the last hour.",
        "Open the Errors section: group by type, fix the most frequent first. 'fetch_failed' "
        "spikes often mean a source changed its API or is blocking requests."),
    ("event_loop", WARN): (
        "The server was slow to respond — it may be overloaded.",
        "If this persists, upgrade to a plan with more CPU or lower the rate limit."),
}

SUMMARY = {
    "ok":          "Everything is working. Cited is serving searches normally.",
    "degraded":    "Cited is serving searches, but something needs attention soon.",
    "unavailable": "Cited cannot serve searches right now.",
}


def _explain(checks: dict) -> None:
    for name, check in checks.items():
        check["label"] = CHECK_LABELS.get(name, name)
        info = EXPLAIN.get((name, check["status"]))
        if info:
            check["meaning"], check["action"] = info


# ── Checker ───────────────────────────────────────────────────────────────────

_SELFTEST_CHUNKS = [
    ("selftest-match", "Annual pricing tiers help SaaS companies capture more value from customers."),
    ("selftest-other", "Recording a podcast needs a quiet room and a decent microphone."),
    ("selftest-other2", "Hiring engineers works best with structured interviews and work samples."),
]


class HealthChecker:
    def __init__(self, *,
                 list_tools: Callable[[], Awaitable[list[str]]],
                 log_queries: bool,
                 db_path: Callable[[], Path],
                 db_check: Callable[[], None],
                 rate_limit_per_min: int | None = None,
                 on_status_change: Callable[[str | None, str, dict], None] | None = None,
                 recent_levels: Callable[[], list[str]] | None = None,
                 low_conf_alert: float = 0.5,
                 recent_errors: Callable[[], int | None] | None = None,
                 library_status: Callable[[], dict] | None = None,
                 error_alert: int = 5):
        self._list_tools  = list_tools
        self._log_queries = log_queries
        self._db_path     = db_path
        self._db_check    = db_check
        self._rate_limit  = rate_limit_per_min
        self._on_change   = on_status_change
        self._recent_levels = recent_levels
        self._low_conf_alert = low_conf_alert
        self._recent_errors = recent_errors
        self._library_status = library_status
        self._error_alert   = error_alert
        self.last_status: str | None = None
        self.started      = time.time()
        self.prewarm      = {"state": "not_started", "error": None}
        self._selftest: tuple[float, dict] | None = None
        self._selftest_lock = asyncio.Lock()

    # Prewarm bookkeeping (called from the prewarm thread) --------------------
    def prewarm_started(self) -> None:
        self.prewarm = {"state": "loading", "error": None}

    def prewarm_finished(self, error: Exception | None = None) -> None:
        self.prewarm = ({"state": "failed", "error": f"{type(error).__name__}: {error}"}
                        if error else {"state": "ready", "error": None})

    # Individual checks --------------------------------------------------------
    def _check_models(self) -> dict:
        if _pipeline.models_loaded():
            return {"status": OK}
        if self.prewarm["state"] == "failed":
            return {"status": ERROR, "detail": f"Model loading failed: {self.prewarm['error']}"}
        return {"status": STARTING, "detail": "Search models are still loading."}

    async def _check_selftest(self) -> dict:
        """End-to-end search over a tiny in-memory corpus (BM25 + embeddings + reranker)."""
        if not _pipeline.models_loaded():
            return {"status": STARTING, "detail": "Waiting for models."}
        async with self._selftest_lock:
            now = time.monotonic()
            if self._selftest and now - self._selftest[0] < SELFTEST_TTL:
                return {**self._selftest[1], "cached": True}

            def run():
                chunks = [_pipeline.Chunk(title=t, url=f"https://selftest.invalid/{t}",
                                          date="", text=x) for t, x in _SELFTEST_CHUNKS]
                return _pipeline.search("SaaS pricing tiers", chunks, top_k=1, cache_key=None)

            t0 = time.monotonic()
            try:
                hits = await asyncio.wait_for(asyncio.to_thread(run), SELFTEST_TIMEOUT)
                ms = round((time.monotonic() - t0) * 1000)
                if hits and hits[0]["title"] == "selftest-match":
                    result = {"status": OK, "latency_ms": ms}
                else:
                    result = {"status": ERROR, "latency_ms": ms,
                              "detail": "Search returned an unexpected top result."}
            except asyncio.TimeoutError:
                result = {"status": ERROR, "detail": f"Search self-test exceeded {SELFTEST_TIMEOUT:.0f}s."}
            except Exception as exc:
                log.exception("Search self-test failed")
                result = {"status": ERROR, "detail": f"{type(exc).__name__}: {exc}"}
            self._selftest = (time.monotonic(), result)
            return {**result, "cached": False}

    async def _check_tools(self) -> tuple[dict, list[str]]:
        try:
            names = await self._list_tools()
        except Exception as exc:
            return {"status": ERROR, "detail": f"{type(exc).__name__}: {exc}"}, []
        if "search_newsletter" not in names:
            return {"status": ERROR, "detail": "search_newsletter tool is not registered."}, names
        return {"status": OK, "count": len(names)}, names

    async def _check_history_db(self) -> dict:
        if not self._log_queries:
            return {"status": DISABLED}
        try:
            await asyncio.to_thread(self._db_check)
            return {"status": OK}
        except Exception as exc:
            return {"status": ERROR, "detail": f"History DB not writable: {type(exc).__name__}: {exc}"}

    def _check_memory(self) -> dict:
        rss, limit = _rss_mb(), _memory_limit_mb()
        out: dict = {"status": OK, "rss_mb": round(rss, 1) if rss is not None else None,
                     "limit_mb": round(limit, 1) if limit else None}
        warn_pct = float(os.getenv("CITED_MEMORY_WARN_PCT", "90"))
        warn_mb  = os.getenv("CITED_MEMORY_WARN_MB")
        if rss is not None and limit:
            pct = rss / limit * 100
            out["used_pct"] = round(pct, 1)
            if pct >= warn_pct:
                out.update(status=WARN, detail=f"Using {pct:.0f}% of the memory limit.")
        if rss is not None and warn_mb and rss >= float(warn_mb):
            out.update(status=WARN, detail=f"RSS {rss:.0f} MB is above CITED_MEMORY_WARN_MB={warn_mb}.")
        return out

    def _check_disk(self) -> dict:
        if not self._log_queries:
            return {"status": DISABLED}
        try:
            folder = self._db_path().parent
            free = shutil.disk_usage(folder).free / (1024 * 1024)
        except Exception as exc:
            return {"status": WARN, "detail": f"Could not read disk usage: {exc}"}
        warn = float(os.getenv("CITED_DISK_WARN_MB", "200"))
        out = {"status": OK, "free_mb": round(free)}
        if free < warn:
            out.update(status=WARN, detail=f"Only {free:.0f} MB free for the history DB.")
        return out

    def _check_db_size(self) -> dict:
        """ThinkWithMe-style alert: database file over 100 MB."""
        if not self._log_queries:
            return {"status": DISABLED}
        try:
            size = self._db_path().stat().st_size / (1024 * 1024)
        except FileNotFoundError:
            return {"status": OK, "size_mb": 0}
        except OSError as exc:
            return {"status": WARN, "detail": f"Could not read DB size: {exc}"}
        limit = float(os.getenv("CITED_DB_SIZE_WARN_MB", "100"))
        out = {"status": OK, "size_mb": round(size, 1), "limit_mb": limit}
        if size > limit:
            out.update(status=WARN, detail=f"Database is {size:.0f} MB (over {limit:.0f} MB).")
        return out

    def _check_search_quality(self) -> dict:
        """ThinkWithMe-style alert: >50% of the last 20 searches had Limited coverage."""
        levels = self._recent_levels() if self._recent_levels else []
        if len(levels) < 5:
            return {"status": OK, "recent_searches": len(levels),
                    "detail": "Not enough recent searches to judge (needs 5)."}
        low = sum(1 for lv in levels if lv == "low")
        ratio = low / len(levels)
        out = {"status": OK, "recent_searches": len(levels), "limited_coverage": low,
               "limited_pct": round(ratio * 100)}
        if ratio > self._low_conf_alert:
            out.update(status=WARN, detail=f"{ratio:.0%} of the last {len(levels)} searches had "
                                           "Limited coverage.")
        return out

    def _check_library(self) -> dict:
        st = self._library_status() if self._library_status else {"enabled": False}
        if not st.get("enabled"):
            return {"status": DISABLED}
        srcs = st["sources"]
        ready = sum(1 for s in srcs if s["state"] in ("ready", "refreshing", "stale"))
        failing = [s["name"] for s in srcs if s["state"] in ("error", "stale")]
        out = {"status": OK, "sources": len(srcs), "ready": ready,
               "posts": sum(s["posts"] for s in srcs), "needs_url": len(st.get("pending", []))}
        if failing:
            out.update(status=WARN, detail="Refresh failing for: " + ", ".join(failing))
        return out

    def _check_recent_errors(self) -> dict:
        n = self._recent_errors() if self._recent_errors else None
        if n is None:
            return {"status": DISABLED}
        out = {"status": OK, "errors_last_hour": n}
        if n >= self._error_alert:
            out.update(status=WARN, detail=f"{n} errors in the last hour.")
        return out

    async def _check_event_loop(self) -> dict:
        t0 = time.monotonic()
        await asyncio.sleep(0)
        lag = time.monotonic() - t0
        out = {"status": OK, "lag_ms": round(lag * 1000, 1)}
        if lag > LOOP_LAG_WARN:
            out.update(status=WARN, detail="Event loop is slow to respond.")
        return out

    # Aggregation ---------------------------------------------------------------
    async def live(self) -> tuple[int, dict]:
        return 200, {"status": OK, "uptime_seconds": round(time.time() - self.started)}

    def _note_status(self, status: str, checks: dict) -> None:
        if status != self.last_status:
            previous, self.last_status = self.last_status, status
            if self._on_change and not (previous is None and status == "ok"):
                try:
                    self._on_change(previous, status, checks)
                except Exception:
                    log.exception("Health status-change hook failed")

    async def ready(self) -> tuple[int, dict]:
        checks, _ = await self._core_checks()
        status = _overall(checks)
        self._note_status(status, checks)
        return _http(status), {"status": status,
                               "checks": {k: v["status"] for k, v in checks.items()}}

    async def report(self, warmed_newsletters: int = 0) -> tuple[int, dict]:
        checks, tools = await self._core_checks()
        checks["memory"]     = self._check_memory()
        checks["disk"]       = self._check_disk()
        checks["event_loop"] = await self._check_event_loop()
        checks["db_size"]        = self._check_db_size()
        checks["search_quality"] = self._check_search_quality()
        try:
            checks["library"] = await asyncio.to_thread(self._check_library)
        except Exception as exc:
            checks["library"] = {"status": WARN, "detail": f"Could not read library status: {exc}"}
        try:
            checks["recent_errors"] = await asyncio.to_thread(self._check_recent_errors)
        except Exception as exc:
            checks["recent_errors"] = {"status": WARN, "detail": f"Could not read error log: {exc}"}
        status = _overall(checks)
        self._note_status(status, checks)
        _explain(checks)
        return _http(status), {
            "name": "Cited",
            "status": status,
            "summary": SUMMARY[status],
            "version": VERSION,
            "commit": COMMIT,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "started_at": datetime.fromtimestamp(self.started, timezone.utc).isoformat(timespec="seconds"),
            "uptime_seconds": round(time.time() - self.started),
            "mcp_endpoint": "/mcp",
            "tools": tools,
            "checks": checks,
            "prewarm": self.prewarm,
            "warmed_newsletters": warmed_newsletters,
            "rate_limit_per_minute": self._rate_limit,
        }

    async def _core_checks(self) -> tuple[dict, list[str]]:
        tools_check, tools = await self._check_tools()
        checks = {
            "models":         self._check_models(),
            "search_selftest": await self._check_selftest(),
            "mcp_tools":      tools_check,
            "history_db":     await self._check_history_db(),
        }
        return checks, tools


def _overall(checks: dict) -> str:
    states = {c["status"] for c in checks.values()}
    if states & {ERROR, STARTING}:
        return "unavailable"
    if WARN in states:
        return "degraded"
    return "ok"


def _http(status: str) -> int:
    return 503 if status == "unavailable" else 200
