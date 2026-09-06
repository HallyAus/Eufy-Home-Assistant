"""Validated setup, collision-safe reconfiguration and performance options."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_PORT, CONF_HOST, CONF_RTSP_PORT, CONF_SCAN_INTERVAL,
    CONF_SNAPSHOT_INTERVAL, DEFAULT_API_PORT, DEFAULT_HOST, DEFAULT_RTSP_PORT,
    DEFAULT_SCAN_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL, DOMAIN,
    MAX_SCAN_INTERVAL, MAX_SNAPSHOT_INTERVAL, MIN_SCAN_INTERVAL,
    MIN_SNAPSHOT_INTERVAL, REQUEST_TIMEOUT,
)
from .go2rtc_api import (
    Go2RtcAuthenticationError, Go2RtcClient, Go2RtcError, Go2RtcPayloadError,
    host_from_internal_url, normalize_host, validate_port,
)


class EndpointError(Exception):
    """An actionable, translated setup problem."""
    error_key = "cannot_connect"


class InvalidEndpoint(EndpointError):
    error_key = "invalid_endpoint"


class CannotConnect(EndpointError):
    error_key = "cannot_connect"


class NoStreams(EndpointError):
    error_key = "no_streams"


class WrongInstance(EndpointError):
    error_key = "wrong_instance"


class AuthenticationRequired(EndpointError):
    error_key = "auth_required"


class InvalidResponse(EndpointError):
    error_key = "invalid_response"


async def _validate_go2rtc(hass, host: str, api_port: int) -> tuple[str, int]:
    """Validate the selected endpoint; only use HA's own configured host as fallback."""
    try:
        normalized_host = normalize_host(host)
        validate_port(api_port)
    except ValueError as err:
        raise InvalidEndpoint from err
    candidates = [normalized_host]
    if normalized_host == DEFAULT_HOST:
        fallback = host_from_internal_url(hass.config.internal_url)
        if fallback and fallback != normalized_host:
            candidates.append(fallback)
    problem: EndpointError = CannotConnect()
    session = async_get_clientsession(hass)
    for candidate in candidates:
        client = Go2RtcClient(session, candidate, api_port, REQUEST_TIMEOUT)
        try:
            streams = await client.async_get_streams()
        except Go2RtcAuthenticationError:
            problem = AuthenticationRequired()
        except Go2RtcPayloadError:
            problem = InvalidResponse()
        except Go2RtcError:
            # Do not replace a more actionable response from an earlier candidate.
            continue
        else:
            if streams:
                return client.host, len(streams)
            problem = WrongInstance() if client.total_stream_count else NoStreams()
    raise problem


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
        vol.Required(CONF_API_PORT, default=defaults.get(CONF_API_PORT, DEFAULT_API_PORT)):
            vol.All(int, vol.Range(min=1, max=65535)),
        vol.Required(CONF_RTSP_PORT, default=defaults.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)):
            vol.All(int, vol.Range(min=1, max=65535)),
    })


class EufyNvrConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> EufyNvrOptionsFlow:
        return EufyNvrOptionsFlow()

    async def _async_validate(self, values: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_port(values[CONF_API_PORT])
            validate_port(values[CONF_RTSP_PORT])
        except (ValueError, KeyError) as err:
            raise InvalidEndpoint from err
        host, _ = await _validate_go2rtc(self.hass, values[CONF_HOST], values[CONF_API_PORT])
        return {CONF_HOST: host, CONF_API_PORT: values[CONF_API_PORT],
                CONF_RTSP_PORT: values[CONF_RTSP_PORT]}

    def _is_duplicate(self, data: dict[str, Any], entry_id: str | None = None) -> bool:
        # Data comparison also catches old entries whose unique_id became stale.
        for entry in self._async_current_entries():
            if entry.entry_id == entry_id:
                continue
            try:
                host = normalize_host(entry.data.get(CONF_HOST, ""))
            except ValueError:
                continue
            if host == data[CONF_HOST] and entry.data.get(CONF_API_PORT) == data[CONF_API_PORT]:
                return True
        return False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await self._async_validate(user_input)
            except EndpointError as err:
                errors["base"] = err.error_key
            else:
                if self._is_duplicate(data):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(f"{data[CONF_HOST]}:{data[CONF_API_PORT]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Eufy NVR ({data[CONF_HOST]})", data=data)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await self._async_validate(user_input)
            except EndpointError as err:
                errors["base"] = err.error_key
            else:
                if self._is_duplicate(data, entry.entry_id):
                    errors["base"] = "already_configured"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates=data,
                        unique_id=f"{data[CONF_HOST]}:{data[CONF_API_PORT]}",
                        reason="reconfigure_successful",
                    )
        return self.async_show_form(step_id="reconfigure",
                                    data_schema=_schema(user_input or dict(entry.data)), errors=errors)


class EufyNvrOptionsFlow(OptionsFlow):
    """Tune polling and thumbnail caching without changing camera identity."""
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
                vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)),
            vol.Required(CONF_SNAPSHOT_INTERVAL, default=options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)):
                vol.All(int, vol.Range(min=MIN_SNAPSHOT_INTERVAL, max=MAX_SNAPSHOT_INTERVAL)),
        }))
