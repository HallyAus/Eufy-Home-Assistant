"""Offline behavioural tests for discovery-state/config generation."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hardening_generator", ROOT / "bridge/gen_go2rtc.py")
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)
AUTH = {"username": "eufy", "password": "0123456789abcdef"}


def camera(channel=0, name="Garage", sn="CAM1", status=1):
    return {"channel": channel, "name": name, "sn": sn, "status": status}


def manifest(*cameras):
    return {"nvr_sn": "TEST_NVR", "cameras": list(cameras)}


def prepare(tmp_path, value):
    source, output, registry = [tmp_path / name for name in ("cameras.json", "go2rtc.yaml", "stream_names.json")]
    source.write_text(json.dumps(value))
    return source, output, registry


def test_import_does_not_read_auth_or_discovery_files():
    assert callable(gen.main)


def test_preserves_legacy_slug_for_unique_camera():
    named, _ = gen.assign_names(gen.validate_manifest(manifest(camera())), {})
    assert named[0][0] == "eufy_garage"


def test_colliding_names_are_distinct_and_order_independent():
    cams = [camera(2, "Front door", "B"), camera(1, "Front-door", "A"), camera(3, "Front_door_ch2", "C")]
    first, _ = gen.assign_names(gen.validate_manifest(manifest(*cams)), {})
    second, _ = gen.assign_names(gen.validate_manifest(manifest(*reversed(cams))), {})
    assert first == second
    assert len({name for name, _ in first}) == 3
    assert len(yaml.safe_load(gen.render_config(first, **AUTH))["streams"]) == 3


def test_rename_and_channel_move_preserve_identity(tmp_path):
    paths = prepare(tmp_path, manifest(camera()))
    first = gen.generate(*paths, **AUTH)
    paths[0].write_text(json.dumps(manifest(camera(4, "New Name", "CAM1"))))
    second = gen.generate(*paths, **AUTH)
    assert first[0][0] == second[0][0] == "eufy_garage"
    config = paths[1].read_text()
    assert "eufy_run.py 4 --rtsp" in config
    assert "#starttimeout=60#killsignal=2#killtimeout=5" in config


def test_removed_camera_name_cannot_be_hijacked(tmp_path):
    paths = prepare(tmp_path, manifest(camera()))
    gen.generate(*paths, **AUTH)
    paths[0].write_text(json.dumps(manifest(camera(0, "Garage", "REPLACEMENT"))))
    named = gen.generate(*paths, **AUTH)
    assert named[0][0] != "eufy_garage"


def test_offline_identity_is_reserved_and_empty_mapping_is_valid(tmp_path):
    paths = prepare(tmp_path, manifest(camera(status="0")))
    first = gen.generate(*paths, **AUTH)
    assert yaml.safe_load(paths[1].read_text())["streams"] == {}
    paths[0].write_text(json.dumps(manifest(camera(name="Renamed", status=1))))
    second = gen.generate(*paths, **AUTH)
    assert first[0][0] == second[0][0]


def test_no_camera_manifest_is_valid_empty_mapping(tmp_path):
    paths = prepare(tmp_path, manifest())
    gen.generate(*paths, **AUTH)
    assert yaml.safe_load(paths[1].read_text())["streams"] == {}


@pytest.mark.parametrize("value", [None, [], {}, {"cameras": {}}, manifest({}), manifest(camera(True)),
    manifest(camera(-1)), manifest(camera(256)), manifest(camera("zero")),
    manifest(camera(name=[])), manifest(camera(sn=[])), manifest(camera(status={})),
    manifest(camera(status=True)), manifest(camera(), camera(0, sn="OTHER")),
    manifest(camera(), camera(1)), {"nvr_sn": [], "cameras": []}])
def test_invalid_manifest_does_not_overwrite_working_config(tmp_path, value):
    paths = prepare(tmp_path, value)
    paths[1].write_text("LAST_KNOWN_GOOD")
    with pytest.raises(ValueError):
        gen.generate(*paths, **AUTH)
    assert paths[1].read_text() == "LAST_KNOWN_GOOD"
    assert not paths[2].exists()


@pytest.mark.parametrize("port", [0, 65536, True, "1984"])
def test_invalid_ports_rejected(port):
    with pytest.raises(ValueError):
        gen.render_config([], **AUTH, api_port=port)


def test_colliding_listeners_rejected():
    with pytest.raises(ValueError):
        gen.render_config([], **AUTH, api_port=8554)


def test_generated_listeners_require_authentication():
    config = yaml.safe_load(gen.render_config([], **AUTH))
    assert config["api"]["username"] == AUTH["username"]
    assert config["api"]["password"] == AUTH["password"]
    assert config["api"]["local_auth"] is False
    assert config["rtsp"]["username"] == AUTH["username"]
    assert config["rtsp"]["password"] == AUTH["password"]


@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "", "password": AUTH["password"]},
        {"username": "eufy", "password": "too-short"},
        {"username": "bad\nname", "password": AUTH["password"]},
    ],
)
def test_invalid_credentials_rejected(credentials):
    with pytest.raises(ValueError):
        gen.render_config([], **credentials)


def test_malformed_registry_is_not_reset(tmp_path):
    paths = prepare(tmp_path, manifest(camera()))
    paths[1].write_text("LAST_KNOWN_GOOD")
    paths[2].write_text('{"version":1,"names":{"x":"bad name"}}')
    with pytest.raises(ValueError):
        gen.generate(*paths, **AUTH)
    assert paths[1].read_text() == "LAST_KNOWN_GOOD"


def test_duplicate_registry_names_are_rejected():
    with pytest.raises(ValueError):
        gen.validate_registry({"version": 1, "names": {"A": "eufy_a", "B": "eufy_a"}})


def test_paths_cannot_alias_source(tmp_path):
    paths = prepare(tmp_path, manifest(camera()))
    with pytest.raises(ValueError):
        gen.generate(paths[0], paths[0], paths[2], **AUTH)
    assert json.loads(paths[0].read_text())["nvr_sn"] == "TEST_NVR"


def test_atomic_replace_failure_preserves_file_and_cleans_temporary(tmp_path):
    target = tmp_path / "go2rtc.yaml"
    target.write_text("LAST_KNOWN_GOOD")
    with patch.object(gen.os, "replace", side_effect=OSError("test failure")):
        with pytest.raises(OSError):
            gen.atomic_write(target, "NEW")
    assert target.read_text() == "LAST_KNOWN_GOOD"
    assert list(tmp_path.iterdir()) == [target]


def test_private_registry_permissions(tmp_path):
    paths = prepare(tmp_path, manifest(camera()))
    gen.generate(*paths, **AUTH)
    if os.name != "nt":
        assert paths[2].stat().st_mode & 0o777 == 0o600


def test_cli_missing_file_exits_cleanly(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "bridge/gen_go2rtc.py"), "127.0.0.1"],
        env={**os.environ, "EUFY_CAMERAS": str(tmp_path / "missing.json"),
             "GO2RTC_USERNAME": AUTH["username"], "GO2RTC_PASSWORD": AUTH["password"]},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "--discover" in result.stderr
