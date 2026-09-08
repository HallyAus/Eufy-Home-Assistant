# Changelog

## 0.7.8

- Send `closeLive` after discovery sessions as well as video sessions. The NVR counts the discovery
  WebRTC control connection against the same one-session limit, so merely closing the peer connection
  could make every later producer receive signaling status `486` despite no active local consumer.
- Retry only cameras that still lack a primed snapshot, using bounded exponential backoff. A partial
  startup cycle no longer waits 30 minutes before trying the missing camera fallbacks again or repeats
  successful camera work during the retry window.

## 0.7.7

- Send Eufy's `closeLive` command (`cmd 1004`) over the active SCTP/WebRTC control channel before
  stopping a producer. Previous releases killed the process tree without telling the appliance to
  retire the live session, which could leave every subsequent camera request returning status `486`.
- Make supervised shutdown graceful-first: interrupt only the Python protocol engine while keeping
  its framing oracle and media child alive long enough to transmit `closeLive`, then force-clean the
  complete process group if it does not exit within the bounded grace period.

## 0.7.6

- Reduce go2rtc's idle producer kill delay from eight seconds to one. The adaptive warmer already
  owns intentional reuse, so the old delay only consumed almost all of Home Assistant's nine-second
  snapshot deadline when handing the NVR's single session to another camera.
- Keep the cross-process Eufy session gate locked for a separate one-second appliance teardown grace
  period. This prevents the next producer from racing the NVR's internal WebRTC cleanup and receiving
  signaling status `486`.
- Add credential-free adaptive-controller startup/API diagnostics for live deployment verification.

## 0.7.5

- Prime one snapshot per camera sequentially after Home Assistant loads the integration, then refresh
  the fallback set only every 30 minutes. A shared one-hour stale cache lets a first-time multi-camera
  dashboard return known images while the NVR services cold producers one at a time, instead of timing
  out later camera requests.
- Share snapshot coalescing across all camera entities and retain per-camera eviction, preserving the
  NVR's one-session invariant without coupling unrelated camera lifecycles.

## 0.7.4

- Serialize access to the NVR's single live WebRTC session across every camera producer and classify
  signaling status `486` immediately, eliminating the 25-second timeout/retry cascades caused by
  concurrent Home Assistant dashboard requests.
- Replace the permanent per-camera `keep_warm` processes with an Eufy-specific adaptive session
  controller. It holds only the most recently viewed camera for 30 seconds by default, hands the
  session to a newly requested camera, and carries credentials only in authenticated HTTP headers.
- Serve snapshots through go2rtc's authenticated, coalescing JPEG endpoint instead of launching a
  second Home Assistant FFmpeg process. Cache a fresh frame for 30 seconds and use a bounded stale
  frame during transient camera handoffs so dashboards reopen immediately.
- Start cloud signing and the SCTP framing runtime in parallel, disable debug-frame disk writes unless
  explicitly requested, and reduce per-frame hot-path logging while retaining five-second progress
  markers for stall detection.
- Change the container health check to an authentication-independent TCP probe so enabling required
  go2rtc credentials cannot make a healthy add-on appear unhealthy.

## 0.7.3

- Make the Dockerfile self-contained with a pinned multi-architecture Debian base image. Current Home
  Assistant Supervisor versions reject digest references in the deprecated `build.yaml` schema and
  otherwise fall back to Alpine, where the Debian `apt-get` build fails.
- Remove the deprecated build file and make CI exercise the same Dockerfile default used by Supervisor.

## 0.7.2

- Implement the current official web mailbox/device-verification flow for `fa_info.step=26052`: request
  the email code through the push service and repeat the encrypted passport login with the provisional
  token and six-digit `verification_code` (GitHub issue #13).
- Bind every auth cache to a non-plaintext account/region/country fingerprint. A failed login now refuses
  legacy, malformed, or different-account caches instead of feeding a previous user's token to the NVR.
- Require local go2rtc credentials and apply them to both the management API/UI and RTSP server. The HACS
  integration supplies Basic auth, emits credentialed RTSP URLs internally, and omits the secret from logs,
  state attributes, configuration URLs, and diagnostics.
- Reap the complete engine process group on cancellation or leader exit, add a post-SDP connection timeout,
  trust the oracle's decoded `-104` marker instead of a byte-length heuristic, and fail producers when
  ffmpeg exits or its pipe breaks.
- Pin Python runtime dependencies, CI tools, GitHub Actions, the Home Assistant base images, the bridge
  source commit, and go2rtc version. Verify
  SHA-256 for go2rtc and all downloaded Eufy worker assets; remove the mutable unverified FFmpeg download.
- Run CI against the real runtime dependency set on Python 3.12, 3.13, and 3.14, including import smoke tests.
- Upgrade `idna` to 3.15, the first patched release for CVE-2026-45409.

## 0.7.1

- Add `eufy_run.py`, a process supervisor around the reversed WebRTC engine. It opens fresh signaling
  sessions when scall/TURN stalls without an SDP offer, retries a producer that never reaches its first
  video frame, and restarts a stream that stops producing frames while go2rtc itself remains healthy.
- Own the complete oracle/ffmpeg/engine process group so failed or stalled sessions are terminated as a
  unit before retry, preventing abandoned camera producers from accumulating.
- Route generated go2rtc producers and add-on discovery through the supervisor while retaining go2rtc's
  60-second startup allowance and bounded kill timeout.
- Detect the fixed 148-byte non-JSON command-rejection shape reported by shared/member accounts during
  discovery and emit an explicit owner/admin-account diagnostic instead of retrying a permission failure.
- Add offline regression coverage for signaling state, video progress/stall markers, authorization
  classification, and supervised producer generation.

## 0.7.0

- Harden discovery/config generation with persistent collision-safe stream identities, strict manifest and
  registry validation, atomic writes, and valid empty configuration when all cameras are offline.
- Harden the Home Assistant integration with safer host/port validation, IPv6-safe URLs, duplicate endpoint
  detection, corrected reconfiguration identity, removal of the redundant double-reload listener, and
  coalesced/timeout-bounded snapshots.
- Upgrade go2rtc from `v1.9.9` to `v1.9.14` and configure a 60-second exec `starttimeout` plus bounded process
  termination for slow Eufy WebRTC cold starts. This directly addresses the premature `exec: timeout` path
  reported in issue #5, while keeping the issue open until physical NVR validation confirms the full live path.
- Document that the configured eufy account must own/administer the NVR; shared/member accounts can authenticate
  but may receive fixed `-104` command rejections (issue #8).
- Keep issue #6 open: `scall/turn status 100` without a later status 200 / SDP offer is a separate signalling
  failure and is not safe to claim fixed without device-side validation.
- Make development add-on builds use an existing source ref rather than depending on a release tag before it exists.

## 0.6.9

- Update eufy's required SCTP framing runtime from the removed `0_0_2` CDN assets to the current
  `0_0_4` files used by the web client. Build-time and startup checks now require the matching JS/WASM
  assets so an image cannot ship with a stale framing runtime.

## 0.6.8

- Persist the discovered camera manifest under `/data` (new `EUFY_CAMERAS` override, mirroring
  `EUFY_AUTH`) so `eufy_stream.py --discover` and `gen_go2rtc.py` always agree on its location and
  the list survives a container restart/rebuild.
- Make `eufy_stream.py --discover` exit non-zero when it never received a camera list, instead of
  reporting "Discovery OK" and letting `gen_go2rtc.py` crash with `FileNotFoundError` on the missing
  `cameras.json`. Write the manifest atomically.
- When discovery keeps failing, regenerate `go2rtc.yaml` from the last persisted camera list before
  falling back to the previous `go2rtc.yaml`.

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
