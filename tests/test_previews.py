"""Offline tests for stored camera previews (passive capture + idle refresh)."""
import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
previews = pytest.importorskip("custom_components.eufy_nvr.previews")
api = pytest.importorskip("custom_components.eufy_nvr.go2rtc_api")
Image = pytest.importorskip("PIL.Image")


def jpeg(noisy: bool) -> bytes:
    image = Image.new("L", (64, 36), 128)
    if noisy:
        image.putdata([(x * 37 + y * 91) % 256 for y in range(36) for x in range(64)])
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG")
    return out.getvalue()


def test_flat_grey_startup_frames_are_rejected():
    assert previews._looks_grey(jpeg(noisy=False)) is True
    assert previews._looks_grey(jpeg(noisy=True)) is False
    assert previews._looks_grey(b"not a jpeg") is False


def manager(tmp_path):
    hass = SimpleNamespace(config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))))
    return previews.PreviewManager(hass, client=None, entry_id="test")


def test_stored_previews_load_and_grey_files_are_dropped(tmp_path):
    store = tmp_path / ".storage" / "eufy_nvr_previews_test"
    store.mkdir(parents=True)
    (store / "eufy_front.jpg").write_bytes(jpeg(noisy=True))
    (store / "eufy_back.jpg").write_bytes(jpeg(noisy=False))
    (store / "other.jpg").write_bytes(jpeg(noisy=True))
    mgr = manager(tmp_path)
    mgr._load()
    assert mgr.has_preview("eufy_front")
    assert not mgr.has_preview("eufy_back")
    assert not mgr.has_preview("other")
    assert mgr.get("eufy_front").startswith(b"\xff\xd8")
    assert mgr.updated_iso("eufy_front").endswith("+00:00")
    assert mgr.updated_iso("eufy_back") is None


def test_saved_preview_round_trips(tmp_path):
    mgr = manager(tmp_path)
    mgr._save("eufy_front", jpeg(noisy=True))
    again = manager(tmp_path)
    again._load()
    assert again.has_preview("eufy_front")


class _Response:
    headers = {"Content-Type": "image/jpeg"}
    content_length = None

    def __init__(self, body):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def read(self):
        return self._body


class _Session:
    def __init__(self, body):
        self.body, self.calls = body, []

    def get(self, url, **kwargs):
        self.calls.append(kwargs["params"])
        return _Response(self.body)


@pytest.mark.parametrize("cache, expected", [(True, {"src": "eufy_front", "cache": "30s"}),
                                             (False, {"src": "eufy_front"})])
def test_frame_cache_can_be_bypassed_for_watched_streams(cache, expected):
    frame = jpeg(noisy=True)
    client = api.Go2RtcClient.__new__(api.Go2RtcClient)
    client._session, client._headers, client._timeout = _Session(frame), {}, 10
    client.frame_url = "http://go2rtc.test/api/frame.jpeg"
    assert asyncio.run(client.async_get_frame("eufy_front", cache=cache)) == frame
    assert client._session.calls == [expected]


def test_priming_skips_cameras_that_already_have_a_preview():
    coordinator = (ROOT / "custom_components/eufy_nvr/coordinator.py").read_text()
    setup = (ROOT / "custom_components/eufy_nvr/__init__.py").read_text()
    camera = (ROOT / "custom_components/eufy_nvr/camera.py").read_text()
    assert "self.previews.has_preview(s)" in coordinator
    assert setup.index("previews.async_load()") < setup.index("await coordinator.async_prime_frames()")
    assert "coordinator.previews.async_start(entry)" in setup
    assert camera.index("self.coordinator.previews.get(self._stream)") < camera.index(
        "await self.coordinator.async_get_frame(self._stream)"
    )
