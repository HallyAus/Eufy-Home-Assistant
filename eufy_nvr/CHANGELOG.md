# Changelog

## 0.5.0

- **Live view now actually plays.** The bridge transcodes the NVR's HEVC to **H.264**
  (libx264 ultrafast/zerolatency, ~1s GOP) before publishing, so Home Assistant's browser
  live view renders it. Previously the stream was raw H.265 (`-c:v copy`), which most
  browsers can't play live — you'd get the snapshot thumbnail but "enlarge" never loaded.
  This is the headline fix and is always on.
- **Optional low-latency "keep-warm"** (`keep_warm`, default **off**). Holds each online
  camera warm so opening live view is near-instant instead of waiting 5-13s for the WebRTC
  cold start. It's **off by default** because it runs one continuous H.264 software encode
  per online camera — only enable it on a host with CPU headroom (3-4 always-on encodes can
  saturate a low-power Pi). The NVR itself streams all channels concurrently, so the NVR side
  is fine; the cost is host CPU. Pair with `video_copy` for a cheap always-on warm.
- **`video_copy` option** (default off). Publishes raw H.265 instead of transcoding — lower
  CPU, but the live view is thumbnail-only. Replaces the undocumented `EUFY_VIDEO_COPY` env.
- **Periodic re-login** (`token_refresh_hours`, default 6) refreshes `auth.json` so a warm
  stream that drops can reconnect past the ~1-day eufy session-token lifetime.

## 0.4.1

- Fix: headless discovery (`--discover`) now exits when it completes, so the add-on
  reliably moves on to start go2rtc instead of hanging (previously it could loop on
  `STATS … video=0` and never start the streams).

## 0.4.0

- Headless **email/password login** — no more one-time token paste. On start the add-on
  logs into the eufy passport, derives your NVR's `station_sn`, and writes `auth.json`.
- Auto-discovers the NVR + cameras (cmd 9100) and serves each channel as RTSP/WebRTC via
  a bundled, pinned go2rtc.
- Add-on relocated to the repo root and `webui`/`watchdog` use the `[PORT:1984]`
  placeholder so the Supervisor store lists it correctly.
