import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_oracle_classifies_minus_104_and_stops_parent():
    source = (ROOT / "bridge/sctp_oracle.js").read_text()
    assert "fixedControlStatus" in source
    assert "EUFY_AUTHORIZATION_ERROR_-104" in source
    assert 'process.kill(process.ppid, "SIGTERM")' in source


def test_addon_classifies_authorization_and_supervised_signaling_failures():
    source = (ROOT / "eufy_nvr/run.sh").read_text()
    supervisor = (ROOT / "bridge/eufy_run.py").read_text()
    assert "EUFY_AUTHORIZATION_ERROR_-104" in source
    assert "Shared/member accounts" in source
    assert "signaling timed out" in source
    assert "supervised signaling retries" in source
    assert "if discovery and rc == 0" not in supervisor
    assert 'rc == 0 and reason == "exit"' in supervisor
    assert "attempt * 5" in source


def test_go2rtc_generation_is_restricted_and_tolerates_cold_start():
    source = (ROOT / "bridge/gen_go2rtc.py").read_text()
    assert "starttimeout=" in source
    assert "killsignal=2#killtimeout=5" in source
    assert "modules: [api, rtsp, webrtc, exec, mjpeg, mpegts]" in source
    assert "allow_paths: [python]" in source
    assert "/api/streams" in source
    assert "/api/frame.jpeg" in source
    assert "/api/stream.ts" in source
    assert "eufy_run.py" in source


def test_live_sessions_are_closed_before_process_teardown():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    supervisor = (ROOT / "bridge/eufy_run.py").read_text()

    assert "build_cmd(USER_ID, 1004, {})" in stream
    assert "-> closeLive (1004)" in stream
    assert "await asyncio.wait_for(close_ack.wait(), timeout=1.0)" in stream
    assert "<- closeLive acknowledged" in stream
    assert "await close_live()" in stream
    assert 'if (\n            DISCOVER\n            or state["close_sent"]' not in stream
    assert "os.kill(proc.pid, signal.SIGINT)" in supervisor
    assert "timeout=3.0" in supervisor
    assert "await asyncio.sleep(SESSION_RELEASE_DELAY)" in supervisor
    assert "if not stop_event.is_set() and SESSION_RELEASE_DELAY" not in supervisor


def test_signaling_messages_match_current_official_web_client():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()

    assert 'hashlib.md5(f"{channel}{user_id}{timestamp}".encode())' in stream
    assert 'msgid = "0" if join else f"{self.auth_token}_{uuid.uuid4()}"' in stream
    assert '"channelId": self.channel' in stream
    assert '"region": ec.signaling_region(REGION)' in stream
    assert 'CALL_TYPE = os.environ.get("EUFY_CALL_TYPE", "call")' in stream
    assert 'await self.action3(self.call_type, {})' in stream
    assert 'inner.get("dataType") in ("call", "scall")' in stream
    assert "if status == 200:" in stream
    assert "await sig.ack()" in stream
    assert "str(random.random())" not in stream


def test_native_and_compact_sdp_modes_are_both_supported():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()

    assert 'self.call_type == "scall"' in stream
    assert 'val.lstrip().startswith("v=0")' in stream
    assert 'offer_mode = "native"' in stream
    assert 'sig.send_sdp(pc.localDescription.sdp' in stream

    run_script = (ROOT / "eufy_nvr/run.sh").read_text()
    config = (ROOT / "eufy_nvr/config.yaml").read_text()
    assert 'EUFY_CALL_TYPE="$(bashio::config \'signaling_mode\' \'call\')"' in run_script
    assert 'signaling_mode: list(call|scall)?' in config


def test_release_versions_and_go2rtc_are_aligned():
    manifest = json.loads((ROOT / "custom_components/eufy_nvr/manifest.json").read_text())
    config = (ROOT / "eufy_nvr/config.yaml").read_text()
    dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()
    addon_version = re.search(r'^version: "([^"]+)"$', config, re.MULTILINE).group(1)
    assert manifest["version"] == addon_version
    assert tuple(map(int, addon_version.split("."))) >= (0, 7, 0)
    assert 'ARG GO2RTC_VERSION="v1.9.14"' in dockerfile
