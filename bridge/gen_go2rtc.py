#!/usr/bin/env python3
"""Generate go2rtc.yaml from auto-discovered cameras.json.

The generated config is intentionally small and hardened: only the go2rtc modules
and HTTP paths required by this bridge are enabled, exec sources are restricted to
Python, and cold Eufy sessions get a generous first-frame startup timeout.
"""

import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
CAMERAS_JSON = os.environ.get("EUFY_CAMERAS", os.path.join(ROOT, "cameras.json"))
BRIDGE_IP = os.environ.get(
    "BRIDGE_IP", sys.argv[1] if len(sys.argv) > 1 else "BRIDGE_IP"
)
API_PORT = int(os.environ.get("GO2RTC_API_PORT", "1984"))
RTSP_PORT = int(os.environ.get("GO2RTC_RTSP_PORT", "8554"))
WEBRTC_PORT = int(os.environ.get("GO2RTC_WEBRTC_PORT", "8555"))
STREAM_START_TIMEOUT = int(os.environ.get("EUFY_STREAM_START_TIMEOUT", "60"))

try:
    with open(CAMERAS_JSON, encoding="utf-8") as _f:
        cams = json.load(_f)
except FileNotFoundError:
    sys.exit(
        f"gen_go2rtc: {CAMERAS_JSON} not found "
        "- run `python eufy_stream.py --discover` first"
    )


def slug(name, ch):
    """Return a readable base stream slug."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or f"ch{ch}").lower()).strip("_")
    return "eufy_" + (s or f"ch{ch}")


def stream_names(cameras):
    """Return deterministic collision-safe stream names for camera records.

    Duplicate Eufy names are common (for example two cameras both called
    ``Backyard``). go2rtc YAML keys must be unique, so duplicate slugs receive a
    channel suffix while the normal single-camera case keeps its existing name.
    """
    base = [slug(c.get("name"), c.get("channel")) for c in cameras]
    counts = Counter(base)
    result = []
    used = set()
    for name, camera in zip(base, cameras, strict=True):
        candidate = name
        if counts[name] > 1 or candidate in used:
            candidate = f"{name}_ch{camera.get('channel')}"
        suffix = 2
        unique = candidate
        while unique in used:
            unique = f"{candidate}_{suffix}"
            suffix += 1
        used.add(unique)
        result.append(unique)
    return result


camera_list = cams.get("cameras")
if not isinstance(camera_list, list):
    sys.exit(f"gen_go2rtc: {CAMERAS_JSON} does not contain a cameras list")

names = stream_names(camera_list)
named = list(zip(names, camera_list, strict=True))

# Only publish ONLINE cameras. An offline channel has no producer, so emitting it
# creates a dead stream/entity. The persisted manifest still retains it and a later
# successful discovery will publish it again.
online = [(name, c) for name, c in named if c.get("status") != 0]
offline = [name for name, c in named if c.get("status") == 0]
if offline:
    print(
        "skipping OFFLINE camera(s) (no stream/entity until they're back online):",
        ", ".join(offline),
    )

lines = [
    f"# Auto-generated from cameras.json (eufy NVR {cams.get('nvr_sn', '')}). Online cameras only; on-demand streams.",
    "app:",
    "  modules: [api, rtsp, webrtc, exec]",
    "",
    "streams:",
]
for name, c in online:
    # go2rtc 1.9.14+ supports starttimeout for exec sources. Eufy's reversed WebRTC
    # cold start can legitimately take longer than go2rtc's historical default,
    # particularly when TURN is involved, so avoid killing a healthy startup early.
    lines.append(
        f'  {name}: "exec:python eufy_stream.py {c["channel"]} --rtsp {{output}}#starttimeout={STREAM_START_TIMEOUT}#killtimeout=5"'
    )
lines += [
    "",
    "exec:",
    "  allow_paths: [python]",
    "",
    "rtsp:",
    f'  listen: ":{RTSP_PORT}"',
    "",
    "api:",
    f'  listen: ":{API_PORT}"',
    "  allow_paths: [/api, /api/streams, /api/webrtc, /api/frame.jpeg]",
    "",
    "webrtc:",
    f'  listen: ":{WEBRTC_PORT}"',
    "",
    "log:",
    "  level: info",
    "",
]

out_path = os.path.join(ROOT, "go2rtc.yaml")
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("wrote", out_path, f"({len(online)} online cameras)")

print(
    "\n# --- paste into Home Assistant /config/go2rtc.yaml (set BRIDGE_IP) ---\nstreams:"
)
for name, _ in online:
    print(f"  {name}:\n  - rtsp://{BRIDGE_IP}:{RTSP_PORT}/{name}")
