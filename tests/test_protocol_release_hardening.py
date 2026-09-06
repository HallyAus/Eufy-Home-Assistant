import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_oracle_classifies_minus_104_and_stops_parent():
    source = (ROOT / "bridge/sctp_oracle.js").read_text()
    assert "fixedControlStatus" in source
    assert "EUFY_AUTHORIZATION_ERROR_-104" in source
    assert 'process.kill(process.ppid, "SIGTERM")' in source


def test_addon_classifies_authorization_and_stalled_signaling():
    source = (ROOT / "eufy_nvr/run.sh").read_text()
    assert "EUFY_AUTHORIZATION_ERROR_-104" in source
    assert "Shared/member accounts" in source
    assert "scall/turn status 100" in source
    assert "NVR SDP offer received" in source
    assert "attempt * 5" in source


def test_go2rtc_generation_is_restricted_and_tolerates_cold_start():
    source = (ROOT / "bridge/gen_go2rtc.py").read_text()
    assert "starttimeout=" in source
    assert "killtimeout=5" in source
    assert "modules: [api, rtsp, webrtc, exec]" in source
    assert "allow_paths: [python]" in source
    assert "/api/streams" in source


def test_release_versions_and_go2rtc_are_aligned():
    manifest = json.loads((ROOT / "custom_components/eufy_nvr/manifest.json").read_text())
    config = (ROOT / "eufy_nvr/config.yaml").read_text()
    dockerfile = (ROOT / "eufy_nvr/Dockerfile").read_text()
    build = yaml.safe_load((ROOT / "eufy_nvr/build.yaml").read_text())
    addon_version = re.search(r'^version: "([^"]+)"$', config, re.MULTILINE).group(1)
    assert manifest["version"] == addon_version == "0.7.0"
    assert 'ARG REPO_REF="v0.7.0"' in dockerfile
    assert build["args"]["GO2RTC_VERSION"] == "v1.9.14"
