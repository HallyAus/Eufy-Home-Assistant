"""Config flow for Eufy NVR (local).

The user supplies only where go2rtc lives (host + ports). Cameras are discovered
automatically afterwards by the coordinator — no channel names, no per-camera
entry. The flow validates that go2rtc's REST API answers before creating the
entry, so misconfiguration is caught immediately.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_PORT,
    CONF_HOST,
    CONF_RTSP_PORT,
    DEFAULT_API_PORT,
    DEFAULT_HOST,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    REQUEST_TIMEOUT,
)
from .go2rtc_api import (
    Go2RtcClient,
    Go2RtcError,
    host_from_internal_url,
    normalize_host,
)


async def _validate_go2rtc(hass, host: str, api_port: int) -> tuple[str, int]:
    """Probe go2rtc and return its normalized host and Eufy stream count.

    Distinguishes bad input, an unreachable API, and a reachable bridge that has
    not published any Eufy cameras yet so the setup form can be actionable.
    """
    session = async_get_clientsession(hass)
    try:
        normalized_host = normalize_host(host)
    except ValueError as err:
        raise InvalidEndpoint from err

    candidates = [normalized_host]
    if normalized_host == DEFAULT_HOST:
        fallback = host_from_internal_url(hass.config.internal_url)
        if fallback and fallback != normalized_host:
            candidates.append(fallback)

    last_error: Go2RtcError | None = None
    for candidate in candidates:
        client = Go2RtcClient(session, candidate, api_port, REQUEST_TIMEOUT)
        try:
            streams = await client.async_get_streams()
        except Go2RtcError as err:
            last_error = err
            continue
        break
    else:
        raise CannotConnect from last_error

    if not streams:
        if client.total_stream_count:
            raise WrongInstance
        raise NoStreams
    return client.host, len(streams)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the form schema with the given defaults."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
            vol.Required(
                CONF_API_PORT, default=defaults.get(CONF_API_PORT, DEFAULT_API_PORT)
            ): int,
            vol.Required(
                CONF_RTSP_PORT, default=defaults.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
            ): int,
        }
    )


class EufyNvrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config + reconfigure flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            api_port = user_input[CONF_API_PORT]
            rtsp_port = user_input[CONF_RTSP_PORT]

            try:
                host, _ = await _validate_go2rtc(self.hass, host, api_port)
            except InvalidEndpoint:
                errors["base"] = "invalid_endpoint"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except WrongInstance:
                errors["base"] = "wrong_instance"
            except NoStreams:
                errors["base"] = "no_streams"
            else:
                # One entry per normalized go2rtc API endpoint.
                await self.async_set_unique_id(f"{host}:{api_port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Eufy NVR ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_API_PORT: api_port,
                        CONF_RTSP_PORT: rtsp_port,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow editing host/ports of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            api_port = user_input[CONF_API_PORT]
            try:
                host, _ = await _validate_go2rtc(self.hass, host, api_port)
            except InvalidEndpoint:
                errors["base"] = "invalid_endpoint"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except WrongInstance:
                errors["base"] = "wrong_instance"
            except NoStreams:
                errors["base"] = "no_streams"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: host,
                        CONF_API_PORT: api_port,
                        CONF_RTSP_PORT: user_input[CONF_RTSP_PORT],
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(dict(entry.data)),
            errors=errors,
        )


class CannotConnect(Exception):
    """Raised when go2rtc's REST API is unreachable."""


class InvalidEndpoint(Exception):
    """Raised when a host or port is invalid."""


class NoStreams(Exception):
    """Raised when go2rtc is reachable but has no Eufy streams."""


class WrongInstance(Exception):
    """Raised when the endpoint contains only non-Eufy streams."""
