#!/usr/bin/env python3
"""Generate validated go2rtc configuration with persistent camera stream names.

Run after eufy_stream.py --discover. The registry lives beside cameras.json,
so the add-on keeps names under /data across restarts and camera renames.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

ROOT = os.path.dirname(os.path.abspath(__file__))
CAMERAS_JSON = os.environ.get("EUFY_CAMERAS", os.path.join(ROOT, "cameras.json"))
_STREAM_RE = re.compile(r"eufy_[a-z0-9_]+")


def slug(name: str | None, ch: int) -> str:
    """Retain the previous naming convention for ordinary, unique camera names."""
    value = re.sub(r"[^a-z0-9]+", "_", (name or f"ch{ch}").lower()).strip("_")
    return "eufy_" + (value or f"ch{ch}")


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    """Reject ambiguous/malformed channels before touching the working config."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("cameras"), list):
        raise ValueError("camera manifest must contain a cameras list")
    channels: set[int] = set()
    serials: set[str] = set()
    cameras = []
    for camera in manifest["cameras"]:
        if not isinstance(camera, dict):
            raise ValueError("camera entries must be objects")
        channel = camera.get("channel")
        if type(channel) is not int or not 0 <= channel <= 255:
            raise ValueError("camera channel must be an integer from 0 to 255")
        if channel in channels:
            raise ValueError("camera manifest contains duplicate channels")
        channels.add(channel)
        if camera.get("name") is not None and not isinstance(camera["name"], str):
            raise ValueError("camera name must be text")
        serial = camera.get("sn")
        if serial is not None and not isinstance(serial, str):
            raise ValueError("camera serial must be text")
        if serial and serial in serials:
            raise ValueError("camera manifest contains duplicate serials")
        if serial:
            serials.add(serial)
        cameras.append(dict(camera))
    return sorted(cameras, key=lambda camera: camera["channel"])


def assign_names(manifest: dict[str, Any], registry: dict[str, Any] | None = None):
    """Reserve unique names for online AND offline cameras; never rename old streams."""
    cameras = validate_manifest(manifest)
    if registry is not None and (not isinstance(registry, dict) or registry.get("version") != 1
                                 or not isinstance(registry.get("names"), dict)):
        raise ValueError("invalid stream-name registry; restore it from a backup")
    names = dict(registry["names"]) if registry and registry.get("nvr_sn") == manifest.get("nvr_sn") else {}
    if (any(not isinstance(value, str) or not _STREAM_RE.fullmatch(value) for value in names.values())
            or len(set(names.values())) != len(names)):
        raise ValueError("stream-name registry contains invalid or duplicate names")
    used = set(names.values())
    streams = []
    for camera in cameras:
        # Channel is the NVR feed identity; replacing a camera on a channel preserves automations.
        key = str(camera["channel"])
        if key not in names:
            base = slug(camera.get("name"), camera["channel"])
            name = base
            suffix = 1
            while name in used:
                name = f"{base}_ch{camera['channel']}" + (f"_{suffix}" if suffix > 1 else "")
                suffix += 1
            names[key] = name
            used.add(name)
        streams.append((names[key], camera))
    return streams, {"version": 1, "nvr_sn": manifest.get("nvr_sn"), "names": names}


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("go2rtc ports must be between 1 and 65535")
    return port


def render_config(streams, api_port: int, rtsp_port: int, webrtc_port: int) -> str:
    online = [(name, camera) for name, camera in streams if camera.get("status") not in (0, "0", False)]
    lines = ["# Generated camera streams. Keep this API on a trusted LAN.", "streams:" if online else "streams: {}"]
    for name, camera in online:
        command = f"exec:python eufy_stream.py {camera['channel']} --rtsp {{output}}"
        lines.append(f"  {name}: {json.dumps(command)}")
    lines.extend(["", "rtsp:", f'  listen: ":{rtsp_port}"', "", "api:",
                  f'  listen: ":{api_port}"', "", "webrtc:",
                  f'  listen: ":{webrtc_port}"', "", "log:", "  level: info", ""])
    return "\n".join(lines)


def atomic_write(path: Path, content: str) -> None:
    """Never leave a truncated working config or name registry behind."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}-", delete=False) as handle:
            temporary = handle.name
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def main() -> int:
    camera_path = Path(CAMERAS_JSON)
    registry_path = Path(os.environ.get("EUFY_STREAM_REGISTRY", str(camera_path.with_name("stream_names.json"))))
    output_path = Path(os.environ.get("EUFY_GO2RTC_CONFIG", os.path.join(ROOT, "go2rtc.yaml")))
    try:
        if not camera_path.is_file():
            raise ValueError("camera manifest not found; run python eufy_stream.py --discover first")
        manifest = json.loads(camera_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else None
        streams, registry = assign_names(manifest, registry)
        api_port = _port(os.environ.get("GO2RTC_API_PORT", "1984"))
        rtsp_port = _port(os.environ.get("GO2RTC_RTSP_PORT", "8554"))
        webrtc_port = _port(os.environ.get("GO2RTC_WEBRTC_PORT", "8555"))
        if len({api_port, rtsp_port, webrtc_port}) != 3:
            raise ValueError("API, RTSP and WebRTC must use different ports")
        config = render_config(streams, api_port, rtsp_port, webrtc_port)
        atomic_write(registry_path, json.dumps(registry, indent=2) + "\n")
        atomic_write(output_path, config)
    except (ValueError, OSError) as error:
        # JSON decoding errors can contain private input snippets: report only the class.
        message = "invalid JSON; working configuration was not replaced" if isinstance(error, json.JSONDecodeError) else str(error)
        print(f"gen_go2rtc: {message}", file=sys.stderr)
        return 1
    online = [(name, camera) for name, camera in streams if camera.get("status") not in (0, "0", False)]
    print(f"Generated {len(online)} online camera stream(s); {len(streams) - len(online)} offline channel(s) reserved.")
    bridge_host = os.environ.get("BRIDGE_IP", sys.argv[1] if len(sys.argv) > 1 else "BRIDGE_IP")
    if ":" in bridge_host and not bridge_host.startswith("["):
        bridge_host = f"[{bridge_host}]"
    print("\n# Home Assistant go2rtc pull configuration:\nstreams:")
    for name, _ in online:
        print(f"  {name}:\n  - rtsp://{bridge_host}:{rtsp_port}/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
