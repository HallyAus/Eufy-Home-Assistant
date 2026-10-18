"""Offline tests for per-camera startStream sensor selection."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("stream_sensors", ROOT / "bridge/stream_sensors.py")
sensors = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sensors)

MANIFEST = {
    "nvr_sn": "TEST_NVR",
    "cameras": [
        {"channel": 0, "name": "Patio", "sn": "T8P00TESTCAM0000", "status": 1},
        {"channel": 1, "name": "Side", "sn": "t8p00testcam0001", "status": 1},
        {"channel": 2, "name": "Lens", "sn": "T8E00TESTCAM0002", "status": 1},
        {"channel": 4, "name": "Doorbell", "sn": "T8214TESTCAM0004", "status": 1},
    ],
}


def test_blank_override_means_no_overrides():
    assert sensors.parse_stream_sensors("") == {}
    assert sensors.parse_stream_sensors("   ") == {}
    assert sensors.parse_stream_sensors(None) == {}


def test_overrides_are_parsed():
    assert sensors.parse_stream_sensors("0:0, 2:0,4:1") == {0: 0, 2: 0, 4: 1}


@pytest.mark.parametrize(
    "value", ["0", "0:", ":0", "0:2", "-1:0", "256:0", "a:0", "0:a", "0:0,,1:0", "0;0", "٠:0"]
)
def test_invalid_overrides_are_rejected(value):
    with pytest.raises(ValueError):
        sensors.parse_stream_sensors(value)


def test_e40_defaults_to_sensor_zero_and_others_keep_sensor_one():
    serials = sensors.serials_by_channel(MANIFEST)
    resolved = sensors.sensors_for_channels([0, 1, 2, 4], serials, {})
    assert resolved == {0: 0, 1: 0, 2: 1, 4: 1}


def test_unknown_channel_keeps_historical_default():
    assert sensors.sensor_for_channel(7, sensors.serials_by_channel(MANIFEST), {}) == 1


def test_override_wins_over_model_default():
    serials = sensors.serials_by_channel(MANIFEST)
    resolved = sensors.sensors_for_channels([0, 2], serials, {0: 1, 2: 0})
    assert resolved == {0: 1, 2: 0}


def test_malformed_manifest_rows_are_ignored():
    manifest = {"cameras": [None, {"channel": "0", "sn": "T8P00X"}, {"channel": True, "sn": "T8P00X"},
                            {"channel": 3, "sn": 42}, {"channel": 5, "sn": "T8P00X"}]}
    assert sensors.serials_by_channel(manifest) == {5: "T8P00X"}
    assert sensors.serials_by_channel([]) == {}
    assert sensors.serials_by_channel({"cameras": "nope"}) == {}


def test_missing_or_invalid_manifest_falls_back_to_defaults(tmp_path):
    assert sensors.load_serials(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert sensors.load_serials(bad) == {}
    good = tmp_path / "cameras.json"
    good.write_text(json.dumps(MANIFEST))
    assert sensors.load_serials(good)[0] == "T8P00TESTCAM0000"


def test_startstream_uses_resolved_sensors_not_a_hardcoded_value():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    assert '"sensor": 1}' not in stream
    assert "import stream_sensors" in stream
    assert 'os.environ.get("EUFY_STREAM_SENSORS", "")' in stream
    assert "build_startstream(USER_ID, CHANNELS, stream_id=1, sensors=STREAM_SENSORS)" in stream


def test_addon_exposes_and_validates_stream_sensors():
    config = yaml.safe_load((ROOT / "eufy_nvr/config.yaml").read_text())
    assert config["options"]["stream_sensors"] == ""
    assert config["schema"]["stream_sensors"] == "str?"
    run_script = (ROOT / "eufy_nvr/run.sh").read_text()
    assert "export EUFY_STREAM_SENSORS=\"$(bashio::config 'stream_sensors' '')\"" in run_script
    assert "stream_sensors.parse_stream_sensors" in run_script
