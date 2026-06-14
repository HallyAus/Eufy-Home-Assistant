# Changelog

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
