"""Bounded, short-lived snapshot caching without a background worker.

Concurrent requests for a camera share cached results. Each camera serialises
its captures, including different dimensions, rather than starting multiple
FFmpeg processes at once. Cache entries never survive their explicit TTL.
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0 or timeout <= 0 or max_entries < 1 or failure_ttl < 0:
            raise ValueError("invalid snapshot cache limits")
        self._ttl = ttl
        self._timeout = timeout
        self._max_entries = max_entries
        self._failure_ttl = failure_ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[Hashable, tuple[float, bytes | None]] = OrderedDict()

    def clear(self) -> None:
        """Discard cached images, for example when the camera is unavailable."""
        self._cache.clear()

    async def async_get(
        self, key: Hashable, capture: Callable[[], Awaitable[bytes | None]]
    ) -> bytes | None:
        """Return a recent image or capture one within the request's deadline.

        The deadline covers waiting for the lock as well as image capture.
        Cancellation propagates to the capture coroutine; no detached task is
        created. A failed/empty result gets a brief cooldown, never a stale image.
        """
        async with asyncio.timeout(self._timeout):
            async with self._lock:
                now = self._clock()
                for expired in [k for k, (until, _) in self._cache.items() if until <= now]:
                    del self._cache[expired]
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return self._cache[key][1]
                try:
                    image = await capture()
                except Exception:
                    # The first caller sees the error; queued requests get a
                    # short cooldown instead of immediately restarting FFmpeg.
                    self._remember(key, None)
                    raise
                self._remember(key, image)
                return image

    def _remember(self, key: Hashable, image: bytes | None) -> None:
        ttl = self._ttl if image else self._failure_ttl
        self._cache[key] = (self._clock() + ttl, image)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
