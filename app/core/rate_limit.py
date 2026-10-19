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

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when explicitly deployed behind a trusted proxy
    # (e.g. the Next.js rewrite or a load balancer); otherwise it is spoofable.
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"
