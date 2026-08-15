# Eufy NVR Local — Home Assistant add-on (experimental)

Runs the whole eufy WebRTC -> RTSP engine **on your Home Assistant host**, so you don't need a
separate always-on PC. It auto-discovers your NVR's cameras and serves them as RTSP/WebRTC via a
bundled, pinned go2rtc. No cloud media, no Frigate — only the signaling handshake touches eufy's
cloud; the video itself is pulled LAN-direct from the NVR.

> **Status: experimental.** v0.6 adds persistent recovery state, automatic host-reboot startup,
> region-aware signaling, and a rebuilt companion integration with diagnostics. The HACS integration,
> **"Eufy NVR (local)"**, auto-creates the camera entities from the bridge's go2rtc; install it
> separately from this repo.

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
   - `email`     -> your eufy account email
   - `password`  -> your eufy account password
   - `region`    -> `US`, `EU`, or `IE` (the eufy server region holding the account)
   - `country`   -> optional real account country such as `AU` or `GB`; leave blank to use `region`
   - `log_level` -> `info` (raise to `debug` only when troubleshooting)
   - *(optional)* `station_sn` — only if auto-discovery can't find your NVR's serial.
   - *(optional)* `captcha_id` + `captcha_answer` — only if a login is challenged with a graphic
     captcha (the log prints the `captcha_id`; solve it and set both, then restart).

   On start the add-on logs into the eufy passport, derives your NVR's `station_sn` from the station
   list, and writes an in-container `auth.json` (chmod 600). Your password is passed only via the
   environment, scrubbed right after login, and never printed to the log.
4. **Start** the add-on and watch the **Log** tab. It logs in, discovers your cameras, generates the
   stream list, and starts go2rtc. Click **Open Web UI** (Eufy go2rtc on port 1985) to see/test the streams.

> The NVR allows **one** active live session, and a passport login bumps the signed-in app session.
> Avoid logging into the eufy mobile app at the same moment the add-on is starting/discovering/streaming.

## Use the cameras in Home Assistant

On the HA host the add-on serves:

- RTSP: `rtsp://<HA-LAN-IP>:8556/eufy_<camera>`
- go2rtc UI / API: `http://<ha-ip>:1985/`

Stream names are slugified from your camera names (e.g. "Garage" -> `eufy_garage`); the exact list is
printed in the add-on log and shown in the go2rtc UI. To surface them as camera entities, either:

- install the companion **"Eufy NVR (local)"** HACS integration (auto-creates one camera per stream), or
- use the **Generic Camera** integration -> *Stream Source* `rtsp://<HA-LAN-IP>:8556/eufy_garage`, or
- add them to HA's own `/config/go2rtc.yaml` and reference from a `camera:` / WebRTC card.

Streams are **on-demand**: the engine only connects to the NVR while something is actually pulling a
stream, so the single live session is freed when nobody is watching.

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
- **Persistent recovery state** keeps the last successful auth session, discovery result, and generated
  go2rtc configuration in `/data`, so a transient cloud/login outage does not erase a working local setup.
- **Automatic boot** brings the bridge back after a Home Assistant host restart.
- **In-process supervise loop** in `run.sh` restarts go2rtc on a plain crash with exponential backoff
  (2s -> 60s cap), recovering faster than a full container bounce and without hammering the NVR.
- A Docker `HEALTHCHECK` hits the same API endpoint.

## Troubleshooting

- **"Set email and password"** — fill in the Configuration tab (step 3).
- **"Headless login failed"** — check email / password / region. If the log shows a **CAPTCHA**, set
  `captcha_id` + `captcha_answer` from the log and restart. A wrong password several times in a row can
  trigger a temporary lockout (retry after ~24h).
- **"Discovery failed" / streams never start** — confirm `region` matches your account (US/EU/IE). If
  auto-discovery can't find the NVR, set `station_sn` to your NVR's serial explicitly.
- **Image build fails at `fetch_deps` or the SCTP self-test** — eufy changed or removed the framing
  runtime. Match `SCTP_VERSION` in `bridge/fetch_deps.js` + `bridge/sctp_oracle.js` to the web
  client's current `versionControl.verLibsctp`, then rebuild. The add-on deliberately refuses to
  install an image that cannot frame NVR traffic.
- **No video but discovery worked** — raise `log_level` to `debug`, restart, and check the log for the
  go2rtc `exec` line failing (python/ffmpeg/node path) or a non-200 from `ws/sign`.

## Notes

- This is independent interoperability work for **your own hardware**; not affiliated with Anker/eufy.
- Video is pulled LAN-direct; only the signaling token uses eufy's cloud.
