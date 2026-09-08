# Eufy NVR Local — Home Assistant add-on (experimental)

Runs the whole eufy WebRTC -> RTSP engine **on your Home Assistant host**, so you don't need a
separate always-on PC. It auto-discovers your NVR's cameras and serves them as RTSP/WebRTC via a
bundled, pinned go2rtc. No cloud media, no Frigate — only the signaling handshake touches eufy's
cloud; the video itself is pulled LAN-direct from the NVR.

> **Status: experimental.** v0.7.15 serializes the NVR's single live session, retains only the
> last-viewed live camera with an adaptive lease, and serves pre-seeded Home Assistant thumbnails stale-while-revalidate.
> It also includes Eufy mailbox/device verification, account-bound auth caches, authenticated LAN access,
> strict process supervision, and verified immutable build inputs. The
> HACS integration, **"Eufy NVR (local)"**, auto-creates the camera
> entities from the bridge's go2rtc; install it separately from this repo.

## What it runs

- A Debian-based container (HA `*-base-debian:bookworm`) with python3 + aiortc/av/pycryptodome/
  cryptography, nodejs (the libsctp WASM framing oracle), ffmpeg, and a pinned go2rtc.
- `run.sh` (bashio): logs into the eufy passport (`auth_login.py`) to build `auth.json`,
  auto-discovers cameras, generates `go2rtc.yaml`, then supervises go2rtc with restart-on-crash and
  capped backoff.

## Install

1. Home Assistant -> **Settings -> Add-ons -> Add-on Store -> ⋮ -> Repositories** -> add
   `https://github.com/HallyAus/Eufy-Home-Assistant` -> **Add**.
2. Find and install **Eufy NVR Local (experimental)**. (First build is slow: it compiles/links the
   WebRTC stack and downloads go2rtc.)
3. **Configuration tab** of the add-on — enter your eufy account and region:
   - `email`     -> the **owner/admin eufy account for the NVR**. Shared/member accounts can login and
     see the station but the NVR may reject control commands with status `-104`.
   - `password`  -> your eufy account password
   - `region`    -> `US`, `EU`, or `IE` (the eufy server region holding the account)
   - `country`   -> optional real account country such as `AU` or `GB`; leave blank to use `region`
   - `signaling_mode` -> `call` (the compatible T8N00 native-SDP mode; use `scall` only for diagnostics)
   - `log_level` -> `info` (raise to `debug` only when troubleshooting)
   - `go2rtc_username` / `go2rtc_password` -> required local credentials shared with the companion
     integration; use a password of at least 16 characters
   - `adaptive_warm_seconds` -> retain only the last-viewed camera for this many seconds after it closes
     (default `30`; a different camera preempts it; set `0` to disable)
   - *(optional)* `station_sn` — only if auto-discovery can't find your NVR's serial.
   - *(optional)* `captcha_id` + `captcha_answer` — only if a login is challenged with a graphic
     captcha (the log prints the `captcha_id`; solve it and set both, then restart).
   - *(optional)* `verification_code` — if the log reports mailbox verification, enter the six-digit
     code Eufy emailed and restart. The first challenged start requests the code automatically.

   On start the add-on logs into the eufy passport, derives your NVR's `station_sn` from the station
   list, and writes an in-container `auth.json` (chmod 600). Your password is passed only via the
   environment, scrubbed right after login, and never printed to the log. Cached auth is accepted only
   when it is bound to the same account, server region, and country.
4. **Start** the add-on and watch the **Log** tab. It logs in, discovers your cameras, generates the
   stream list, and starts go2rtc. Click **Open Web UI** (Eufy go2rtc on port 1985) to see/test the streams.

> The NVR allows **one** active live session, and a passport login bumps the signed-in app session.
> Avoid logging into the eufy mobile app at the same moment the add-on is starting/discovering/streaming.

## Use the cameras in Home Assistant

On the HA host the add-on serves:

- RTSP: `rtsp://<username>:<password>@<HA-LAN-IP>:8556/eufy_<camera>`
- go2rtc UI / API: `http://<ha-ip>:1985/`

Stream names are derived from your camera names and persisted after first assignment. That means a
camera rename does not unnecessarily create a new Home Assistant entity, and colliding names are
resolved without silently replacing another camera. The exact stream list is printed in the add-on
log and shown in the go2rtc UI. To surface them as camera entities, either:

- install the companion **"Eufy NVR (local)"** HACS integration (auto-creates one camera per stream), or
- use the **Generic Camera** integration -> *Stream Source* `rtsp://<HA-LAN-IP>:8556/eufy_garage`, or
- add them to HA's own `/config/go2rtc.yaml` and reference from a `camera:` / WebRTC card.

Streams are **on-demand**: the engine only connects to the NVR while something is actually pulling a
stream, so the single live session is freed when nobody is watching. go2rtc 1.9.14 is configured with
a 90-second exec `starttimeout` so queued or retrying Eufy WebRTC cold starts are not killed prematurely. An
Eufy-specific controller holds only the most recently viewed producer for a short adaptive lease and
hands the single NVR session to a newly requested camera.

## Ports

| Port      | Purpose                                                        |
|-----------|---------------------------------------------------------------|
| 8556/tcp  | RTSP — HA pulls cameras from here                             |
| 1985/tcp  | Eufy go2rtc API + UI (also the Supervisor watchdog endpoint)  |
| 8557/tcp+udp | WebRTC candidates                                          |

The add-on runs with `host_network: true` (required: LAN-direct media to the NVR + same-host RTSP to
HA), so these ports are opened directly on the host.

## Reliability

- **Supervisor watchdog** polls `tcp://[HOST]:1985`; if go2rtc's API stops answering, the container
  is restarted automatically.
- **Persistent recovery state** keeps the last successful account-bound auth session, discovery result, generated
  go2rtc configuration, and stable stream-name registry in `/data`, so transient failures or camera
  renames do not destroy working state.
- **Automatic boot** brings the bridge back after a Home Assistant host restart.
- **Periodic authentication refresh** runs independently of camera demand, preventing an
  expired cloud signaling session from leaving healthy local go2rtc ports with unusable producers.
- **In-process supervise loop** in `run.sh` restarts go2rtc on a plain crash with exponential backoff
  (2s -> 60s cap), recovering faster than a full container bounce and without hammering the NVR.
- Generated camera and go2rtc state is validated before replacement and written atomically.
- Concurrent Home Assistant thumbnail requests use go2rtc's authenticated cached-JPEG endpoint and a
  bounded stale-while-revalidate fallback. A low-frequency sequential primer seeds each camera without opening
  competing producers; later dashboard bursts return immediately while one background refresh runs.
- A cross-process gate prevents competing cameras from opening simultaneous sessions against the NVR;
  signaling status `486` is classified immediately instead of waiting for the signaling timeout. go2rtc sends
  `SIGINT` to the Eufy supervisor with a bounded graceful ceiling, and the engine waits for the NVR's status-0
  `closeLive` acknowledgement after discovery and video sessions, then retains the session lock for the bounded
  appliance retirement grace so neither a ghost owner nor a next-camera race can produce status `486`.
  A queued producer directly preempts a different camera's adaptive warm lease, without waiting for go2rtc to
  expose a consumer that cannot exist until that producer has started.
- A Docker `HEALTHCHECK` probes the local go2rtc TCP listener without bypassing API authentication.

## Troubleshooting

- **"Set email and password"** — fill in the Configuration tab (step 3).
- **"Set go2rtc username/password"** — configure a local password of at least 16 characters, then use
  the same values when setting up or reconfiguring the companion integration.
- **"Email verification required"** — check the mailbox for the six-digit code, put it in
  `verification_code`, and restart. Remove the code after the login succeeds.
- **`ERROR STATUS=-104` / discovery connects but never returns `dev_list`** — use the eufy account
  that owns/administers the NVR, not a shared/member account.
- **"Headless login failed"** — check email / password / region. If the log shows a **CAPTCHA**, set
  `captcha_id` + `captcha_answer` from the log and restart. A wrong password several times in a row can
  trigger a temporary lockout.
- **"Discovery failed" / streams never start** — confirm `region` matches your account (US/EU/IE). If
  auto-discovery can't find the NVR, set `station_sn` to your NVR's serial explicitly.
- **`scall/turn status 100` with no later status 200 or SDP offer** — leave `signaling_mode` at its
  default `call`. Some deployed T8N00 firmware advertises online but returns status `408` for compact
  `scall`; native `call` returns status `200` and a full SDP offer immediately.
- **go2rtc `exec: timeout` / discovery works but live video does not** — v0.7 uses go2rtc 1.9.14 and a
  longer producer startup window. If it still fails, capture the per-camera `eufy_stream.py` signalling
  lines; the remaining fault is inside the live-session handshake rather than camera discovery.
- **`scall/turn status 486`** — another client, commonly the official eufy app, owns the NVR's one live
  session. Close that live view and retry; the add-on itself never opens competing camera sessions.
- **Image build fails at `fetch_deps` or the SCTP self-test** — eufy changed or removed the framing
  runtime. Match `SCTP_VERSION` in `bridge/fetch_deps.js` + `bridge/sctp_oracle.js` to the web
  client's current `versionControl.verLibsctp`, then rebuild. The add-on deliberately refuses to
  install an image that cannot frame NVR traffic.

## Notes

- This is independent interoperability work for **your own hardware**; not affiliated with Anker/eufy.
- Video is pulled LAN-direct; only the signaling token uses eufy's cloud.
