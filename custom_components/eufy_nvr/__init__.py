"""Eufy NVR (local) — auto-discovering camera integration.

The heavy lifting (reversed-WebRTC -> RTSP) is done by the bridge/add-on, which
runs a go2rtc instance. This integration is a thin, local-polling client: it asks
go2rtc which ``eufy_*`` streams exist and exposes each as a Home Assistant camera,
grouped under a single "Eufy NVR" device. New cameras appear automatically as the
bridge publishes them — there is nothing to configure per camera.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import EufyNvrCoordinator
from .go2rtc_api import validate_credentials

PLATFORMS: list[Platform] = [Platform.CAMERA]

# Typed config entry so ``entry.runtime_data`` carries the coordinator (HA 2024.11+).
type EufyNvrConfigEntry = ConfigEntry[EufyNvrCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EufyNvrConfigEntry) -> bool:
    """Set up Eufy NVR from a config entry."""
    try:
        validate_credentials(
            entry.data.get(CONF_USERNAME, ""), entry.data.get(CONF_PASSWORD, "")
        )
    except ValueError as err:
        # Entries created before local go2rtc authentication was introduced do
        # not contain credentials. Start HA's guided reauthentication flow.
        raise ConfigEntryAuthFailed(
            "Local go2rtc credentials are required; enter the credentials "
            "configured in the Eufy NVR add-on or bridge"
        ) from err

    coordinator = EufyNvrCoordinator(hass, entry)

    # Fail setup (with a retry) if go2rtc is not reachable yet — the add-on may
    # still be discovering cameras when HA starts.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    # Seed each camera before exposing entities. HA's camera proxy has a fixed
    # ten-second request ceiling, while this NVR can cold-start only one camera
    # at a time. Completing the bounded sequential seed here prevents the first
    # dashboard load from racing the primer and partially returning HTTP 500.
    await coordinator.async_prime_frames()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    primer = hass.async_create_task(
        coordinator.async_prime_frames_forever(),
        f"{DOMAIN} snapshot primer",
    )
    entry.async_on_unload(primer.cancel)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EufyNvrConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
