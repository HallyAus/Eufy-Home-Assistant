"""Camera platform — one entity per auto-discovered go2rtc ``eufy_*`` stream.

Entities are created dynamically from the coordinator's stream list. A listener
on the coordinator adds entities for streams that appear after setup, so plugging
in / enabling another NVR channel surfaces a new camera without any user action.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EufyNvrConfigEntry
from .const import (
    CONF_HOST,
    CONF_RTSP_PORT,
    DEVICE_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import EufyNvrCoordinator
from .go2rtc_api import STREAM_PREFIX, rtsp_url, stream_summary

_LOGGER = logging.getLogger(__name__)


def _friendly_name(stream: str) -> str:
    """Turn a stream name into a human label: ``eufy_front_gate`` -> ``Front Gate``."""
    base = stream[len(STREAM_PREFIX) :] if stream.startswith(STREAM_PREFIX) else stream
    return base.replace("_", " ").strip().title() or stream


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyNvrConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up cameras and keep them in sync with go2rtc's stream list."""
    coordinator = entry.runtime_data
    host: str = entry.data[CONF_HOST]
    rtsp_port: int = entry.data[CONF_RTSP_PORT]

    known: set[str] = set()

    @callback
    def _async_add_new_cameras() -> None:
        """Add entities for any newly discovered streams."""
        current = set(coordinator.data or {})
        new = current - known
        if not new:
            return
        known.update(new)
        async_add_entities(
            EufyNvrCamera(coordinator, entry.entry_id, host, rtsp_port, name)
            for name in sorted(new)
        )

    # Add whatever exists now, then react to future refreshes.
    _async_add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_cameras))


class EufyNvrCamera(CoordinatorEntity[EufyNvrCoordinator], Camera):
    """A single eufy NVR channel served as RTSP by the bridge's go2rtc."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: EufyNvrCoordinator,
        entry_id: str,
        host: str,
        rtsp_port: int,
        stream: str,
    ) -> None:
        """Initialise the camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)

        self._stream = stream
        self._stream_source = rtsp_url(host, rtsp_port, stream)

        self._attr_name = _friendly_name(stream)
        # Stable across host/port edits so history/automations survive a reconfig.
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{stream}"

        # Group every camera under one "Eufy NVR" device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=DEVICE_NAME,
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=f"http://{coordinator.host}:{coordinator.api_port}",
        )

    @property
    def available(self) -> bool:
        """Available only while go2rtc is reachable AND this stream still exists."""
        return super().available and self._stream in (self.coordinator.data or {})

    async def stream_source(self) -> str:
        """Return the RTSP URL HA's stream component should pull."""
        return self._stream_source

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Grab a still frame from the RTSP stream via HA's bundled ffmpeg."""
        return await async_get_image(
            self.hass, self._stream_source, width=width, height=height
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the stream name and source for diagnostics."""
        info = (self.coordinator.data or {}).get(self._stream, {})
        return {
            "stream_name": self._stream,
            **stream_summary(info),
        }
