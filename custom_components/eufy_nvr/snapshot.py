"""Bounded on-demand snapshots; no background polling or persistent images."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic


class SnapshotCache:
    """Coalesce concurrent requests, cache one size, and back off after failure.

    Cancellation is deliberately propagated. A failed refresh never disguises an
    old image as a current frame. Keeping only one size bounds memory per camera.
    """

    def __init__(self, ttl: float = 30, timeout: float = 9, cooldown: float = 10,
                 clock: Callable[[], float] = monotonic) -> None:
        self.ttl = ttl
        self.timeout = timeout
        self.cooldown = cooldown
        self._clock = clock
        self._lock = asyncio.Lock()
        self._image: bytes | None = None
        self._size: tuple[int | None, int | None] | None = None
        self._expires = 0.0
        self._retry_after = 0.0

    async def async_get(self, size: tuple[int | None, int | None],
                        fetch: Callable[[], Awaitable[bytes | None]]) -> bytes | None:
        """Fetch only for a viewer, with at most one extraction per camera."""
        async with self._lock:
            now = self._clock()
            if self._image is not None and size == self._size and now < self._expires:
                return self._image
            if now < self._retry_after:
                return None
            self._image = None
            try:
                async with asyncio.timeout(self.timeout):
                    image = await fetch()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Extractor errors may contain a private RTSP URL. Do not log them.
                self._retry_after = self._clock() + self.cooldown
                return None
            if not image:
                self._retry_after = self._clock() + self.cooldown
                return None
            self._image = image
            self._size = size
            self._expires = self._clock() + self.ttl
            self._retry_after = 0.0
            return image

    def clear(self) -> None:
        """Forget private image bytes when a camera is unloaded or unavailable."""
        self._image = None
        self._size = None
        self._expires = self._retry_after = 0.0
