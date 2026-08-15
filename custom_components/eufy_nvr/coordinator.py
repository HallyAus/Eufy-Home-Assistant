"""DataUpdateCoordinator that polls go2rtc for the list of eufy_* streams.

The coordinator is the single source of truth for "which cameras exist and are
they reachable". The camera platform listens to it: when go2rtc gains a new
``eufy_*`` stream the coordinator picks it up and the platform adds a new camera
entity automatically — no YAML, no re-config.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_PORT,
    CONF_HOST,
    DOMAIN,
    REQUEST_TIMEOUT,
    UPDATE_INTERVAL,
)
from .go2rtc_api import Go2RtcClient, Go2RtcError

_LOGGER = logging.getLogger(__name__)


class EufyNvrCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetch and cache the set of eufy_* streams advertised by go2rtc.

    ``data`` is a mapping of ``{stream_name: stream_info}`` where ``stream_info``
    is the raw object go2rtc returns for that stream. Presence of a key means the
    stream exists; that is what camera availability is derived from.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        self._client = Go2RtcClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_API_PORT],
            REQUEST_TIMEOUT,
        )
        self.host = self._client.host
        self.api_port = self._client.api_port

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({self.host})",
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Query go2rtc and return only the eufy_* streams.

        Raises ``UpdateFailed`` on transport/HTTP/parse errors so HA marks every
        dependent entity unavailable until go2rtc is reachable again.
        """
        try:
            streams = await self._client.async_get_streams()
        except Go2RtcError as err:
            raise UpdateFailed(str(err)) from err

        _LOGGER.debug(
            "Discovered %d eufy stream(s) from %s: %s",
            len(streams),
            self._client.url,
            ", ".join(sorted(streams)) or "(none)",
        )
        return streams
