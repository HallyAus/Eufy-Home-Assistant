"""Offline regression tests for the Eufy session supervisor."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eufy_run", ROOT / "bridge/eufy_run.py")
supervisor = importlib.util.module_from_spec(SPEC)
# Python 3.13's dataclasses implementation resolves annotations through
# sys.modules while the class decorator runs, matching normal import semantics.
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


def state():
    now = time.monotonic()
    return supervisor.SessionState(started_at=now, last_progress=now)


def test_turn_pending_and_sdp_progress():
    current = state()
    supervisor.inspect_log_line("[12:00:00] scall/turn status 100", current, True)
    assert current.turn_pending is True
    assert current.sdp_seen is False

    supervisor.inspect_log_line("[12:00:01] scall/turn status 200", current, True)
    supervisor.inspect_log_line("[12:00:01] NVR SDP offer received", current, True)
    assert current.turn_pending is False
    assert current.sdp_seen is True


def test_video_progress_tracks_first_and_latest_frame():
    current = state()
    supervisor.inspect_log_line("[12:00:02] DC open: WebrtcDataChannel", current, False)
    assert current.datachannel_seen is True
    supervisor.inspect_log_line("[12:00:03] VIDEO #1 cmd=1300", current, False)
    first = current.first_video_at
    assert first is not None
    assert current.last_video_at == first
    supervisor.inspect_log_line("[12:00:08] VIDEO_PROGRESS frames=125 bytes=1000", current, False)
    assert current.last_video_at is not None
    assert current.last_video_at >= first


def test_busy_status_is_classified_immediately():
    current = state()
    supervisor.inspect_log_line("[12:00:00] scall/turn status 100", current, False)
    supervisor.inspect_log_line("[12:00:01] scall/turn status 486", current, False)
    assert current.turn_pending is False
    assert current.busy_rejection is True
    assert current.sdp_seen is False


def test_explicit_oracle_status_is_classified_as_authorization_failure():
    current = state()
    supervisor.inspect_log_line(
        "[12:00:00] oracle[stderr]: EUFY_AUTHORIZATION_ERROR_-104: owner required",
        current,
        True,
    )
    assert current.authorization_rejection is True


def test_success_status_zero_ack_is_not_authorization_failure():
    current = state()
    supervisor.inspect_log_line(
        "[12:00:00] CTRL cmd=1350 link=1 len=148 ",
        current,
        True,
    )
    assert current.authorization_rejection is False


def test_json_control_reply_is_not_classified_as_authorization_failure():
    current = state()
    supervisor.inspect_log_line(
        '[12:00:00] CTRL cmd=1350 link=1 len=148 {"cmd":9100,"payload":{"dev_list":[]}}',
        current,
        True,
    )
    assert current.authorization_rejection is False


def test_ambiguous_live_reply_is_not_guessed_to_be_authorization_failure():
    current = state()
    supervisor.inspect_log_line(
        "[12:00:00] CTRL cmd=1350 link=1 len=148 ����",
        current,
        False,
    )
    assert current.authorization_rejection is False


def test_supervisor_limits_are_bounded():
    assert 1 <= supervisor.MAX_ATTEMPTS <= 6
    assert 10 <= supervisor.SIGNAL_TIMEOUT <= 120
    assert 15 <= supervisor.FIRST_FRAME_TIMEOUT <= 180
    assert 10 <= supervisor.CONNECTION_TIMEOUT <= 180
    assert 10 <= supervisor.STALL_TIMEOUT <= 180
    assert 0 < supervisor.SESSION_LOCK_POLL <= 1
