#!/usr/bin/env python3
"""Supervise one eufy_stream.py session.

The reversed protocol engine remains in eufy_stream.py. This wrapper owns its
process tree and adds bounded recovery around the failure modes seen in the field:

* signaling stuck at scall/TURN status 100 with no SDP offer (#6)
* discovery rejected by the NVR with a fixed 132-byte authorization reply (#8)
* a live producer that never produces its first video frame, or later stalls (#5)

All child stderr is relayed unchanged so existing diagnostics remain useful.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "eufy_stream.py"
MAX_ATTEMPTS = max(1, min(int(os.environ.get("EUFY_SESSION_ATTEMPTS", "3")), 6))
SIGNAL_TIMEOUT = max(10, min(int(os.environ.get("EUFY_SIGNAL_TIMEOUT", "25")), 120))
FIRST_FRAME_TIMEOUT = max(15, min(int(os.environ.get("EUFY_FIRST_FRAME_TIMEOUT", "50")), 180))
STALL_TIMEOUT = max(10, min(int(os.environ.get("EUFY_STALL_TIMEOUT", "25")), 180))


@dataclass
class SessionState:
    started_at: float
    last_progress: float
    sdp_seen: bool = False
    datachannel_seen: bool = False
    first_video_at: float | None = None
    last_video_at: float | None = None
    turn_pending: bool = False
    authorization_rejection: bool = False


def inspect_log_line(line: str, state: SessionState, discovery: bool) -> None:
    """Update supervised state from stable engine log markers."""
    now = time.monotonic()
    if "scall/turn status 100" in line:
        state.turn_pending = True
        state.last_progress = now
    elif "scall/turn status 200" in line:
        state.turn_pending = False
        state.last_progress = now
    if "NVR SDP offer received" in line:
        state.sdp_seen = True
        state.last_progress = now
    if "DC open: WebrtcDataChannel" in line:
        state.datachannel_seen = True
        state.last_progress = now
    if "VIDEO #" in line:
        if state.first_video_at is None:
            state.first_video_at = now
        state.last_video_at = now
        state.last_progress = now

    # During --discover the only application command is getDeviceList (9100). A
    # shared/member account returns the issue-#8 shape: 16-byte XZYH header plus a
    # fixed 132-byte non-JSON status payload. Owner accounts return dev_list JSON.
    # eufy_stream currently decodes non-UTF8 bytes with replacement characters, so
    # classify by the structural shape rather than attempting to recover lost bytes.
    if (
        discovery
        and "CTRL cmd=1350" in line
        and "len=148" in line
        and "dev_list" not in line
        and "{" not in line
    ):
        state.authorization_rejection = True


async def _terminate_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass


async def _relay_stderr(
    proc: asyncio.subprocess.Process, state: SessionState, discovery: bool
) -> None:
    assert proc.stderr is not None
    while True:
        raw = await proc.stderr.readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace").rstrip("\n")
        print(line, file=sys.stderr, flush=True)
        inspect_log_line(line, state, discovery)


async def run_attempt(engine_args: list[str], discovery: bool) -> tuple[int, str, SessionState]:
    now = time.monotonic()
    state = SessionState(started_at=now, last_progress=now)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ENGINE),
        *engine_args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=None,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name != "nt"),
    )
    relay = asyncio.create_task(_relay_stderr(proc, state, discovery))
    reason = "exit"
    try:
        while proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if state.authorization_rejection:
                reason = "authorization"
                await _terminate_tree(proc)
                break
            if not state.sdp_seen and now - state.started_at >= SIGNAL_TIMEOUT:
                reason = "signaling_timeout"
                await _terminate_tree(proc)
                break
            if not discovery and state.datachannel_seen and state.first_video_at is None:
                if now - state.last_progress >= FIRST_FRAME_TIMEOUT:
                    reason = "first_frame_timeout"
                    await _terminate_tree(proc)
                    break
            if not discovery and state.last_video_at is not None:
                if now - state.last_video_at >= STALL_TIMEOUT:
                    reason = "stream_stall"
                    await _terminate_tree(proc)
                    break
        rc = await proc.wait()
    finally:
        try:
            await asyncio.wait_for(relay, timeout=2)
        except asyncio.TimeoutError:
            relay.cancel()
    return rc, reason, state


async def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    discovery = "--discover" in args

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = min(2 ** (attempt - 2), 4)
            print(
                f"eufy supervisor: retrying session {attempt}/{MAX_ATTEMPTS} in {delay}s",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(delay)

        rc, reason, state = await run_attempt(args, discovery)

        if reason == "authorization":
            print("EUFY_AUTHORIZATION_ERROR_-104", file=sys.stderr, flush=True)
            print(
                "eufy supervisor: NVR returned the fixed authorization-rejection reply "
                "seen with shared/member accounts; use the eufy account that owns/administers the NVR.",
                file=sys.stderr,
                flush=True,
            )
            return 78

        # Discovery is successful only when the engine itself returns success. The
        # engine already verifies cameras.json was written before returning 0.
        if discovery and rc == 0:
            return 0

        # A live producer normally runs until go2rtc removes the consumer. Preserve
        # a clean engine exit instead of manufacturing retries after intentional stop.
        if not discovery and rc == 0 and reason == "exit":
            return 0

        if reason == "signaling_timeout":
            detail = "TURN remained pending" if state.turn_pending else "no SDP offer arrived"
            print(
                f"eufy supervisor: signaling timed out ({detail}); starting a fresh signaling session",
                file=sys.stderr,
                flush=True,
            )
        elif reason == "first_frame_timeout":
            print(
                "eufy supervisor: data channel opened but no video frame arrived; restarting producer",
                file=sys.stderr,
                flush=True,
            )
        elif reason == "stream_stall":
            print(
                f"eufy supervisor: no video frame for {STALL_TIMEOUT}s; restarting stalled producer",
                file=sys.stderr,
                flush=True,
            )
        elif rc != 0:
            print(
                f"eufy supervisor: engine exited rc={rc}; starting a fresh session",
                file=sys.stderr,
                flush=True,
            )

    print(
        f"eufy supervisor: session failed after {MAX_ATTEMPTS} attempts",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
