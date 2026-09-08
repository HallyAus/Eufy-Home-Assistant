"""Async snapshot helper tests; no Home Assistant runtime or hardware required."""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location("hardening_snapshot", Path(__file__).resolve().parents[1] / "custom_components/eufy_nvr/snapshot.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
SnapshotCache = module.SnapshotCache


@pytest.mark.asyncio
async def test_concurrent_requests_capture_once():
    capture = AsyncMock(return_value=b"image")
    cache = SnapshotCache()
    results = await asyncio.gather(*(cache.async_get((640, 480), capture) for _ in range(20)))
    assert results == [b"image"] * 20
    assert capture.await_count == 1


@pytest.mark.asyncio
async def test_different_dimensions_never_capture_in_parallel():
    active, peak = 0, 0
    async def capture():
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        return b"image"
    cache = SnapshotCache()
    await asyncio.gather(*(cache.async_get(key, capture) for key in range(10)))
    assert peak == 1
    assert len(cache._cache) == 4


@pytest.mark.asyncio
async def test_expiry_returns_stale_while_refreshing():
    now = [0.0]
    capture = AsyncMock(side_effect=[b"first", b"second"])
    cache = SnapshotCache(clock=lambda: now[0])
    assert await cache.async_get(1, capture) == b"first"
    now[0] = 3.0
    assert await cache.async_get(1, capture) == b"first"
    await asyncio.gather(*cache._refresh_tasks.values())
    assert await cache.async_get(1, capture) == b"second"


@pytest.mark.asyncio
async def test_concurrent_stale_requests_start_one_background_refresh():
    now = [0.0]
    release = asyncio.Event()
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        if calls == 2:
            await release.wait()
        return f"image-{calls}".encode()

    cache = SnapshotCache(clock=lambda: now[0])
    assert await cache.async_get(1, capture) == b"image-1"
    now[0] = 4.0
    results = await asyncio.gather(*(cache.async_get(1, capture) for _ in range(20)))
    assert results == [b"image-1"] * 20
    assert calls == 2
    assert len(cache._refresh_tasks) == 1
    release.set()
    await asyncio.gather(*cache._refresh_tasks.values())
    assert await cache.async_get(1, capture) == b"image-2"


@pytest.mark.asyncio
async def test_none_has_only_short_failure_cooldown():
    now = [0.0]
    capture = AsyncMock(side_effect=[None, b"recovered"])
    cache = SnapshotCache(clock=lambda: now[0])
    assert await cache.async_get(1, capture) is None
    assert await cache.async_get(1, capture) is None
    now[0] = 0.5
    assert await cache.async_get(1, capture) == b"recovered"
    assert capture.await_count == 2


@pytest.mark.asyncio
async def test_exception_returns_bounded_stale_image():
    now = [0.0]
    capture = AsyncMock(side_effect=[b"old", OSError("unavailable"), b"new"])
    cache = SnapshotCache(clock=lambda: now[0])
    assert await cache.async_get(1, capture) == b"old"
    now[0] = 4.0
    assert await cache.async_get(1, capture) == b"old"
    await asyncio.gather(*cache._refresh_tasks.values())
    assert await cache.async_get(1, capture) == b"old"
    now[0] = 5.0
    assert await cache.async_get(1, capture) == b"old"
    await asyncio.gather(*cache._refresh_tasks.values())
    assert await cache.async_get(1, capture) == b"new"


@pytest.mark.asyncio
async def test_timeout_cancels_capture_and_releases_lock():
    cancelled = asyncio.Event()
    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    cache = SnapshotCache(timeout=0.02)
    with pytest.raises(TimeoutError):
        await cache.async_get(1, blocked)
    assert cancelled.is_set()
    assert not cache._lock.locked()
    assert await cache.async_get(2, AsyncMock(return_value=b"ok")) == b"ok"


@pytest.mark.asyncio
async def test_external_cancellation_propagates():
    started = asyncio.Event()
    async def blocked():
        started.set()
        await asyncio.Event().wait()
    cache = SnapshotCache()
    task = asyncio.create_task(cache.async_get(1, blocked))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cache._lock.locked()
    assert not cache._cache


@pytest.mark.asyncio
async def test_clear_removes_cached_images():
    cache = SnapshotCache()
    capture = AsyncMock(side_effect=[b"old", b"fresh"])
    assert await cache.async_get(1, capture) == b"old"
    cache.clear()
    assert await cache.async_get(1, capture) == b"fresh"


@pytest.mark.asyncio
async def test_discard_removes_only_one_camera():
    cache = SnapshotCache()
    first = AsyncMock(return_value=b"first")
    second = AsyncMock(return_value=b"second")
    assert await cache.async_get("front", first) == b"first"
    assert await cache.async_get("shed", second) == b"second"
    cache.discard("front")
    assert "front" not in cache._cache
    assert "shed" in cache._cache


@pytest.mark.parametrize("options", [{"ttl": 0}, {"timeout": 0}, {"max_entries": 0}, {"failure_ttl": -1}, {"ttl": 5, "stale_ttl": 4}])
def test_invalid_cache_limits(options):
    with pytest.raises(ValueError):
        SnapshotCache(**options)
