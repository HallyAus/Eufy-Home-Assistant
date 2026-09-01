# Changelog

## 0.6.8

- Update eufy's required SCTP framing runtime from the removed `0_0_2` CDN assets to the current
  `0_0_4` files used by the web client. `security.eufy.com/plugin/libsctp_0_0_2.*` (and the two
  worker shims) now return 404, so every image build failed at `fetch_deps.js`.

## 0.6.7

- Refresh the eufy auth session periodically even when `keep_warm` is disabled, and replace the auth
  file atomically, so on-demand camera streams continue working after the original token expires.
- Stop logging the `ws/sign` response body or token prefix, and report expired/rejected sessions with
  an actionable error instead of an internal `KeyError`.

## 0.6.6

- Start the video stream shortly after the NVR acknowledges `openLive` instead of waiting a fixed
  second, leaving enough time for Home Assistant to decode the first JPEG within its request limit.

## 0.6.5

- Skip ffmpeg's unnecessary input analysis for the known raw HEVC camera feed. This removes several
  seconds from a cold stream launch so Home Assistant can receive a still before its fixed 10-second
  camera-image timeout.

## 0.6.4

- When `homeassistant.local` is unreachable from the Home Assistant Core container, retry the
  configured internal URL's LAN host automatically. This keeps the friendly default while avoiding
  mDNS failures inside HAOS containers.

## 0.6.3

- Preserve line boundaries when parsing discovered stream names for `keep_warm`, so each camera gets
  its own warmer instead of all stream slugs being concatenated into one invalid RTSP path.

## 0.6.2

- Update eufy's required SCTP framing runtime from removed `0_0_1` CDN assets to the current
  `0_0_2` files used by the web client.
- Fail the add-on image build when those runtime-critical files cannot be downloaded, and run the
  offline SCTP round-trip self-test during the build so a broken image cannot be installed again.

## 0.6.1

- Move the add-on's go2rtc to dedicated host-network ports: API `1985`, RTSP `8556`, and WebRTC
  `8557`. Home Assistant's built-in go2rtc already owns API `1984` on HAOS, so v0.6.0 could connect
  the companion integration to the wrong server and report that no `eufy_*` cameras existed.
- Detect a reachable go2rtc containing only non-Eufy streams and report it as the wrong instance,
  with the dedicated Eufy API port in the corrective message.

## 0.6.0

- Fix discovery and streaming for EU and IE accounts by routing `ws/sign` and the signaling WebSocket
  through the selected region's smart-service host. Headless and browser-captured auth files now persist
  the signaling region so manual bridge commands work without re-exporting `EUFY_REGION`.
- Rebuild the companion integration around a shared go2rtc client, with normalized IPv4/IPv6 endpoints,
  a distinct "bridge reachable but no Eufy streams" setup error, live producer/consumer attributes, and
  privacy-safe downloadable diagnostics.
- Persist `auth.json`, camera discovery, and generated go2rtc configuration under the add-on `/data`
  directory. A transient login or discovery outage can reuse the last working local configuration.
- Start the add-on automatically after a host reboot and remove unused Supervisor/Home Assistant API
  permissions.
- Pin the bridge source used by the add-on image to the matching `v0.6.0` release instead of mutable `main`.

## 0.5.2

- **More login regions + a separate account country.** The `region` option now offers
  US | EU | IE (the eufy *server* your account lives on), and a new optional `country` field
  lets accounts registered outside those regions — e.g. AU — authenticate against the nearest
  server while still identifying their real country. Leave `country` blank to use the region.
- Removed a hard-coded device-serial fallback from the bridge; it now relies on auto-discovery
  or the optional `station_sn` override.

## 0.5.1

- **Offline cameras no longer create dead "no feed" entities.** Discovery used to publish an
  offline channel as a normal stream (the "offline" note was an inert comment), so the HA
  integration made a green entity that 404'd on open. `gen_go2rtc.py` now skips status-0
  cameras entirely; they're re-added automatically the next time discovery sees them online.
- **Faster live-view open.** Shortened the transcode GOP from 25 to 12 frames. The feed runs
  below 25 fps, so a keyframe now arrives every ~0.5-0.8s instead of ~1.5-2s, cutting the
  per-open keyframe wait (on top of `keep_warm`, which removes the producer cold start).

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
