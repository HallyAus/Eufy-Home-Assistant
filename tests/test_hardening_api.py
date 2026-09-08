"""Real client/pure endpoint tests with a fake HTTP response boundary."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

SPEC = importlib.util.spec_from_file_location("hardening_go2rtc_api", Path(__file__).resolve().parents[1] / "custom_components/eufy_nvr/go2rtc_api.py")
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


@pytest.mark.parametrize("value", [None, 123, "", "host/path", "host:1985", "https://host", "http://user:pass@host", "http://host:1985", "http://host/stream", "http://ho\nst", "bad\\host", "-bad.local", "bad..local", "a"*64 + ".local", "@host", "bad host"])
def test_invalid_hosts(value):
    with pytest.raises(ValueError):
        api.normalize_host(value)


@pytest.mark.parametrize("value,expected", [(" HA.Example.Local. ", "ha.example.local"), ("[FD00:0:0:0:0:0:0:10]", "fd00::10"), ("http://192.168.0.9/", "192.168.0.9"), ("büro.local", "xn--bro-hoa.local")])
def test_canonical_hosts(value, expected):
    assert api.normalize_host(value) == expected


def test_ipv6_management_and_rtsp_urls():
    assert api.api_base_url("FD00::10", 1985) == "http://[fd00::10]:1985"
    assert api.rtsp_url(
        "fd00::10", 8556, "eufy_front gate", "eufy", "long pass:@/word!"
    ) == "rtsp://eufy:long%20pass%3A%40%2Fword%21@[fd00::10]:8556/eufy_front%20gate"


class Response:
    def __init__(self, payload=None, error=None, body=None, content_type="application/json"):
        self.payload, self.error, self.body = payload, error, body
        self.headers = {"Content-Type": content_type}
        self.content_length = len(body) if body is not None else None
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def raise_for_status(self):
        if isinstance(self.error, aiohttp.ClientResponseError):
            raise self.error
    async def json(self, **kwargs):
        if self.error:
            raise self.error
        return self.payload
    async def read(self):
        if self.error:
            raise self.error
        return self.body


def client(response):
    return api.Go2RtcClient(
        SimpleNamespace(get=lambda *args, **kwargs: response),
        "bridge.local", 1985, 10, "eufy", "0123456789abcdef"
    )


class CapturingSession:
    def __init__(self, response):
        self.response = response
        self.request = None

    def get(self, *args, **kwargs):
        self.request = (args, kwargs)
        return self.response


@pytest.mark.asyncio
async def test_client_filters_streams_counts_all_and_keeps_summary_private():
    instance = client(Response({"eufy_garage": {"producers": [{"url": "PRIVATE"}], "consumers": [{}]}, "other": {}}))
    streams = await instance.async_get_streams()
    assert set(streams) == {"eufy_garage"}
    assert instance.total_stream_count == 2
    assert "PRIVATE" not in repr(api.summarize_streams(streams))


@pytest.mark.asyncio
async def test_client_sends_basic_auth_without_putting_credentials_in_url():
    session = CapturingSession(Response({"eufy_garage": {}}))
    instance = api.Go2RtcClient(
        session, "bridge.local", 1985, 10, "eufy", "0123456789abcdef"
    )

    await instance.async_get_streams()

    args, kwargs = session.request
    assert args == ("http://bridge.local:1985/api/streams",)
    assert kwargs["headers"]["Authorization"].startswith("Basic ")
    assert "0123456789abcdef" not in args[0]


@pytest.mark.asyncio
async def test_client_fetches_one_cached_full_size_jpeg():
    jpeg = b"\xff\xd8image\xff\xd9"
    session = CapturingSession(Response(body=jpeg, content_type="image/jpeg"))
    instance = api.Go2RtcClient(
        session, "bridge.local", 1985, 10, "eufy", "0123456789abcdef"
    )

    assert await instance.async_get_frame("eufy_front_gate") == jpeg
    args, kwargs = session.request
    assert args == ("http://bridge.local:1985/api/frame.jpeg",)
    assert kwargs["params"] == {"src": "eufy_front_gate", "cache": "30s"}
    assert "width" not in kwargs["params"]
    assert kwargs["headers"]["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,content_type",
    [(b"", "image/jpeg"), (b"not jpeg", "image/jpeg"), (b"\xff\xd8x\xff\xd9", "text/plain")],
)
async def test_client_rejects_invalid_snapshot(body, content_type):
    session = CapturingSession(Response(body=body, content_type=content_type))
    instance = api.Go2RtcClient(
        session, "bridge.local", 1985, 10, "eufy", "0123456789abcdef"
    )
    with pytest.raises(api.Go2RtcPayloadError):
        await instance.async_get_frame("eufy_front_gate")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientConnectionError("offline"), aiohttp.ClientResponseError(None, (), status=401)])
async def test_network_errors_map_to_connection_error(error):
    with pytest.raises(api.Go2RtcConnectionError):
        await client(Response(error=error)).async_get_streams()


@pytest.mark.asyncio
async def test_bad_json_maps_to_payload_error():
    with pytest.raises(api.Go2RtcPayloadError):
        await client(Response(error=ValueError("bad JSON"))).async_get_streams()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"streams": []}])
async def test_bad_shape_maps_to_payload_error(payload):
    with pytest.raises(api.Go2RtcPayloadError):
        await client(Response(payload)).async_get_streams()


@pytest.mark.asyncio
async def test_client_preserves_cancellation():
    with pytest.raises(asyncio.CancelledError):
        await client(Response(error=asyncio.CancelledError())).async_get_streams()


@pytest.mark.parametrize("value", ["fe80::1%Eth0", "http://[fe80::1%25Eth0]/"])
def test_scoped_ipv6_is_uri_escaped_without_changing_interface_case(value):
    assert api.api_base_url(value, 1985) == "http://[fe80::1%25Eth0]:1985"
