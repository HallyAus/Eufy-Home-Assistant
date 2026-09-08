#!/usr/bin/env python3
"""Keep only the most recently viewed Eufy stream warm.

The NVR accepts one live WebRTC session at a time. Home Assistant dashboards,
however, commonly request every camera thumbnail concurrently. A permanent
warmer per camera therefore creates a retry storm instead of reducing latency.

This controller watches go2rtc's local API. When a real consumer appears it
adds one lightweight MPEG-TS consumer for that same stream, retaining the producer
for a short lease after the viewer leaves. A request for another camera takes
priority and releases the previous lease immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp


POLL_INTERVAL = 0.20
API_TIMEOUT = 2.0
RESTART_DELAY = 0.75
SNAPSHOT_FORMATS = frozenset({"keyframe", "jpeg", "mjpeg"})


def log(message: str) -> None:
    """Emit a credential-free controller message."""
    print(f"[{time.strftime('%H:%M:%S')}] adaptive-warm: {message}", file=sys.stderr, flush=True)


def external_consumer_counts(
    streams: dict[str, Any], warm_stream: str | None, warmer_running: bool
) -> dict[str, int]:
    """Return consumer counts with our own warmer subtracted."""
    counts: dict[str, int] = {}
    for name, info in streams.items():
        if not isinstance(name, str) or not name.startswith("eufy_"):
            continue
        consumers = info.get("consumers") if isinstance(info, dict) else None
        count = 0
        if isinstance(consumers, list):
            count = sum(
                1
                for consumer in consumers
                if not isinstance(consumer, dict)
                or str(consumer.get("format_name", "")).lower()
                not in SNAPSHOT_FORMATS
            )
        if warmer_running and name == warm_stream and count:
            count -= 1
        counts[name] = count
    return counts


def choose_stream(counts: dict[str, int], current: str | None) -> str | None:
    """Choose an active stream, preferring the current lease on a tie."""
    active = [name for name, count in counts.items() if count > 0]
    if not active:
        return None
    if current in active:
        return current
    return min(active, key=lambda name: (-counts[name], name))


class AdaptiveWarmer:
    """Maintain at most one lightweight local MPEG-TS consumer."""

    def __init__(self, lease_seconds: int) -> None:
        self.lease_seconds = lease_seconds
        self.api_url = f"http://127.0.0.1:{os.environ.get('GO2RTC_API_PORT', '1985')}/api/streams"
        self.stream_url = f"http://127.0.0.1:{os.environ.get('GO2RTC_API_PORT', '1985')}/api/stream.ts"
        self.preempt_path = Path(
            os.environ.get("EUFY_SESSION_PREEMPT", "/data/eufy-preempt")
        )
        self.username = os.environ["GO2RTC_USERNAME"]
        self.password = os.environ["GO2RTC_PASSWORD"]
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.stop_event = asyncio.Event()
        self.warm_stream: str | None = None
        self.warmer: asyncio.Task[None] | None = None
        self.last_external_at = 0.0
        self.last_start_attempt = 0.0
        self.last_api_error_log = 0.0
        try:
            self.preempt_path.unlink(missing_ok=True)
        except OSError:
            pass

    def request_stop(self) -> None:
        self.stop_event.set()

    def warmer_running(self) -> bool:
        return self.warmer is not None and not self.warmer.done()

    def consume_preempt_request(self) -> bool:
        """Consume the session gate's cross-process handoff hint."""
        try:
            self.preempt_path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    async def fetch_streams(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        async with session.get(
            self.api_url, headers=self.headers, timeout=API_TIMEOUT
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if isinstance(payload, dict) and isinstance(payload.get("streams"), dict):
            payload = payload["streams"]
        if not isinstance(payload, dict):
            raise ValueError("go2rtc returned a non-object stream list")
        return payload

    async def _consume(self, session: aiohttp.ClientSession, stream: str) -> None:
        async with session.get(
            self.stream_url,
            params={"src": stream},
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=None, connect=API_TIMEOUT),
        ) as response:
            response.raise_for_status()
            async for _ in response.content.iter_chunked(64 * 1024):
                pass

    async def start_warmer(
        self, session: aiohttp.ClientSession, stream: str
    ) -> None:
        await self.stop_warmer()
        self.last_start_attempt = time.monotonic()
        self.warm_stream = stream
        self.warmer = asyncio.create_task(self._consume(session, stream))
        log(f"holding {stream} for up to {self.lease_seconds}s after its last viewer")

    async def stop_warmer(self) -> None:
        task = self.warmer
        stream = self.warm_stream
        self.warmer = None
        self.warm_stream = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if stream:
            log(f"released {stream}")

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        log("controller started")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self.stop_event.is_set():
                if self.consume_preempt_request():
                    if self.warm_stream is not None:
                        log("new producer requested the NVR session; preempting warm lease")
                        await self.stop_warmer()
                    await self.wait(POLL_INTERVAL)
                    continue
                try:
                    streams = await self.fetch_streams(session)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    now = time.monotonic()
                    if now - self.last_api_error_log >= 30.0:
                        log(f"local API unavailable ({type(error).__name__}); retrying")
                        self.last_api_error_log = now
                    await self.stop_warmer()
                    await self.wait(POLL_INTERVAL)
                    continue

                running = self.warmer_running()
                counts = external_consumer_counts(streams, self.warm_stream, running)
                requested = choose_stream(counts, self.warm_stream)
                now = time.monotonic()

                if requested is not None:
                    self.last_external_at = now
                    if requested != self.warm_stream:
                        await self.start_warmer(session, requested)
                    elif not running and now - self.last_start_attempt >= RESTART_DELAY:
                        await self.start_warmer(session, requested)
                elif self.warm_stream is not None:
                    if not running and now - self.last_start_attempt >= RESTART_DELAY:
                        await self.start_warmer(session, self.warm_stream)
                    elif now - self.last_external_at >= self.lease_seconds:
                        await self.stop_warmer()

                await self.wait(POLL_INTERVAL)
        await self.stop_warmer()

    async def wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def async_main(lease_seconds: int) -> int:
    if not 1 <= lease_seconds <= 600:
        raise ValueError("lease seconds must be between 1 and 600")
    warmer = AdaptiveWarmer(lease_seconds)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, warmer.request_stop)
        except NotImplementedError:
            pass
    await warmer.run()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    return asyncio.run(async_main(args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
