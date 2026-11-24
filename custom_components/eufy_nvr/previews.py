"""Low-impact camera previews for the single-session Eufy NVR.

The NVR serves one live session at a time, so fetching a thumbnail the normal
way (go2rtc frame.jpeg on an idle stream) opens an NVR call and competes with
live viewing. This module avoids that:

* Passive capture: while a camera is already being watched (go2rtc reports a
  consumer on its stream), grab one frame from the running stream. The producer
  is already connected, so no extra NVR session is opened.
* Idle refresh: every REFRESH_INTERVAL, refresh previews that are
  older than that, one camera at a time, only when no camera has been watched
  for REFRESH_IDLE_REQUIRED seconds. A capture is cancelled the moment any
  other camera gets a viewer.

Previews are kept in memory and on disk so they survive restarts. Camera
entities only ever read the stored image; they never trigger a capture.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import logging
import os
import re
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .go2rtc_api import STREAM_PREFIX, Go2RtcClient, Go2RtcError, stream_summary

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 3.0
PASSIVE_MIN_VIEW = 8.0  # stream watched this long (past the grey start) first
PASSIVE_MIN_GAP = 120.0  # at most one good passive grab per camera every 2 min
PASSIVE_RETRY = 6.0  # retry this soon after a grey/failed passive grab
PASSIVE_TIMEOUT = 5.0

REFRESH_INTERVAL = 30.0 * 60.0  # refresh previews older than 30 minutes
REFRESH_IDLE_REQUIRED = 180.0  # no camera watched for 3 minutes first
REFRESH_TIMEOUT = 60.0
REFRESH_WARM = 6.0  # hold the stream open this long before grabbing
REFRESH_GREY_RETRIES = 2  # extra grabs (3 s apart) if the frame is still grey
REFRESH_GAP = 15.0  # pause between cameras so the NVR settles
REFRESH_CHECK = 60.0
STARTUP_DELAY = 300.0

# Every refresh holds the NVR's single live session, so live views queue behind
# it. Extra lenses (<camera>_lens<N>) refresh at half the rate and multi-view
# groups (eufy_group_<name>) are never refreshed in the background; watching
# them still updates their preview passively.
LENS_REFRESH_FACTOR = 2.0


def refresh_interval(stream: str) -> float | None:
    """Background refresh interval for a stream, or None to never refresh."""
    if stream.startswith("eufy_group_"):
        return None
    if re.search(r"_lens\d+$", stream):
        return REFRESH_INTERVAL * LENS_REFRESH_FACTOR
    return REFRESH_INTERVAL


GREY_STDDEV = 6.0  # luminance std-dev below this = flat grey placeholder frame


def _looks_grey(data: bytes) -> bool:
    """True for the flat grey frames sent before the first real keyframe."""
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(data)) as img:
            small = img.convert("L").resize((64, 36))
        return ImageStat.Stat(small).stddev[0] < GREY_STDDEV
    except Exception:  # noqa: BLE001 - never block a preview on the check
        return False


class PreviewManager:
    """Hold the latest preview per camera and keep it fresh without contention."""

    def __init__(self, hass: HomeAssistant, client: Go2RtcClient, entry_id: str) -> None:
        self._hass = hass
        self._client = client
        self._dir = hass.config.path(".storage", f"eufy_nvr_previews_{entry_id}")
        self._images: dict[str, bytes] = {}
        self._stamps: dict[str, float] = {}  # wall-clock time of each image
        self._watch_since: dict[str, float] = {}
        self._last_grab: dict[str, float] = {}
        self._streams: set[str] = set()
        self._viewers: set[str] = set()
        self._last_viewed = 0.0  # monotonic time any camera was last watched
        self._attempts: dict[str, float] = {}  # wall-clock time of last refresh try
        self._capturing: str | None = None  # stream an idle refresh is capturing
        self._refresh_capture: asyncio.Task | None = None
        self._rtsp_url = None  # set in async_start from the config entry

    # ----- read side (camera entities) -------------------------------------
    def has_preview(self, stream: str) -> bool:
        """True when a real (non-grey) preview is stored for this camera."""
        return stream in self._images

    def get(self, stream: str) -> bytes | None:
        """Return the stored preview, never triggering a capture."""
        return self._images.get(stream)

    def updated_iso(self, stream: str) -> str | None:
        """Return when the preview was captured, as an ISO timestamp."""
        stamp = self._stamps.get(stream)
        if stamp is None:
            return None
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")

    # ----- lifecycle --------------------------------------------------------
    def async_start(self, entry: ConfigEntry) -> None:
        """Start background tasks; they are cancelled when the entry unloads."""
        from .const import CONF_HOST, CONF_PASSWORD, CONF_RTSP_PORT, CONF_USERNAME
        from .go2rtc_api import rtsp_url

        data = dict(entry.data)
        self._rtsp_url = lambda stream: rtsp_url(
            data[CONF_HOST], data[CONF_RTSP_PORT], stream,
            data[CONF_USERNAME], data[CONF_PASSWORD],
        )
        entry.async_create_background_task(
            self._hass, self._async_run(), "eufy_nvr previews"
        )

    async def async_load(self) -> None:
        """Load stored previews; called during setup, before snapshot priming."""
        await self._hass.async_add_executor_job(self._load)

    async def _async_run(self) -> None:
        tasks = [
            asyncio.create_task(self._async_watch()),
            asyncio.create_task(self._async_refresh_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            if self._refresh_capture is not None:
                self._refresh_capture.cancel()

    # ----- storage ----------------------------------------------------------
    def _load(self) -> None:
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            return
        for name in names:
            if not (name.startswith(STREAM_PREFIX) and name.endswith(".jpg")):
                continue
            path = os.path.join(self._dir, name)
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
                stamp = os.path.getmtime(path)
            except OSError:
                continue
            if data.startswith(b"\xff\xd8") and not _looks_grey(data):
                stream = name[: -len(".jpg")]
                self._images[stream] = data
                self._stamps[stream] = stamp
        _LOGGER.debug("Loaded %d stored camera previews", len(self._images))

    def _save(self, stream: str, data: bytes) -> None:
        os.makedirs(self._dir, exist_ok=True)
        path = os.path.join(self._dir, f"{stream}.jpg")
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

    async def _async_fetch_frame(self, stream: str, timeout: float) -> bytes:
        """Fetch a fresh JPEG from the running producer (bypassing go2rtc's cache)."""
        return await self._client.async_get_frame(stream, timeout=timeout, cache=False)

    async def _async_is_grey(self, frame: bytes) -> bool:
        return await self._hass.async_add_executor_job(_looks_grey, frame)

    async def _async_warm_frame(self, stream: str, skip: float) -> bytes:
        """Read the camera over RTSP, skip the grey start-up, keep one frame.

        A cold go2rtc frame.jpeg returns the NVR's flat grey start-up frame,
        and the add-on's go2rtc has no MP4/MJPEG endpoint to hold the producer
        open. One ffmpeg RTSP read is a single NVR session: decode
        and discard ``skip`` seconds, write one JPEG, exit.
        """
        from asyncio.subprocess import DEVNULL, PIPE

        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        if self._rtsp_url is None:
            raise Go2RtcError("RTSP details unavailable")
        binary = get_ffmpeg_manager(self._hass).binary
        proc = await asyncio.create_subprocess_exec(
            binary, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rtsp_transport", "tcp", "-i", self._rtsp_url(stream),
            "-ss", f"{skip:.1f}", "-frames:v", "1", "-an",
            "-c:v", "mjpeg", "-q:v", "4", "-f", "image2", "pipe:1",
            stdout=PIPE, stderr=DEVNULL,
        )
        try:
            out, _ = await proc.communicate()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if not out or not out.startswith(b"\xff\xd8"):
            raise Go2RtcError("ffmpeg produced no frame")
        return out

    async def _async_refresh_frame(self, stream: str) -> bytes:
        frame = await self._async_warm_frame(stream, REFRESH_WARM)
        for attempt in range(REFRESH_GREY_RETRIES):
            if not await self._async_is_grey(frame):
                break
            frame = await self._async_warm_frame(stream, REFRESH_WARM + 4.0 * (attempt + 1))
        return frame

    async def _async_grab(self, stream: str, timeout: float, reason: str) -> bool:
        try:
            async with asyncio.timeout(timeout):
                if reason == "refresh":
                    frame = await self._async_refresh_frame(stream)
                else:
                    frame = await self._async_fetch_frame(stream, timeout)
        except (Go2RtcError, TimeoutError) as err:
            _LOGGER.debug("Preview %s for %s failed (%s)", reason, stream, type(err).__name__)
            return False
        except Exception as err:  # noqa: BLE001 - e.g. ffmpeg missing or a process error
            _LOGGER.debug("Preview %s for %s failed (%s)", reason, stream, type(err).__name__)
            return False
        if await self._async_is_grey(frame):
            _LOGGER.debug("Preview %s for %s was a grey frame; kept the old one", reason, stream)
            return False
        self._images[stream] = frame
        self._stamps[stream] = time.time()
        try:
            await self._hass.async_add_executor_job(self._save, stream, frame)
        except OSError:
            _LOGGER.debug("Could not store preview for %s", stream)
        _LOGGER.debug("Preview %s captured for %s", reason, stream)
        return True

    # ----- passive capture --------------------------------------------------
    async def _async_watch(self) -> None:
        while True:
            try:
                streams = await self._client.async_get_streams()
            except Go2RtcError:
                await asyncio.sleep(POLL_INTERVAL * 5)
                continue
            now = time.monotonic()
            self._streams = set(streams)
            viewers: set[str] = set()
            for name, info in streams.items():
                if stream_summary(info)["consumers"] > 0:
                    viewers.add(name)
                    self._watch_since.setdefault(name, now)
                else:
                    self._watch_since.pop(name, None)

            # Our own idle-refresh capture shows up as a consumer; ignore it.
            others = viewers - ({self._capturing} if self._capturing else set())
            self._viewers = others
            if others:
                self._last_viewed = now
                if self._refresh_capture is not None and not self._refresh_capture.done():
                    _LOGGER.debug("Viewer on %s; cancelling idle preview refresh", sorted(others))
                    self._refresh_capture.cancel()

            for name in others:
                if (
                    now - self._watch_since.get(name, now) >= PASSIVE_MIN_VIEW
                    and now - self._last_grab.get(name, -1e9) >= PASSIVE_MIN_GAP
                ):
                    self._last_grab[name] = now
                    self._hass.async_create_background_task(
                        self._async_passive(name, now),
                        f"eufy_nvr passive preview {name}",
                    )
            await asyncio.sleep(POLL_INTERVAL)

    async def _async_passive(self, stream: str, started: float) -> None:
        if not await self._async_grab(stream, PASSIVE_TIMEOUT, "passive"):
            # Grey or failed: allow another try a few seconds later while the
            # viewer is still watching, instead of waiting the full gap.
            self._last_grab[stream] = started - PASSIVE_MIN_GAP + PASSIVE_RETRY

    # ----- idle refresh -----------------------------------------------------
    async def _async_refresh_loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            await self._async_refresh_cycle()
            await asyncio.sleep(REFRESH_CHECK)

    def _idle(self) -> bool:
        return (
            not self._viewers
            and time.monotonic() - self._last_viewed >= REFRESH_IDLE_REQUIRED
        )

    async def _async_refresh_cycle(self) -> None:
        for stream in sorted(self._streams):
            interval = refresh_interval(stream)
            if interval is None:
                continue
            now_wall = time.time()
            if now_wall - self._stamps.get(stream, 0.0) < interval:
                continue
            # One attempt per camera per interval, success or not, so a failing
            # NVR/cloud session is never hammered.
            if now_wall - self._attempts.get(stream, 0.0) < interval:
                continue
            if not self._idle():
                return
            self._attempts[stream] = now_wall
            self._capturing = stream
            self._refresh_capture = asyncio.create_task(
                self._async_grab(stream, REFRESH_TIMEOUT, "refresh")
            )
            try:
                await asyncio.wait({self._refresh_capture})
            finally:
                cancelled = self._refresh_capture.cancelled()
                self._refresh_capture = None
                self._capturing = None
            if cancelled:
                return  # a viewer appeared; try again once things are idle
            if stream not in self._stamps or now_wall > self._stamps[stream]:
                return  # capture failed; stop this cycle and back off
            await asyncio.sleep(REFRESH_GAP)
