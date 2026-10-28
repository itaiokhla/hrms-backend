"""Small sliding-window rate limiter for authentication endpoints.

State is per process. Behind several workers or instances, use a shared
store (e.g. Redis) instead; the interface stays the same.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import Request, status

from app.config import settings
from app.core.errors import AppError


class RateLimiter:
    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        limit = max(1, int(limit * settings.auth_rate_limit_scale))
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                retry = int(window_seconds - (now - hits[0])) + 1
                raise AppError(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many attempts. Please try again later.",
                    "RATE_LIMITED",
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)

    def count(self, key: str, window_seconds: int) -> int:
        """Hits of ``key`` within the window, without recording one (cooldown checks)."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            return len(hits)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when explicitly deployed behind a trusted proxy
    # (e.g. the Next.js rewrite or a load balancer); otherwise it is spoofable.
    # The trusted-hop rule (AS-GEO-07, AS 07 §18 T-28): the entry ``trusted_proxy_hops`` positions from the right is
    # the one our nearest trusted proxy appended; anything to its left is client-controlled.
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            entries = [e.strip() for e in forwarded.split(",") if e.strip()]
            if entries:
                hops = max(1, int(settings.trusted_proxy_hops or 1))
                return entries[max(len(entries) - hops, 0)][:64]
    return request.client.host if request.client else "unknown"
