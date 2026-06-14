# Eufy NVR Local — Home Assistant add-on (experimental)

Runs the whole eufy WebRTC→RTSP engine **on your Home Assistant host** so you don't need a separate PC.
It auto-discovers your NVR's cameras and serves them as RTSP/WebRTC via a bundled go2rtc.

> **Status: experimental / foundation.** The container + engine + auto-discovery work; the two pieces that make
> it fully "install → done" are in progress: (1) headless email/password login (v0.2 still needs a one-time token),
> and (2) a companion integration to auto-create the camera entities. See the repo roadmap.

## Install
1. Home Assistant → **Settings → Add-ons → Add-on Store → ⋮ → Repositories** → add
   `https://github.com/HallyAus/Eufy-Home-Assistant` → **Add**.
2. Install **Eufy NVR Local (experimental)**.
3. **Configuration:** until v0.3's headless login lands, paste a session token (one-time): on any PC run
   `bridge/get_auth.js` (logs into the eufy web portal once) and copy `authToken`, `gtoken`, `userId` (=account id),
   and `stationSn` into the add-on options. Set `region` (US/EU).
4. **Start** the add-on. Watch the log — it discovers your cameras and starts go2rtc.

## Use the cameras in Home Assistant
The add-on serves (on the HA host): `rtsp://127.0.0.1:8554/eufy_<camera>` and go2rtc's UI at
`http://<ha-ip>:1984/`. Until the companion integration lands, surface them with either:
- **Generic Camera** integration → Stream Source `rtsp://127.0.0.1:8554/eufy_garage` (one per camera), or
- add them to HA's `/config/go2rtc.yaml`.

Stream names come from your camera names (e.g. "Garage" → `eufy_garage`); check the add-on log or go2rtc UI.

## Notes
- The NVR allows one active live session; the streams are on-demand (only run while something is watching).
- Video is pulled **LAN-direct** from the NVR; only the signaling token uses eufy's cloud.
- This is independent interoperability work for your own hardware; not affiliated with Anker/eufy.
