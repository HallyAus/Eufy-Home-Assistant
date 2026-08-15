"""Eufy NVR (local) — auto-discovering camera integration.

The heavy lifting (reversed-WebRTC -> RTSP) is done by the bridge/add-on, which
runs a go2rtc instance. This integration is a thin, local-polling client: it asks
go2rtc which ``eufy_*`` streams exist and exposes each as a Home Assistant camera,
grouped under a single "Eufy NVR" device. New cameras appear automatically as the
bridge publishes them — there is nothing to configure per camera.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import EufyNvrCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CAMERA]

# Typed config entry so ``entry.runtime_data`` carries the coordinator (HA 2024.11+).
type EufyNvrConfigEntry = ConfigEntry[EufyNvrCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EufyNvrConfigEntry) -> bool:
    """Set up Eufy NVR from a config entry."""
    coordinator = EufyNvrCoordinator(hass, entry)

    # Fail setup (with a retry) if go2rtc is not reachable yet — the add-on may
    # still be discovering cameras when HA starts.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EufyNvrConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: EufyNvrConfigEntry
) -> None:
    """Reload the entry when its options/data change (e.g. host edited)."""
    await hass.config_entries.async_reload(entry.entry_id)
