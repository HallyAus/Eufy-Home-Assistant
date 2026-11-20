"""Per-camera ``sensor`` selection for the cmd 1003 startStream request.

The NVR's ``chn_list`` entry carries a ``sensor`` index. Dual-lens cameras such
as the T8214 doorbell stream on sensor 1, which the bridge has always sent, but
single-lens PoE Cam E40 (T8P00) cameras only deliver video on sensor 0: with
sensor 1 the session opens and startStream is acknowledged, but no video frame
ever arrives.

Resolution order for each requested channel:

1. an explicit ``EUFY_STREAM_SENSORS`` override (``"channel:sensor,..."``),
2. a known single-sensor model, matched on the camera serial from discovery,
3. the historical default of sensor 1, so unlisted models are unaffected.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_SENSOR = 1
# Serial prefixes of cameras confirmed on hardware to stream only on sensor 0.
SINGLE_SENSOR_SERIAL_PREFIXES: tuple[str, ...] = ("T8P00",)  # PoE Cam E40


def parse_stream_sensors(value: str | None) -> dict[int, int]:
    """Parse ``"0:0,2:0"`` into ``{0: 0, 2: 0}``; blank means no overrides."""
    if value is None or not value.strip():
        return {}
    overrides: dict[int, int] = {}
    for part in value.split(","):
        channel, sep, sensor = part.strip().partition(":")
        channel, sensor = channel.strip(), sensor.strip()
        if (
            not sep
            or not channel.isascii() or not channel.isdecimal()
            or not sensor.isascii() or not sensor.isdecimal()
            or not 0 <= int(channel) <= 255
            or int(sensor) > 1
        ):
            raise ValueError(
                "stream_sensors must be comma-separated channel:sensor pairs, "
                "e.g. '0:0,2:0', with channels 0-255 and sensor 0 or 1"
            )
        overrides[int(channel)] = int(sensor)
    return overrides


def serials_by_channel(manifest: Any) -> dict[int, str]:
    """Map discovered channel numbers to camera serials, ignoring malformed rows."""
    cameras = manifest.get("cameras") if isinstance(manifest, Mapping) else None
    serials: dict[int, str] = {}
    for camera in cameras if isinstance(cameras, list) else ():
        if not isinstance(camera, Mapping):
            continue
        channel, serial = camera.get("channel"), camera.get("sn")
        if isinstance(channel, int) and not isinstance(channel, bool) and isinstance(serial, str):
            serials[channel] = serial
    return serials


def load_serials(manifest_path: str | Path) -> dict[int, str]:
    """Best-effort read of the discovery manifest; a missing or bad file means no models."""
    try:
        return serials_by_channel(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def sensor_for_channel(
    channel: int,
    serials: Mapping[int, str],
    overrides: Mapping[int, int],
) -> int:
    """Return the startStream sensor index for one channel."""
    if channel in overrides:
        return overrides[channel]
    serial = serials.get(channel, "").upper()
    if serial.startswith(SINGLE_SENSOR_SERIAL_PREFIXES):
        return 0
    return DEFAULT_SENSOR


def sensors_for_channels(
    channels: Iterable[int],
    serials: Mapping[int, str],
    overrides: Mapping[int, int],
) -> dict[int, int]:
    """Resolve the sensor index for every requested channel."""
    return {channel: sensor_for_channel(channel, serials, overrides) for channel in channels}
