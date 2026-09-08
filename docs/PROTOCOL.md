# eufy S4 / T8N00 NVR — WebRTC local-stream protocol (reverse-engineered)

Reversed from the official eufy web client (`security.eufy.com` / `nvr.eufy.com`) for interoperability with
**owned** hardware. Tested against an S4 Max NVR (`T8N00`, fw 1.3.3.0) + PoE cameras (`T8E00`, channels 0–3).

## 0. Why P2P libraries can't stream it
The NVR's cloud provisioning returns empty `p2p_conn` / `app_conn` and instead exposes `signaling_servers` +
`webrtc_sdk_version`. There is **no ThroughTek/PPPP session to dial** — it's WebRTC. `bropat/eufy-security-client`
(and the HACS integration on top of it) has no WebRTC support, hence "experimental"/non-working for this model.

## 1. Auth
- Log into the eufy web portal (passport login → `auth_token`, 40 chars). Account region routes via
  `…/passport/estimate_domain`.
- Per-station signaling token: `GET https://security-smart.eufylife.com/v1/smart/nvr/ws/sign?station_sn=<SN>`
  with headers `x-auth-token`, `gtoken` (= MD5(user_id)), `app-name: eufy_mega`, `model-type: WEB`,
  `web-country: <CC>` → `{code:0, data:"<sessionToken>"}`.
- `account_id` (a.k.a. `user_id`, 40-hex) used inside commands = AES-decrypt of localStorage `auid`
  (CryptoJS AES, passphrase `"aes"`, OpenSSL `EVP_BytesToKey`/MD5, AES-256-CBC). `gtoken`/`_gt` is MD5(user_id)
  and is **not** reversible.

## 2. Signaling (cloud WebSocket; plaintext JSON; media stays LAN-local)
`wss://security-smart.eufylife.com/v1/rtc/ws/join?reqtype=nvr`, auth carried in the `Sec-WebSocket-Protocol`
header: `v1, base64url({region,type:"NVR",sn,token,gtoken,sign,appName,modelType})`.
Every frame: `{"msgid": "<auth-token>_<request-id>", "data": "<stringified inner JSON>"}` (the join
uses message ID `0`). Action-3 messages bind the request to the logged-in NVR administrator with
`account = MD5(channel_id + admin_user_id + unix_timestamp)`. Handshake:
1. C→S `action:1` join with `data:<sessionToken>`
2. S→C `action:1 isResponse:1 {status:200}`
3. C→S `action:3 dataType:"scall"` → S→C returns `turn:{...}` (relay creds; unused for LAN-direct)
   with status 100, then status 200; acknowledge status 200 immediately with `dataType:"ack"`.
4. S→C `action:3 dataType:"info" source:"DEVICE"` = compact SDP offer `{ice:{ufrag,pwd,fingerprint}, setup:"actpass"}`
5. S→C trickle `CANDIDATE`s (host `192.168.1.152`, `192.168.32.2`, srflx, relay). **The host candidate can arrive
   *before* the offer — buffer candidates until the remote description is set, or the LAN pair is lost.**
6. C→S `ack`, then `info` with our compact SDP (`setup:"active"`) + our host candidate.
- The compact SDP carries only ice ufrag/pwd/fingerprint + setup; the full SDP is a fixed template
  (`m=application 9 UDP/DTLS/SCTP webrtc-datachannel`, `a=sctp-port:5000`, `a=max-message-size:262144`).
- **DTLS gotcha:** the NVR cert is RSA; aiortc only offers ECDHE-ECDSA by default → handshake fails. Broaden the
  cipher list to include ECDHE-RSA.

## 3. DataChannels + the inner "PTCS" transport
The client creates 6 channels: `["WebrtcDataChannel","audio","idr","video","notify","download"]`. Over these runs
eufy's own reliable transport (FEC + NACK retransmit), implemented in WASM (`libsctp`, `sctp_frame_manager_web.c`,
v1.0.1). Wire packets begin with magic **`PTCS`** (`50 54 43 53`); app frames are fragmented into ≤1000-byte
packets and reassembled by the WASM. We run eufy's exact `libsctp_*.wasm` in Node (see `bridge/sctp_oracle.js`) so
we don't reimplement FEC. Outgoing app frames go on `WebrtcDataChannel`. **Do not** echo the receive-side
frame-manager's NACK output back to the device — the official client ignores it, and a fresh receiver emits
spurious NACKs that stall the stream.

## 4. App framing — the 16-byte `XZYH` header
Each app message = `XZYH` header + payload:
```
off 0  [4]  "XZYH" (58 5A 59 48)
off 4  [2]  command_id (u16 LE)
off 6  [4]  param_len  (u32 LE)
off 10 [1]  0
off 11 [1]  segmen
off 12 [1]  channel_id
off 13 [1]  0
off 14 [1]  is_response   (also reused to carry streamId on startStream)
off 15 [1]  dev_type      (cloud web client = 2)
```
For these commands the payload is JSON: `{"account_id":<user_id>, "cmd":<sub-cmd>, "payload":{...}}`.

## 5. Commands (all command_id 1350; the sub-command is `cmd`)
- **`cmd 1103` (openLive / getCameraParams)** — returns a params JSON (camera names, config, power). **Does not
  start video.** payload `{"channel_info":{"array_size":N,"channel_array":[ch…]}}`.
- **`cmd 1003` (startStream) — THE video trigger.** Header `is_response` byte = streamId. JSON payload:
  ```json
  {"account_id":"<user_id>","cmd":1003,"payload":{
     "ClientOS":"WEB","entrytype":1,"camera_type":0,"streamtype":2,
     "key":"","msg_id":"","audio_chn":-1,"stitch_mode":1,
     "chn_list":[{"index":0,"chn":0,"sensor":1}]}}
  ```
  `entrytype:1` = live (2 = preset). `streamtype` selects the profile. `chn_list` = channels to stream.
- **`cmd 1004` (closeLive)** — same envelope, empty payload.
- **Heartbeat (`cmd 1139`)** — 36 **raw** bytes (not PTCS-framed) on `WebrtcDataChannel` every ~15 s:
  `[20-byte struct: 00 09 00 00, u16 16 @4, 0x63 @12][16-byte XZYH(1139, dev_type 2)]`. Needed to *sustain*
  (60 s timeout), not to start. The device replies with 1139 at offset 24.
- **`cmd 1032`** — device→client per-channel status/heartbeat (8-byte payload int32; `-2` = channel idle/not
  streaming). **Do not ack.** It transitions once `startStream` succeeds.

## 6. Video frames
command_id **1300** (also 1301/1303 in the raw set), arriving on the live link. Reassembled frame =
`[16-byte XZYH][22-byte media header][Annex-B H.265 NAL]`. The media header's first 4 bytes are the NAL length
(u32 LE). **Strip 38 bytes → clean HEVC Annex-B** (`00 00 00 01` start codes; VPS=32 / SPS=33 / PPS=34 / IDR=19 /
TRAIL=1, ~1080p, GOP ~2 s). Decode with any HEVC decoder; remux/repackage with ffmpeg → RTSP/WebRTC.

## 7. Implementation gotchas (all handled in this repo)
- ICE candidate-before-offer race → buffer + flush after `setRemoteDescription`.
- RSA-only DTLS → broaden aiortc cipher list (add ECDHE-RSA).
- Node child stdout default 64 KB line limit crashes on a base64'd keyframe → raise the subprocess `limit`.
- Single active NVR session; pace reconnects; prefer on-demand (go2rtc exec).
