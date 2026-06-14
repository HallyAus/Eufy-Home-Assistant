<div align="center">

# 🎥 Eufy S4 / PoE NVR → Home Assistant

### Local, LAN-direct live video from a eufy WebRTC NVR — no cloud media, no Frigate.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge&logo=homeassistantcommunitystore&logoColor=white)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.11+-41BDF5?style=for-the-badge&logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![go2rtc](https://img.shields.io/badge/go2rtc-RTSP%20%2F%20WebRTC-success?style=for-the-badge&logo=webrtc&logoColor=white)](https://github.com/AlexxIT/go2rtc)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=HallyAus&repository=Eufy-Home-Assistant&category=integration)

<sub>

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Node.js](https://img.shields.io/badge/Node-18+-339933?logo=nodedotjs&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-H.265-007808?logo=ffmpeg&logoColor=white)
![WebRTC](https://img.shields.io/badge/WebRTC-DTLS%2FSCTP-333333?logo=webrtc&logoColor=white)
[![GitHub stars](https://img.shields.io/github/stars/HallyAus/Eufy-Home-Assistant?style=social)](https://github.com/HallyAus/Eufy-Home-Assistant/stargazers)

</sub>

</div>

Pull a **local, LAN-direct live video stream** from a **eufy PoE NVR (S4 Max / model `T8N00`)** and its PoE
cameras into **Home Assistant** — as a standard RTSP/WebRTC stream you can drop straight onto a dashboard.

No eufy cloud relay for the video. No Frigate. No flashing the cameras. Just Home Assistant's built-in
**go2rtc** and a small bridge that speaks the NVR's (previously undocumented) WebRTC protocol.

> ⚠️ This is independent interoperability/reverse-engineering work for use with **your own** hardware. It is not
> affiliated with or endorsed by Anker/eufy. Use it on devices you own.

---

## TL;DR — two ways to run it

**The easy way (Home Assistant OS):** install the **add-on**, type your eufy **email + password**, done. The
add-on runs the whole engine on your HA host and serves the cameras as RTSP/WebRTC. Add the companion
**integration** and the camera entities appear automatically.

**The manual way (any HA, or a separate PC):** run the **bridge** yourself on a LAN machine with Python + Node
+ ffmpeg, and point HA at its RTSP URLs.

```
eufy NVR  ──WebRTC (DTLS/SCTP, LAN-direct)──►  bridge / add-on  ──RTSP──►  Home Assistant (go2rtc)
192.168.1.152                                  Python + Node + ffmpeg + go2rtc          dashboard / cameras
```

Live 1080p H.265, ~15–25 fps, pulled **directly over your LAN** — the only thing that touches eufy's cloud is the
login/signaling handshake; the pixels never leave your network. Streams are named `eufy_<camera>` (e.g.
`eufy_garage`, `eufy_front_gate`).

---

## Why this exists — what we found

The eufy S4 generation was widely assumed to use the classic eufy/ThroughTek **P2P** transport (the AES-128-ECB
"start livestream" path that [bropat/eufy-security-client](https://github.com/bropat/eufy-security-client) and the
[fuatakgun/eufy-security](https://github.com/fuatakgun/eufy-security) HACS integration implement). **It doesn't.**

We captured the official web client (`security.eufy.com` / `nvr.eufy.com`) and reversed the protocol. The findings:

1. **It's WebRTC, not P2P.** The NVR's cloud provisioning has empty `p2p_conn`/`app_conn` and instead lists
   `signaling_servers` + `webrtc_sdk_version`. bropat/eufy-security-client has **no** WebRTC support, which is
   exactly why this NVR is "experimental"/non-working there.
2. **Signaling is cloud, media is local.** A small JSON handshake over a cloud WebSocket
   (`security-smart.eufylife.com`) exchanges SDP/ICE. The winning ICE pair is your host ↔ the NVR's LAN IP
   (`192.168.1.152 typ host`) — **media flows LAN-direct over DTLS/SCTP**, not through eufy's TURN relay.
3. **There is an extra framing layer.** The 6 WebRTC DataChannels carry an *inner* eufy reliable transport
   (magic `"PTCS"`, with FEC + retransmission) implemented in a WebAssembly module (`libsctp`,
   `sctp_frame_manager_web.c`). App messages are wrapped in a 16-byte `XZYH` header and fragmented into PTCS
   packets. We run eufy's **exact WASM** as a framing oracle (in Node) so we don't have to reimplement FEC.
4. **`openLive` is a red herring; `startStream` is the trigger.** The command that *returns camera params*
   (`cmd 1103`) does **not** start video. Live video only begins after a separate **`startStream` command
   (`cmd 1003`)** with a `chn_list` payload. This single fact was the whole ballgame.
5. **Video = H.265.** Each video DataChannel message is `[16-byte XZYH header][22-byte media header][Annex-B
   HEVC NAL]`. Strip 38 bytes → a clean H.265 elementary stream (VPS/SPS/PPS/IDR + P-frames, 1080p).
6. **Headless login is ECDH, not RSA.** The web "passport" login encrypts the password with ECDH(P-256)+AES-256-CBC
   and rides an encrypted, HMAC-signed request wrapper keyed on the *openapi* key/exchange (`gtoken = MD5(user_id)`).
   Reversing this is what makes "type email + password → done" possible. See [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

Full technical write-up: [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## Architecture

The heavy lifting (WebRTC + DTLS + the libsctp WASM + H.265 extraction) needs Python, Node and ffmpeg. It runs
either **as a HA add-on** (a container on Home Assistant OS) or **as the bridge** on any LAN machine. Either way it
exposes plain RTSP via a local **go2rtc**; Home Assistant simply pulls it.

```
                         ┌──────────── add-on (on HAOS) │ or bridge host (a PC/NUC on the LAN) ────────────┐
 eufy NVR  WebRTC        │  eufy_stream.py (aiortc)  ──►  sctp_oracle.js (eufy libsctp WASM, Node)         │
 T8N00 ───────────────►  │        │  startStream cmd 1003 + heartbeat                                       │
 (LAN-direct DTLS/SCTP)  │        ▼  H.265 Annex-B                                                           │
                         │     ffmpeg ──►  go2rtc ──►  rtsp://<host>:8554/eufy_<camera>  (one per camera)    │
                         └────────────────────────────────────────────────────────────────────────────────┘
                                                       │ RTSP pull (LAN)
                                                       ▼
                              Home Assistant (built-in go2rtc)  ──►  WebRTC/HLS on your dashboard
```

**On-demand:** go2rtc only spawns the engine while something is actually watching, so the NVR's single live
session isn't held 24/7 (important — the NVR allows one active stream at a time).

---

## Install — Option A: Home Assistant add-on (recommended)

Runs everything on your HA host; no always-on PC and no token paste.

1. **Settings → Add-ons → Add-on Store → ⋮ (top-right) → Repositories** → add
   `https://github.com/HallyAus/Eufy-Home-Assistant` → **Add**, then close.
2. Find and install **Eufy NVR Local (experimental)**. (First build is slow — it compiles/links the WebRTC stack
   and downloads go2rtc.)
3. **Configuration** tab → enter your eufy account:
   - `email` / `password` — your eufy login
   - `region` — `US` or `EU`
   - `log_level` — `info` (raise to `debug` only when troubleshooting)
   - *(optional)* `station_sn` — only if auto-discovery can't find your NVR's serial
   - *(optional)* `captcha_id` + `captcha_answer` — only if a login is challenged (the log prints the `captcha_id`)
4. **Start** the add-on and watch the **Log** tab. It logs in, discovers your cameras, generates the stream list,
   and starts go2rtc. Click **Open Web UI** (go2rtc :1984) to test the streams.

Your password is passed only via the environment, scrubbed right after login, and never printed to the log.

Then add the **companion integration** (below) to get camera entities — for the add-on use host **`127.0.0.1`**.

---

## Install — Option B: manual bridge (any HA, or a separate PC)

For a non-HAOS Home Assistant, or to run the engine on a different always-on machine.

**Requirements (bridge host, Windows or Linux, same LAN as the NVR):**
- **Python 3.11+** with `aiortc av websockets aiohttp pycryptodome cryptography` (`bridge/requirements.txt`)
- **Node 18+** (runs the libsctp WASM oracle)
- **ffmpeg** + **go2rtc** (`bridge/fetch_deps.js` downloads both, plus eufy's WASM)
- A **eufy account** that owns the NVR

```bash
git clone https://github.com/HallyAus/Eufy-Home-Assistant
cd Eufy-Home-Assistant/bridge

pip install -r requirements.txt        # Python deps
node fetch_deps.js                     # downloads ffmpeg, go2rtc, eufy's libsctp WASM

# Auth — pick ONE:
#  (a) headless email/password login -> writes auth.json (gitignored):
EUFY_EMAIL="you@example.com" EUFY_PASSWORD="••••••" EUFY_REGION="US" python auth_login.py
#  (b) or the browser token method:
node get_auth.js                       # log into the eufy web portal -> auth.json

# Auto-discover the NVR IP + cameras, then generate friendly stream names:
python eufy_stream.py --discover       # writes cameras.json
python gen_go2rtc.py <BRIDGE_IP>       # writes go2rtc.yaml (eufy_garage, eufy_front_gate, ...)
```

Start it (`start_bridge.cmd` on Windows, `./start_bridge.sh` on Linux) and verify with any RTSP player:

```bash
ffplay rtsp://127.0.0.1:8554/eufy_garage
```

---

## Get the cameras into Home Assistant

Use whichever you like — both ride HA's built-in go2rtc/camera stack.

### Companion integration (recommended) — auto-creates one camera per stream

The integration polls the engine's go2rtc and creates a camera entity for every `eufy_*` stream automatically —
**no stream names to type, no per-camera config.** New cameras appear on their own.

> Prerequisite: [HACS](https://hacs.xyz/) installed.

1. **HACS → ⋮ → Custom repositories** → Repository `https://github.com/HallyAus/Eufy-Home-Assistant`,
   Category **Integration** → **Add**. (Or use the button below.)

   [![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=HallyAus&repository=Eufy-Home-Assistant&category=integration)

2. In HACS search **"Eufy NVR (local)"** → **Download** → **Restart Home Assistant**.
3. **Settings → Devices & Services → + Add Integration →** search **"Eufy NVR (local)"**, then enter:
   - **Host** — where go2rtc runs. For the **add-on** (Option A) use your **HA host's LAN IP**
     (e.g. `192.168.1.177`) — **not** `127.0.0.1`: the integration runs inside HA Core and can't reach a
     `host_network` add-on over localhost, and HA's own built-in go2rtc already occupies `127.0.0.1:1984`.
     For the **bridge** (Option B) use the **bridge machine's IP** (e.g. `192.168.1.7`).
   - **API port** — `1984` &nbsp;•&nbsp; **RTSP port** — `8554`

   It auto-creates a `camera.eufy_nvr_*` entity per discovered stream, grouped under one "Eufy NVR" device.

   [![Add the Eufy NVR integration to your Home Assistant instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=eufy_nvr)

### Or the Generic Camera integration (no HACS)

**Settings → Devices & Services → Add Integration → Generic Camera** → Stream Source
`rtsp://<host>:8554/eufy_garage` (one per camera; `<host>` = your **HA host's LAN IP** for the add-on, or the bridge IP).
Or paste the streams into HA's own `/config/go2rtc.yaml` and reference them from a `camera:` / WebRTC card.

---

## "I'm not a fan of Frigate" — you don't need it

The engine emits **standard RTSP / H.265**, so any of these work with zero extra infrastructure:

- **Home Assistant native (recommended):** built-in **go2rtc** gives you sub-second WebRTC live view on
  dashboards — no add-ons, no NVR software.
- **HA Generic Camera** integration (RTSP) for a simple camera entity.
- **Recording / events (only if you want them):** [Scrypted](https://github.com/koush/scrypted),
  [Blue Iris](https://blueirissoftware.com/), [MediaMTX](https://github.com/bluenviron/mediamtx), or go2rtc
  itself. Frigate is *one* option, not a requirement.
- **Anything that speaks RTSP** (VLC, ffmpeg, a browser via go2rtc's WebRTC).

---

## Status & roadmap

**Released (v0.4.0):** LAN-direct connect, `startStream`, libsctp reassembly, H.265 extraction, sustained
~18–25 fps 1080p, served as RTSP via go2rtc, with **auto-discovery** of the NVR + cameras (channels 0–3),
**headless email/password login**, a **HA add-on**, and a **companion auto-discovery integration**.

- [x] **Engine** — WebRTC → H.265 → RTSP/go2rtc (proven).
- [x] **Auto-discovery** — NVR IP + cameras + channels via cmd 9100.
- [x] **Headless email/password login** — reversed ECDH/AES passport login; no browser token needed.
- [x] **Companion HACS integration** — auto-creates the camera entities from go2rtc (zero manual entry).
- [~] **HA add-on container** — bundles Python+Node+ffmpeg+go2rtc and runs on HAOS (shipped; the engine/login are
      validated — pending broad on-HAOS build testing across architectures).
- [ ] Audio (`1301`) / two-way talk; auto-reconnect hardening; per-camera quality (`streamtype`).

## Notes / limits

- The NVR allows **one** active live session; rapid reconnects can briefly put it into a timeout state. The
  on-demand design avoids holding the session when nobody's watching.
- A passport login bumps the signed-in app session, so avoid logging into the eufy mobile app at the same moment
  the add-on/bridge is logging in or discovering.
- The login/signaling uses eufy's cloud; the **video itself is LAN-local**.
- Your eufy credentials and session tokens are **gitignored** — never commit them.

## Credits

Protocol reversed from the official eufy web client. Built with
[aiortc](https://github.com/aiortc/aiortc), [go2rtc](https://github.com/AlexxIT/go2rtc), ffmpeg, and eufy's own
`libsctp` WASM (loaded at runtime from eufy's CDN, not redistributed here).
