"""Constants for the Eufy NVR (local) integration.

Cameras are auto-discovered from a go2rtc instance that the bridge/add-on runs.
The integration never talks to the eufy cloud or the NVR directly — it only reads
go2rtc's REST API to learn which ``eufy_*`` streams exist, then exposes each as a
Home Assistant camera that pulls ``rtsp://<host>:<rtsp_port>/<stream>``.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "eufy_nvr"

# --- config entry keys -------------------------------------------------------
CONF_HOST = "host"
CONF_API_PORT = "api_port"
CONF_RTSP_PORT = "rtsp_port"

# --- defaults ----------------------------------------------------------------
# The add-on is host-networked but HA Core is a separate container, so loopback
# points at Core rather than the add-on. mDNS is a better starting value; users
# can enter the HA host's LAN address when their network does not resolve it.
DEFAULT_HOST = "homeassistant.local"
DEFAULT_API_PORT = 1985  # dedicated Eufy go2rtc REST API / web UI
DEFAULT_RTSP_PORT = 8556  # dedicated Eufy go2rtc RTSP server

# How often the coordinator re-queries go2rtc so newly added cameras appear and
# availability is kept fresh. Cheap localhost call; 30s is responsive enough.
UPDATE_INTERVAL = timedelta(seconds=30)

# Network timeout for the go2rtc REST call.
REQUEST_TIMEOUT = 10

# DeviceInfo identity for the single "Eufy NVR" hub device the cameras hang off.
MANUFACTURER = "eufy"
MODEL = "PoE NVR (S4 / T8N00)"
DEVICE_NAME = "Eufy NVR"
