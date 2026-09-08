#!/usr/bin/env python3
"""Validate discovery state and generate stable, on-demand go2rtc streams.

Stream identities are persisted so camera renames do not rename Home Assistant
entities. The generated go2rtc surface is restricted to the modules and API paths
this integration actually requires.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STREAM_NAME = re.compile(r"eufy_[a-z0-9_]+\Z")
STREAM_START_TIMEOUT = int(os.environ.get("EUFY_STREAM_START_TIMEOUT", "60"))


def slug(name: str | None, channel: int) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", (name or f"ch{channel}").lower()).strip("_")
    return "eufy_" + (value or f"ch{channel}")


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("cameras"), list):
        raise ValueError("camera manifest must contain a cameras list")
    station = value.get("nvr_sn", "")
    if not isinstance(station, str):
        raise ValueError("nvr_sn must be a string")
    channels: set[int] = set()
    serials: set[str] = set()
    cameras = []
    for item in value["cameras"]:
        if not isinstance(item, dict):
            raise ValueError("each camera must be an object")
        channel = item.get("channel")
        if type(channel) is not int or not 0 <= channel <= 255:
            raise ValueError("camera channel must be an integer from 0 to 255")
        if channel in channels:
            raise ValueError("duplicate camera channel in discovery state")
        channels.add(channel)
        name, serial = item.get("name"), item.get("sn")
        if name is not None and not isinstance(name, str):
            raise ValueError("camera name must be a string or null")
        if serial is not None and not isinstance(serial, str):
            raise ValueError("camera serial must be a string or null")
        if serial and serial in serials:
            raise ValueError("duplicate camera serial in discovery state")
        if serial:
            serials.add(serial)
        status = item.get("status")
        if status is not None and (
            type(status) not in (int, str) or status not in (0, 1, "0", "1")
        ):
            raise ValueError("camera status must be 0, 1, or null")
        cameras.append({**item, "status": int(status) if status is not None else None})
    return {**value, "nvr_sn": station, "cameras": cameras}


def validate_registry(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported stream-name registry; restore a valid backup")
    names = value.get("names")
    if not isinstance(names, dict):
        raise ValueError("stream-name registry must contain a names object")
    if any(
        not isinstance(key, str)
        or not isinstance(name, str)
        or not STREAM_NAME.fullmatch(name)
        for key, name in names.items()
    ):
        raise ValueError("invalid entry in stream-name registry")
    if len(set(names.values())) != len(names):
        raise ValueError("duplicate stream names in registry")
    return dict(names)


def camera_key(station: str, camera: dict[str, Any]) -> str:
    identity = [station, "serial", camera["sn"]] if camera.get("sn") else [
        station, "channel", camera["channel"]
    ]
    return json.dumps(identity, ensure_ascii=True, separators=(",", ":"))


def assign_names(
    manifest: dict[str, Any], previous: dict[str, str]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
    names = dict(previous)
    used = set(names.values())
    result = []
    for camera in sorted(manifest["cameras"], key=lambda camera: camera["channel"]):
        key = camera_key(manifest["nvr_sn"], camera)
        if key not in names:
            base = slug(camera.get("name"), camera["channel"])
            candidate = base
            if candidate in used:
                candidate = f"{base}_ch{camera['channel']}"
            counter = 2
            while candidate in used:
                candidate = f"{base}_ch{camera['channel']}_{counter}"
                counter += 1
            names[key] = candidate
            used.add(candidate)
        result.append((names[key], camera))
    return result, names


def validate_port(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("ports must be integers from 1 to 65535")
    return value


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    """Validate credentials before embedding them in the private go2rtc config."""
    if not isinstance(username, str) or not 1 <= len(username) <= 64:
        raise ValueError("go2rtc username must be 1 to 64 characters")
    if not isinstance(password, str) or not 16 <= len(password) <= 256:
        raise ValueError("go2rtc password must be 16 to 256 characters")
    if any(ord(character) < 32 or ord(character) == 127
           for character in username + password):
        raise ValueError("go2rtc credentials cannot contain control characters")
    return username, password


def render_config(
    named: list[tuple[str, dict[str, Any]]],
    username: str,
    password: str,
    api_port: int = 1984,
    rtsp_port: int = 8554,
    webrtc_port: int = 8555,
) -> str:
    ports = [validate_port(port) for port in (api_port, rtsp_port, webrtc_port)]
    if len(set(ports)) != 3:
        raise ValueError("API, RTSP and WebRTC ports must be different")
    if not 1 <= STREAM_START_TIMEOUT <= 300:
        raise ValueError("EUFY_STREAM_START_TIMEOUT must be between 1 and 300 seconds")
    username, password = validate_credentials(username, password)

    online = [(name, camera) for name, camera in named if camera.get("status") != 0]
    lines = [
        "# Generated from validated discovery state. Online, on-demand streams.",
        "app:",
        "  modules: [api, rtsp, webrtc, exec]",
        "",
        "streams:" if online else "streams: {}",
    ]
    for name, camera in online:
        command = (
            f"exec:python eufy_run.py {camera['channel']} --rtsp {{output}}"
            f"#starttimeout={STREAM_START_TIMEOUT}#killtimeout=8"
        )
        lines.append(f"  {name}: {json.dumps(command)}")
    lines.extend([
        "",
        "exec:",
        "  allow_paths: [python]",
        "",
        "rtsp:",
        f'  listen: ":{rtsp_port}"',
        f"  username: {json.dumps(username)}",
        f"  password: {json.dumps(password)}",
        "",
        "api:",
        f'  listen: ":{api_port}"',
        f"  username: {json.dumps(username)}",
        f"  password: {json.dumps(password)}",
        "  local_auth: false",
        "  allow_paths: [/api, /api/streams, /api/webrtc, /api/frame.jpeg]",
        "",
        "webrtc:",
        f'  listen: ":{webrtc_port}"',
        "",
        "log:",
        "  level: info",
        "",
    ])
    return "\n".join(lines)


def atomic_write(path: Path, text: str) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def generate(
    manifest_path: Path, output_path: Path, registry_path: Path,
    *, username: str, password: str, api_port: int = 1984,
    rtsp_port: int = 8554, webrtc_port: int = 8555,
) -> list[tuple[str, dict[str, Any]]]:
    paths = [path.resolve() for path in (manifest_path, output_path, registry_path)]
    if len(set(paths)) != 3:
        raise ValueError("manifest, config and stream registry must be different files")
    manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    previous = validate_registry(json.loads(registry_path.read_text(encoding="utf-8"))) \
        if registry_path.exists() else {}
    named, names = assign_names(manifest, previous)
    config = render_config(named, username, password, api_port, rtsp_port, webrtc_port)
    registry = json.dumps({"version": 1, "names": names}, indent=2, sort_keys=True) + "\n"
    atomic_write(registry_path, registry)
    atomic_write(output_path, config)
    return named


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    manifest_path = Path(os.environ.get("EUFY_CAMERAS", os.path.join(ROOT, "cameras.json")))
    registry_path = Path(os.environ.get("EUFY_STREAM_NAMES", str(manifest_path.with_name("stream_names.json"))))
    output_path = ROOT / "go2rtc.yaml"
    bridge_host = os.environ.get("BRIDGE_IP", argv[0] if argv else "BRIDGE_IP")
    if ":" in bridge_host and not bridge_host.startswith("["):
        bridge_host = f"[{bridge_host}]"
    try:
        api_port = int(os.environ.get("GO2RTC_API_PORT", "1984"))
        rtsp_port = int(os.environ.get("GO2RTC_RTSP_PORT", "8554"))
        webrtc_port = int(os.environ.get("GO2RTC_WEBRTC_PORT", "8555"))
        username = os.environ.get("GO2RTC_USERNAME", "")
        password = os.environ.get("GO2RTC_PASSWORD", "")
        named = generate(manifest_path, output_path, registry_path,
                         username=username, password=password,
                         api_port=api_port, rtsp_port=rtsp_port, webrtc_port=webrtc_port)
    except FileNotFoundError:
        print("gen_go2rtc: discovery file or output directory not found; run `python eufy_run.py --discover` first", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"gen_go2rtc: generation failed: {error}", file=sys.stderr)
        return 1
    online = [(name, camera) for name, camera in named if camera.get("status") != 0]
    print(f"wrote {output_path} ({len(online)} online cameras)")
    print("\n# --- Home Assistant upstream streams ---")
    print("# Credentials are intentionally omitted; URL-encode them before replacing the placeholders.")
    print("streams:" if online else "streams: {}")
    for name, _ in online:
        print(
            f"  {name}:\n"
            f"  - rtsp://<username>:<password>@{bridge_host}:{rtsp_port}/{name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
