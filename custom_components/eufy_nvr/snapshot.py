"""Bounded, short-lived snapshot caching without a background worker.

Concurrent requests for a camera share cached results. Each camera serialises
its captures rather than starting multiple producers at once. Fresh images use
a short TTL; bounded stale images keep dashboards responsive during a transient
camera handoff or NVR busy response.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable


class SnapshotCache:
    """Serialise captures and retain a small number of recent in-memory images."""

    def __init__(
        self, *, ttl: float = 3.0, timeout: float = 9.0,
        max_entries: int = 4, failure_ttl: float = 0.5,
        stale_ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            ttl <= 0 or timeout <= 0 or max_entries < 1
            or failure_ttl < 0 or stale_ttl < ttl
        ):
            raise ValueError("invalid snapshot cache limits")
        self._ttl = ttl
        self._timeout = timeout
        self._max_entries = max_entries
        self._failure_ttl = failure_ttl
        self._stale_ttl = stale_ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[
            Hashable, tuple[float, float, bytes | None]
        ] = OrderedDict()

    def clear(self) -> None:
        """Discard cached images, for example when the camera is unavailable."""
        self._cache.clear()

    def discard(self, key: Hashable) -> None:
        """Discard one cache entry without affecting the other cameras."""
        self._cache.pop(key, None)

    async def async_get(
        self, key: Hashable, capture: Callable[[], Awaitable[bytes | None]]
    ) -> bytes | None:
        """Return a recent image or capture one within the request's deadline.

        The deadline covers waiting for the lock as well as image capture.
        Cancellation propagates to the capture coroutine; no detached task is
        created. A failed/empty refresh uses a bounded stale image when one is
        available and a brief cooldown prevents an immediate retry storm.
        """
        async with asyncio.timeout(self._timeout):
            async with self._lock:
                now = self._clock()
                for expired in [
                    k for k, (_, stale_until, _) in self._cache.items()
                    if stale_until <= now
                ]:
                    del self._cache[expired]
                if key in self._cache:
                    fresh_until, _, image = self._cache[key]
                    if fresh_until > now:
                        self._cache.move_to_end(key)
                        return image
                stale = self._cache.get(key)
                try:
                    image = await capture()
                except Exception:
                    if stale is not None and stale[2]:
                        self._cache[key] = (now + self._failure_ttl, stale[1], stale[2])
                        self._cache.move_to_end(key)
                        return stale[2]
                    self._remember(key, None)
                    raise
                if image is None and stale is not None and stale[2]:
                    self._cache[key] = (now + self._failure_ttl, stale[1], stale[2])
                    self._cache.move_to_end(key)
                    return stale[2]
                self._remember(key, image)
                return image

    def _remember(self, key: Hashable, image: bytes | None) -> None:
        ttl = self._ttl if image else self._failure_ttl
        now = self._clock()
        stale_ttl = self._stale_ttl if image else ttl
        self._cache[key] = (now + ttl, now + stale_ttl, image)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
