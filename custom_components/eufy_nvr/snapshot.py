"""Bounded snapshot caching with coalesced stale-while-revalidate refreshes.

Concurrent requests for a camera share cached results. Each camera serialises
its captures rather than starting multiple producers at once. Fresh images use
a short TTL. Once seeded, bounded stale images return immediately while one
background refresh runs, so a dashboard never queues four thumbnails behind the
NVR's single hardware live-session limit.
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
        self._refresh_tasks: dict[Hashable, asyncio.Task[None]] = {}

    def clear(self) -> None:
        """Discard cached images, for example when the camera is unavailable."""
        self._cache.clear()
        for task in self._refresh_tasks.values():
            task.cancel()
        self._refresh_tasks.clear()

    def discard(self, key: Hashable) -> None:
        """Discard one cache entry without affecting the other cameras."""
        self._cache.pop(key, None)
        if task := self._refresh_tasks.pop(key, None):
            task.cancel()

    async def async_get(
        self, key: Hashable, capture: Callable[[], Awaitable[bytes | None]]
    ) -> bytes | None:
        """Return a recent image or capture one within the request's deadline.

        The deadline covers waiting for the lock as well as the first image
        capture. Cancellation propagates for an unseeded capture. Once a stale
        image exists it returns immediately and a single coalesced background
        refresh updates it; failures retain the stale image with a brief retry
        cooldown.
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
                    fresh_until, stale_until, image = self._cache[key]
                    if fresh_until > now:
                        self._cache.move_to_end(key)
                        return image
                    if image and stale_until > now:
                        self._cache.move_to_end(key)
                        self._start_refresh(key, capture)
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

    def _start_refresh(
        self, key: Hashable, capture: Callable[[], Awaitable[bytes | None]]
    ) -> None:
        """Start at most one non-blocking refresh for a stale camera image."""
        if key in self._refresh_tasks:
            return
        task = asyncio.create_task(self._async_refresh(key, capture))
        self._refresh_tasks[key] = task
        task.add_done_callback(
            lambda done, cache_key=key: self._refresh_done(cache_key, done)
        )

    def _refresh_done(self, key: Hashable, task: asyncio.Task[None]) -> None:
        """Forget a completed refresh without removing a newer task for the key."""
        if self._refresh_tasks.get(key) is task:
            self._refresh_tasks.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _async_refresh(
        self, key: Hashable, capture: Callable[[], Awaitable[bytes | None]]
    ) -> None:
        """Refresh one stale image serially; errors are represented by cooldown."""
        try:
            async with asyncio.timeout(self._timeout):
                async with self._lock:
                    now = self._clock()
                    stale = self._cache.get(key)
                    if stale is None or stale[1] <= now:
                        return
                    if stale[0] > now:
                        return
                    try:
                        image = await capture()
                    except Exception:
                        self._cache[key] = (
                            now + self._failure_ttl, stale[1], stale[2]
                        )
                        self._cache.move_to_end(key)
                        return
                    if image is None:
                        self._cache[key] = (
                            now + self._failure_ttl, stale[1], stale[2]
                        )
                        self._cache.move_to_end(key)
                        return
                    self._remember(key, image)
        except TimeoutError:
            # A foreground call already received the stale image. Leave it in
            # place and allow another bounded refresh after the cooldown.
            async with self._lock:
                now = self._clock()
                stale = self._cache.get(key)
                if stale is not None and stale[1] > now:
                    self._cache[key] = (
                        now + self._failure_ttl, stale[1], stale[2]
                    )
                    self._cache.move_to_end(key)

    def _remember(self, key: Hashable, image: bytes | None) -> None:
        ttl = self._ttl if image else self._failure_ttl
        now = self._clock()
        stale_ttl = self._stale_ttl if image else ttl
        self._cache[key] = (now + ttl, now + stale_ttl, image)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
