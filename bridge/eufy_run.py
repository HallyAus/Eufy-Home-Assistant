#!/usr/bin/env python3
"""Supervise one eufy_stream.py session.

The reversed protocol engine remains in eufy_stream.py. This wrapper owns its
process tree and adds bounded recovery around the failure modes seen in the field:

* signaling stuck at scall/TURN status 100 with no SDP offer (#6)
* an explicit NVR authorization rejection reported by the framing oracle (#8)
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
CONNECTION_TIMEOUT = max(10, min(int(os.environ.get("EUFY_CONNECTION_TIMEOUT", "35")), 180))
STALL_TIMEOUT = max(10, min(int(os.environ.get("EUFY_STALL_TIMEOUT", "25")), 180))
SESSION_LOCK_PATH = Path(
    os.environ.get(
        "EUFY_SESSION_LOCK",
        "/data/eufy-session.lock" if Path("/data").is_dir() else ROOT / "eufy-session.lock",
    )
)
SESSION_LOCK_POLL = 0.10
SESSION_RELEASE_DELAY = max(
    0.0, min(float(os.environ.get("EUFY_SESSION_RELEASE_DELAY", "1.0")), 5.0)
)


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
    busy_rejection: bool = False


def inspect_log_line(line: str, state: SessionState, discovery: bool) -> None:
    """Update supervised state from stable engine log markers."""
    now = time.monotonic()
    if "scall/turn status 100" in line:
        state.turn_pending = True
        state.last_progress = now
    elif "scall/turn status 200" in line:
        state.turn_pending = False
        state.last_progress = now
    elif "scall/turn status 486" in line:
        state.turn_pending = False
        state.busy_rejection = True
        state.last_progress = now
    if "NVR SDP offer received" in line:
        state.sdp_seen = True
        state.last_progress = now
    if "DC open: WebrtcDataChannel" in line:
        state.datachannel_seen = True
        state.last_progress = now
    if "VIDEO #" in line or "VIDEO_PROGRESS" in line:
        if state.first_video_at is None:
            state.first_video_at = now
        state.last_video_at = now
        state.last_progress = now

    # sctp_oracle.js decodes the binary status and emits this exact marker for
    # -104. Never infer authorization from payload length or replacement bytes.
    if "EUFY_AUTHORIZATION_ERROR_-104" in line:
        state.authorization_rejection = True


class SessionGate:
    """Cross-process exclusive gate for the NVR's single live session."""

    def __init__(self, path: Path = SESSION_LOCK_PATH) -> None:
        self.path = path
        self.handle = None

    async def acquire(self, stop_event: asyncio.Event) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            # The production add-on is Linux. Windows standalone runs retain the
            # in-process behavior rather than pretending msvcrt locks are flock.
            return True
        import fcntl

        while not stop_event.is_set():
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=SESSION_LOCK_POLL)
                except asyncio.TimeoutError:
                    pass
        self.release()
        return False

    def release(self) -> None:
        if self.handle is None:
            return
        if os.name != "nt":
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


async def _terminate_tree(proc: asyncio.subprocess.Process) -> None:
    if os.name == "nt" and proc.returncode is not None:
        return
    if os.name != "nt" and proc.returncode is None:
        # Give the protocol engine a chance to send closeLive (cmd 1004).
        # Signal only the engine/session leader first; its oracle and ffmpeg
        # children must remain alive briefly so the framed close reaches the NVR.
        try:
            os.kill(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
    try:
        if os.name != "nt" and proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
        elif os.name == "nt" and proc.returncode is None:
            proc.terminate()
    except ProcessLookupError:
        pass
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    if os.name != "nt":
        # The session leader can exit before ffmpeg/oracle children. Wait for the
        # process group itself, then kill any descendants that ignored SIGTERM.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.1)
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


async def run_attempt(
    engine_args: list[str], discovery: bool,
    stop_event: asyncio.Event | None = None,
) -> tuple[int, str, SessionState]:
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
            if stop_event is not None and stop_event.is_set():
                reason = "shutdown"
                await _terminate_tree(proc)
                break
            if state.authorization_rejection:
                reason = "authorization"
                await _terminate_tree(proc)
                break
            if state.busy_rejection:
                reason = "busy"
                await _terminate_tree(proc)
                break
            if not state.sdp_seen and now - state.started_at >= SIGNAL_TIMEOUT:
                reason = "signaling_timeout"
                await _terminate_tree(proc)
                break
            if state.sdp_seen and not state.datachannel_seen:
                if now - state.last_progress >= CONNECTION_TIMEOUT:
                    reason = "connection_timeout"
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
        # Covers cancellation and a leader that exits before its process-group
        # descendants. Shield cleanup so a second cancellation cannot strand them.
        await asyncio.shield(_terminate_tree(proc))
        try:
            await asyncio.shield(asyncio.wait_for(relay, timeout=2))
        except asyncio.TimeoutError:
            relay.cancel()
    return rc, reason, state


async def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    discovery = "--discover" in args
    stop_event = asyncio.Event()
    gate = SessionGate()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows event loops do not implement add_signal_handler.
            try:
                signal.signal(
                    sig, lambda *_args: loop.call_soon_threadsafe(stop_event.set)
                )
            except ValueError:
                pass

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = min(2 ** (attempt - 2), 4)
            print(
                f"eufy supervisor: retrying session {attempt}/{MAX_ATTEMPTS} in {delay}s",
                file=sys.stderr,
                flush=True,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return 0
            except asyncio.TimeoutError:
                pass

        if not await gate.acquire(stop_event):
            return 0
        try:
            rc, reason, state = await run_attempt(args, discovery, stop_event)
        finally:
            # go2rtc has already removed the last consumer when the engine exits,
            # but the appliance needs a brief beat to retire its WebRTC session.
            # Holding the lock during that beat prevents the next camera from
            # racing the NVR's internal teardown and receiving status 486. This
            # delay is required when go2rtc sets the stop event for a normal
            # camera handoff, not only when the engine exits on its own.
            if SESSION_RELEASE_DELAY:
                await asyncio.sleep(SESSION_RELEASE_DELAY)
            gate.release()

        if reason == "shutdown":
            return 0

        if reason == "authorization":
            print("EUFY_AUTHORIZATION_ERROR_-104", file=sys.stderr, flush=True)
            print(
                "eufy supervisor: NVR returned the fixed authorization-rejection reply "
                "seen with shared/member accounts; use the eufy account that owns/administers the NVR.",
                file=sys.stderr,
                flush=True,
            )
            return 78

        if reason == "busy" and not discovery:
            print(
                "EUFY_NVR_BUSY_486: another camera or the eufy app owns the NVR live session; "
                "failing fast so a waiting viewer can retry without a 25-second dead period.",
                file=sys.stderr,
                flush=True,
            )
            return 75

        if discovery and rc == 0:
            return 0

        if not discovery and rc == 0 and reason == "exit":
            return 0

        if reason == "signaling_timeout":
            detail = "TURN remained pending" if state.turn_pending else "no SDP offer arrived"
            print(
                f"eufy supervisor: signaling timed out ({detail}); starting a fresh signaling session",
                file=sys.stderr,
                flush=True,
            )
        elif reason == "connection_timeout":
            print(
                "eufy supervisor: SDP arrived but the peer/data channel did not connect; restarting producer",
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
        elif reason == "busy":
            print(
                "eufy supervisor: NVR reported signaling status 486 (busy); "
                "releasing the session gate before retry",
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
