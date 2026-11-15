from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request

from ..errors import Muse2APIError, Unauthorized
from ..services.container import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _check(provided: str, expected: str) -> None:
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise Unauthorized("invalid or missing API key")


class RateLimited(Muse2APIError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"


_buckets: dict[str, tuple[float, float]] = {}
"""key hash -> (tokens, last refill monotonic seconds). Process-local, resets on restart."""


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def require_api_key(request: Request) -> None:
    s = get_services(request).settings
    provided = _bearer(request)
    hashed = key_hash(provided) if provided else ""
    request.state.key_hash = hashed

    # The admin key is a superset of the API key.
    if s.admin_key and provided and hmac.compare_digest(provided.encode(), s.admin_key.encode()):
        _limit(hashed, s.rate_limit_rpm)
        return
    if s.api_key and provided and hmac.compare_digest(provided.encode(), s.api_key.encode()):
        _limit(hashed, s.rate_limit_rpm)
        return
    if hashed and get_services(request).keys is not None and _key_in_store(request, hashed):
        _limit(hashed, s.rate_limit_rpm)
        return
    _check(provided, s.api_key)


def _key_in_store(request: Request, hashed: str) -> bool:
    """Database-backed keys are checked synchronously through a small cache."""
    cache: dict[str, tuple[bool, float]] = request.app.state.key_cache
    hit = cache.get(hashed)
    now = time.monotonic()
    if hit is not None and now - hit[1] < 30.0:
        return hit[0]

    valid = _run_sync(request.app.state.services.keys.exists(hashed))
    if len(cache) >= 512:  # bound the cache: arbitrary bearer values must not grow it forever
        cache.clear()
    cache[hashed] = (valid, now)
    return valid


def _run_sync(coro):
    """Run a coroutine from a sync dependency (keys live in SQLite, a few ms at most)."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _limit(hashed: str, rpm: int) -> None:
    if rpm <= 0 or not hashed:
        return
    capacity = float(rpm)
    now = time.monotonic()
    tokens, last = _buckets.get(hashed, (capacity, now))
    tokens = min(capacity, tokens + (now - last) * (rpm / 60.0))
    if tokens < 1.0:
        _buckets[hashed] = (tokens, now)
        raise RateLimited(f"rate limit exceeded ({rpm} requests/min)")
    _buckets[hashed] = (tokens - 1.0, now)


def require_admin_key(request: Request) -> None:
    _check(_bearer(request), get_services(request).settings.effective_admin_key)


def public_base(request: Request) -> str:
    base = get_services(request).settings.public_base
    return base.rstrip("/") if base else str(request.base_url).rstrip("/")
