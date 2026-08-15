"""Small client and pure helpers for the local go2rtc API."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import quote, urlsplit

API_STREAMS_PATH = "/api/streams"
STREAM_PREFIX = "eufy_"


class Go2RtcError(Exception):
    """Base error raised by the go2rtc client."""


class Go2RtcConnectionError(Go2RtcError):
    """Raised when the go2rtc endpoint cannot be reached."""


class Go2RtcPayloadError(Go2RtcError):
    """Raised when go2rtc returns an unexpected response."""


def validate_port(port: int) -> int:
    """Return a valid TCP port or raise ValueError."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return port


def normalize_host(value: str) -> str:
    """Normalize an IP/hostname or a bare HTTP URL to a host value."""
    raw = value.strip()
    if not raw:
        raise ValueError("host is required")

    if "://" in raw:
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "enter a host or a bare http://host URL without a port or path"
            )
        raw = parsed.hostname
    elif raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    elif any(character in raw for character in "/?#@"):
        raise ValueError("host cannot contain a path, query, or credentials")
    elif raw.count(":") == 1:
        raise ValueError("enter the API port in the separate port field")

    if ":" in raw:
        try:
            ipaddress.IPv6Address(raw)
        except ValueError as err:
            raise ValueError("invalid IPv6 address") from err
    elif not raw or any(character.isspace() for character in raw):
        raise ValueError("invalid host")
    return raw.lower()


def _url_host(host: str) -> str:
    normalized = normalize_host(host)
    return f"[{normalized}]" if ":" in normalized else normalized


def api_url(host: str, port: int) -> str:
    """Build the go2rtc streams API URL."""
    return f"http://{_url_host(host)}:{validate_port(port)}{API_STREAMS_PATH}"


def rtsp_url(host: str, port: int, stream: str) -> str:
    """Build a safe RTSP URL for a discovered stream name."""
    return f"rtsp://{_url_host(host)}:{validate_port(port)}/{quote(stream, safe='_-')}"


def extract_streams(payload: Any) -> dict[str, dict[str, Any]]:
    """Return only eufy streams from either supported go2rtc response shape."""
    if isinstance(payload, dict) and "streams" in payload:
        payload = payload["streams"]
    if not isinstance(payload, dict):
        raise ValueError("go2rtc streams response is not an object")
    return {
        name: info if isinstance(info, dict) else {}
        for name, info in payload.items()
        if isinstance(name, str) and name.startswith(STREAM_PREFIX)
    }


def stream_summary(info: dict[str, Any]) -> dict[str, int | bool]:
    """Return non-sensitive producer/consumer counts for an API stream object."""
    producers = info.get("producers")
    consumers = info.get("consumers")
    producer_count = len(producers) if isinstance(producers, list) else 0
    consumer_count = len(consumers) if isinstance(consumers, list) else 0
    return {
        "producers": producer_count,
        "consumers": consumer_count,
        "streaming": consumer_count > 0,
    }


def summarize_streams(
    streams: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int | bool]]:
    """Return a stable, URL-free summary suitable for diagnostics."""
    return {name: stream_summary(streams[name]) for name in sorted(streams)}


class Go2RtcClient:
    """Fetch camera stream metadata from one local go2rtc instance."""

    def __init__(self, session: Any, host: str, api_port: int, timeout: int) -> None:
        self.host = normalize_host(host)
        self.api_port = validate_port(api_port)
        self.url = api_url(self.host, self.api_port)
        self._session = session
        self._timeout = timeout

    async def async_get_streams(self) -> dict[str, dict[str, Any]]:
        """Fetch and parse the configured Eufy streams."""
        from aiohttp import ClientError, ClientResponseError

        try:
            async with self._session.get(self.url, timeout=self._timeout) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except ClientResponseError as err:
            raise Go2RtcConnectionError(
                f"go2rtc returned HTTP {err.status} from {self.url}"
            ) from err
        except (ClientError, TimeoutError) as err:
            raise Go2RtcConnectionError(
                f"cannot reach go2rtc at {self.url}: {err}"
            ) from err
        except ValueError as err:
            raise Go2RtcPayloadError(
                f"go2rtc returned invalid JSON from {self.url}"
            ) from err

        try:
            return extract_streams(payload)
        except ValueError as err:
            raise Go2RtcPayloadError(
                f"unexpected go2rtc response from {self.url}"
            ) from err
