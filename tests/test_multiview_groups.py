"""Offline tests for extra lens streams and NVR multi-view camera groups."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("multiview_generator", ROOT / "bridge/gen_go2rtc.py")
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)
AUTH = {"username": "eufy", "password": "0123456789abcdef"}


def named(*channels):
    return [(f"eufy_cam{ch}", {"channel": ch, "name": f"Cam {ch}", "sn": f"CAM{ch}", "status": 1})
            for ch in channels]


def test_extra_lenses_add_one_stream_per_lens():
    lenses = gen.parse_extra_lenses("0:0, 1:0, 0:0")
    assert lenses == {0: [0], 1: [0]}
    streams = yaml.safe_load(gen.render_config(named(0, 1, 2), **AUTH, extra_lenses=lenses))["streams"]
    assert set(streams) == {"eufy_cam0", "eufy_cam1", "eufy_cam2", "eufy_cam0_lens0", "eufy_cam1_lens0"}
    assert "eufy_run.py 0 --sensor 0 --rtsp" in streams["eufy_cam0_lens0"]
    assert all(gen.STREAM_NAME.match(name) for name in streams)


def test_extra_lenses_only_for_enabled_cameras():
    streams = yaml.safe_load(
        gen.render_config(named(1), **AUTH, extra_lenses=gen.parse_extra_lenses("0:0"))
    )["streams"]
    assert set(streams) == {"eufy_cam1"}


@pytest.mark.parametrize("value", ["0", "0:", "x:0", "0:8", "256:0", "0:-1"])
def test_extra_lenses_reject_bad_values(value):
    with pytest.raises(ValueError):
        gen.parse_extra_lenses(value)


def test_camera_groups_render_multiview_streams():
    groups = gen.parse_camera_groups("front=0-0-1,1-1-1,3-0-0,4-1-0; inside=0-4-0")
    assert groups == [("front", [(0, 0, 1), (1, 1, 1), (3, 0, 0), (4, 1, 0)]), ("inside", [(0, 4, 0)])]
    streams = yaml.safe_load(gen.render_config(named(0, 1, 4), **AUTH, camera_groups=groups))["streams"]
    assert "eufy_run.py g:0-0-1,1-1-1,3-0-0,4-1-0 --rtsp" in streams["eufy_group_front"]
    assert "eufy_run.py g:0-4-0 --rtsp" in streams["eufy_group_inside"]
    assert all(gen.STREAM_NAME.match(name) for name in streams)


def test_no_groups_without_online_cameras():
    config = gen.render_config([], **AUTH, camera_groups=gen.parse_camera_groups("front=0-0-1"))
    assert yaml.safe_load(config)["streams"] == {}


@pytest.mark.parametrize("value", [
    "Front=0-0-1", "front=", "front=0-0", "front=9-0-1", "front=0-0-8",
    "front=0-0-1,0-1-1", "front=0-0-1;front=0-1-1", "a b=0-0-1", "front=0-x-1",
])
def test_camera_groups_reject_bad_values(value):
    with pytest.raises(ValueError):
        gen.parse_camera_groups(value)


def test_generate_passes_lenses_and_groups(tmp_path):
    source, output, registry = (tmp_path / n for n in ("cameras.json", "go2rtc.yaml", "names.json"))
    source.write_text(json.dumps({"nvr_sn": "TEST_NVR", "cameras": [
        {"channel": 0, "name": "Front", "sn": "A", "status": 1},
        {"channel": 1, "name": "Back", "sn": "B", "status": 1},
    ]}))
    gen.generate(source, output, registry, **AUTH,
                 extra_lenses={0: [0]}, camera_groups=[("both", [(0, 0, 1), (1, 1, 1)])])
    streams = yaml.safe_load(output.read_text())["streams"]
    assert {"eufy_front", "eufy_back", "eufy_front_lens0", "eufy_group_both"} <= set(streams)


def test_startstream_uses_web_multiview_request():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    assert '"stitch_mode": 3' in stream
    assert '"station_video_type": 6' in stream
    # An explicit --sensor (extra lens stream) overrides the per-camera sensor map.
    assert "STREAM_SENSORS = {c: SENSOR for c in CHANNELS}" in stream
    assert "crop=iw*{cols}/3:ih*{rows}/3:0:0" in stream


@pytest.mark.parametrize("args, message", [
    (["g:0-0-1,0-1-1"], "group spec needs"),
    (["g:0-0"], "group spec needs"),
    (["g:a-0-1"], "group spec must look like"),
    (["0", "--sensor", "9"], "--sensor must be between 0 and 7"),
])
def test_stream_engine_rejects_bad_group_and_sensor_arguments(tmp_path, args, message):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"userId": "test", "authToken": "t", "gtoken": "g", "stationSn": "TEST"}))
    env = {**os.environ, "EUFY_AUTH": str(auth)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "bridge/eufy_stream.py"), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert message in result.stderr
