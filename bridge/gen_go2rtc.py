#!/usr/bin/env python3
"""Generate go2rtc.yaml from auto-discovered cameras.json (run `python eufy_stream.py --discover` first).

Turns the discovered channel->name list into friendly, on-demand go2rtc streams, e.g.
  Garage (ch 0) -> stream "eufy_garage" -> rtsp://<bridge>:8554/eufy_garage
Also prints the block to paste into Home Assistant's /config/go2rtc.yaml (HA pulls from this bridge host).
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))  # bridge/
# Same override the discovery step (eufy_stream.py) and the add-on's run.sh use, so
# both ends agree on the path even when the manifest is persisted under /data.
CAMERAS_JSON = os.environ.get("EUFY_CAMERAS", os.path.join(ROOT, "cameras.json"))
BRIDGE_IP = os.environ.get(
    "BRIDGE_IP", sys.argv[1] if len(sys.argv) > 1 else "BRIDGE_IP"
)
API_PORT = int(os.environ.get("GO2RTC_API_PORT", "1984"))
RTSP_PORT = int(os.environ.get("GO2RTC_RTSP_PORT", "8554"))
WEBRTC_PORT = int(os.environ.get("GO2RTC_WEBRTC_PORT", "8555"))
try:
    with open(CAMERAS_JSON) as _f:
        cams = json.load(_f)
except FileNotFoundError:
    sys.exit(
        f"gen_go2rtc: {CAMERAS_JSON} not found "
        "- run `python eufy_stream.py --discover` first"
    )


def slug(name, ch):
    s = re.sub(r"[^a-z0-9]+", "_", (name or f"ch{ch}").lower()).strip("_")
    return "eufy_" + (s or f"ch{ch}")


named = [(slug(c["name"], c["channel"]), c) for c in cams["cameras"]]
# Only publish ONLINE cameras. An offline channel (status 0) has no producer, so emitting it
# created a dead go2rtc stream -> the HA integration made a green entity that 404s on open
# ("provisioned but no feed"). When the camera comes back, the next discovery re-adds it.
online = [(name, c) for name, c in named if c.get("status") != 0]
offline = [name for name, c in named if c.get("status") == 0]
if offline:
    print(
        "skipping OFFLINE camera(s) (no stream/entity until they're back online):",
        ", ".join(offline),
    )

lines = [
    f"# Auto-generated from cameras.json (eufy NVR {cams.get('nvr_sn', '')}). Online cameras only; on-demand streams.",
    "streams:",
]
for name, c in online:
    lines.append(
        f'  {name}: "exec:python eufy_stream.py {c["channel"]} --rtsp {{output}}"'
    )
lines += [
    "",
    "rtsp:",
    f'  listen: ":{RTSP_PORT}"',
    "",
    "api:",
    f'  listen: ":{API_PORT}"',
    "",
    "webrtc:",
    f'  listen: ":{WEBRTC_PORT}"',
    "",
    "log:",
    "  level: info",
    "",
]
open(os.path.join(ROOT, "go2rtc.yaml"), "w").write("\n".join(lines))
print("wrote", os.path.join(ROOT, "go2rtc.yaml"), f"({len(online)} online cameras)")

print(
    "\n# --- paste into Home Assistant /config/go2rtc.yaml (set BRIDGE_IP) ---\nstreams:"
)
for name, c in online:
    print(f"  {name}:\n  - rtsp://{BRIDGE_IP}:{RTSP_PORT}/{name}")
