"""
Per-client sliding-window rate limiting for the hosted (HTTP) server.

Configuration (env vars):
  CITED_RATE_LIMIT_PER_MIN        searches per minute per client IP (default 15; 0 disables)
  CITED_TRUSTED_PROXY_HOPS        how many proxies in front of the app append to
                                  X-Forwarded-For (default 1 — Render/Railway's edge).
                                  The client IP is taken from that position counting
                                  from the right, so a client can't spoof it by sending
                                  its own X-Forwarded-For. Set 0 to ignore the header.
  CITED_RATE_LIMIT_SHARED_CIDRS   comma-separated CIDRs of shared egress IPs (e.g. a
                                  platform that calls on behalf of many users) that get
                                  a separate, higher limit instead of the per-IP one
  CITED_RATE_LIMIT_SHARED_PER_MIN limit for those shared IPs (default 300)
"""
from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from collections import deque

WINDOW_SECONDS = 60.0
_MAX_TRACKED = 100_000  # hard cap on tracked clients (memory safety)


def _parse_cidrs(raw: str) -> list:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


class RateLimiter:
    """Sliding-window log limiter: at most `limit` events per `window` seconds per key."""

    def __init__(self, limit: int, window: float = WINDOW_SECONDS, clock=time.monotonic):
        self.limit  = limit
        self.window = window
        self._clock = clock
        self._events: dict[str, deque] = {}
        self._lock  = threading.Lock()
        self._last_sweep = clock()

    def hit(self, key: str) -> tuple[bool, int, int]:
        """Record an attempt. Returns (allowed, remaining, retry_after_seconds)."""
        if self.limit <= 0:
            return True, -1, 0
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            q = self._events.get(key)
            if q is None:
                if len(self._events) >= _MAX_TRACKED:
                    self._sweep(now)
                q = self._events[key] = deque()
            cutoff = now - self.window
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                retry = max(1, math.ceil(q[0] + self.window - now))
                return False, 0, retry
            q.append(now)
            return True, self.limit - len(q), 0

    def tracked(self) -> int:
        with self._lock:
            return len(self._events)

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep >= self.window:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        for k in [k for k, q in self._events.items() if not q or q[-1] <= cutoff]:
            del self._events[k]
        self._last_sweep = now


class ClientRateLimit:
    """Picks the right limiter for a request's client IP."""

    def __init__(self, per_min: int, shared_cidrs: str = "", shared_per_min: int = 300,
                 trusted_hops: int = 1, clock=time.monotonic):
        self.per_min        = per_min
        self.shared_per_min = shared_per_min
        self.trusted_hops   = max(0, trusted_hops)
        self.shared_nets    = _parse_cidrs(shared_cidrs)
        self.per_ip  = RateLimiter(per_min, clock=clock)
        self.shared  = RateLimiter(shared_per_min, clock=clock)
        self._clock  = clock
        self._blocked: dict[str, float] = {}   # ip → monotonic time the block ends

    # Temporary blocks (set by the auto-fix agent; memory only, never persisted)
    def block(self, ip: str, seconds: float) -> None:
        self._blocked[ip] = self._clock() + seconds

    def unblock(self, ip: str) -> None:
        self._blocked.pop(ip, None)

    def blocked_for(self, ip: str) -> int:
        """Seconds left on a block for this IP (0 if not blocked)."""
        until = self._blocked.get(ip)
        if until is None:
            return 0
        left = until - self._clock()
        if left <= 0:
            del self._blocked[ip]
            return 0
        return max(1, math.ceil(left))

    def blocked(self) -> dict[str, int]:
        return {ip: s for ip in list(self._blocked) if (s := self.blocked_for(ip))}

    @classmethod
    def from_env(cls) -> "ClientRateLimit":
        return cls(
            per_min=int(os.getenv("CITED_RATE_LIMIT_PER_MIN", "15")),
            shared_cidrs=os.getenv("CITED_RATE_LIMIT_SHARED_CIDRS", ""),
            shared_per_min=int(os.getenv("CITED_RATE_LIMIT_SHARED_PER_MIN", "300")),
            trusted_hops=int(os.getenv("CITED_TRUSTED_PROXY_HOPS", "1")),
        )

    def client_ip(self, peer: str | None, forwarded_for: str | None) -> str:
        """Client IP, honoring X-Forwarded-For only as far as trusted proxies go."""
        if self.trusted_hops and forwarded_for:
            parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
            if parts:
                candidate = parts[-self.trusted_hops] if len(parts) >= self.trusted_hops else parts[0]
                try:
                    return str(ipaddress.ip_address(candidate))
                except ValueError:
                    pass
        return peer or "unknown"

    def _is_shared(self, ip: str) -> bool:
        if not self.shared_nets:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.shared_nets)

    def check(self, ip: str) -> tuple[bool, int, int, int]:
        """Returns (allowed, limit, remaining, retry_after_seconds)."""
        if self._is_shared(ip):
            ok, rem, retry = self.shared.hit(ip)
            return ok, self.shared_per_min, rem, retry
        ok, rem, retry = self.per_ip.hit(ip)
        return ok, self.per_min, rem, retry
