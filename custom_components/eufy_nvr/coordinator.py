"""DataUpdateCoordinator that polls go2rtc for the list of eufy_* streams.

The coordinator is the single source of truth for "which cameras exist and are
they reachable". The camera platform listens to it: when go2rtc gains a new
``eufy_*`` stream the coordinator picks it up and the platform adds a new camera
entity automatically — no YAML, no re-config.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_PORT,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    FRAME_CACHE_TTL,
    FRAME_PRIME_INTERVAL,
    FRAME_PRIME_RETRY_INITIAL,
    FRAME_PRIME_RETRY_MAX,
    FRAME_STALE_TTL,
    REQUEST_TIMEOUT,
    UPDATE_INTERVAL,
)
from .go2rtc_api import Go2RtcClient, Go2RtcError
from .snapshot import SnapshotCache

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
            entry.data.get(CONF_USERNAME, ""),
            entry.data.get(CONF_PASSWORD, ""),
        )
        self.host = self._client.host
        self.api_port = self._client.api_port
        self._frame_cache = SnapshotCache(
            ttl=FRAME_CACHE_TTL,
            stale_ttl=FRAME_STALE_TTL,
        )
        self._primed_streams: set[str] = set()

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

    async def async_get_frame(self, stream: str) -> bytes:
        """Fetch or reuse one coalesced snapshot for a camera."""

        async def capture() -> bytes:
            return await self._client.async_get_frame(stream)

        frame = await self._frame_cache.async_get(stream, capture)
        if frame is None:
            raise Go2RtcError("snapshot endpoint returned no frame")
        return frame

    def discard_frame(self, stream: str) -> None:
        """Discard a removed camera's retained image."""
        self._frame_cache.discard(stream)
        self._primed_streams.discard(stream)

    async def async_prime_frames(self) -> int:
        """Seed missing cameras sequentially so dashboards have a fallback."""
        current = set(self.data or {})
        self._primed_streams.intersection_update(current)
        for stream in sorted(current - self._primed_streams):
            try:
                await self.async_get_frame(stream)
            except (Go2RtcError, TimeoutError):
                _LOGGER.debug("Could not prime snapshot for %s", stream)
            else:
                self._primed_streams.add(stream)
        _LOGGER.debug(
            "Primed %d/%d Eufy camera snapshots",
            len(self._primed_streams),
            len(current),
        )
        return len(self._primed_streams)

    async def async_prime_frames_forever(self) -> None:
        """Prime at startup, retry missing cameras, then refresh infrequently."""
        retry_delay = FRAME_PRIME_RETRY_INITIAL
        while True:
            primed = await self.async_prime_frames()
            total = len(self.data or {})
            if primed < total:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, FRAME_PRIME_RETRY_MAX)
                continue
            await asyncio.sleep(FRAME_PRIME_INTERVAL)
            self._primed_streams.clear()
            retry_delay = FRAME_PRIME_RETRY_INITIAL
