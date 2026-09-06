"""Pure runtime guards and orderly cleanup for the streaming worker.

No cloud requests or camera protocol commands are made by this module.
"""
from __future__ import annotations

import asyncio
import contextlib
import struct
from time import monotonic

MAX_PIPE_BUFFER = 8 * 1024 * 1024


def command_error_status(frame: bytes) -> int | None:
    """Read only the known fixed-size binary command acknowledgement shape.

    Shared-account -104 failures were reported in issue #8. Do not mistake
    arbitrary JSON or a media payload for an authorisation response.
    """
    if (len(frame) != 148 or frame[:4] != b"XZYH" or frame[14] != 1
            or struct.unpack_from("<H", frame, 4)[0] != 1350
            or struct.unpack_from("<I", frame, 6)[0] != 132
            or any(frame[20:])):
        return None
    return struct.unpack_from("<i", frame, 16)[0]


class MediaWatchdog:
    """Detect a producer which is alive but no longer supplying video."""
    def __init__(self, first_frame_timeout=45, idle_timeout=30, clock=monotonic):
        self.first_frame_timeout = first_frame_timeout
        self.idle_timeout = idle_timeout
        self._clock = clock
        self.started = clock()
        self.last_frame = None

    def received_frame(self):
        self.last_frame = self._clock()

    def failure(self) -> str | None:
        now = self._clock()
        if self.last_frame is None:
            return "first_frame_timeout" if now - self.started >= self.first_frame_timeout else None
        return "video_stalled" if now - self.last_frame >= self.idle_timeout else None


class SessionResources:
    """Own every child/task/file so failures and SIGTERM release the NVR session."""
    def __init__(self):
        self.processes = []
        self.tasks = []
        self.files = []
        self.peer = None

    async def __aenter__(self):
        return self

    def create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def __aexit__(self, exc_type, exc, traceback):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.peer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.peer.close(), timeout=5)
        for process in reversed(self.processes):
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            else:
                await process.wait()
        for handle in self.files:
            with contextlib.suppress(Exception):
                handle.close()
        return False
