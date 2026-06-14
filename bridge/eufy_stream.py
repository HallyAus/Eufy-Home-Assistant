#!/usr/bin/env python3
"""
eufy_stream.py - full local live pipeline for the eufy S4 NVR (T8N00).

Reuses the proven WebRTC transport (see eufy_webrtc.py / notes/04) and adds:
  * a Node "libsctp oracle" subprocess (scripts/sctp_oracle.js) running eufy's exact framing WASM,
  * sending the openLive XZYH command (notes/05, 06) so the NVR starts pushing video,
  * reassembling the inbound PTCS frames back into XZYH app frames, and dumping the video.

Auth: captures/eufy_auth.json (webcap/token.js). user_id: captures/user_id.txt (decrypted from auid).
Run:  python scripts/eufy_stream.py [channel]      (channel 0..3, default 0)
"""
import asyncio, json, time, uuid, os, sys, hashlib, random, re, base64, struct

import aiohttp
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.sdp import candidate_from_sdp
import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# aiortc only offers ECDHE-ECDSA; the NVR presents an RSA cert. Broaden ciphers (incl. ECDHE-RSA).
from aiortc.rtcdtlstransport import RTCCertificate as _RTCCert
_orig_ssl_ctx = _RTCCert._create_ssl_context
def _broad_ssl_ctx(self, srtp_profiles):
    ctx = _orig_ssl_ctx(self, srtp_profiles)
    ctx.set_cipher_list(
        b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
        b"ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
        b"ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
        b"ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES128-SHA:"
        b"ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:"
        b"AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA"
    )
    return ctx
_RTCCert._create_ssl_context = _broad_ssl_ctx

ROOT = os.path.dirname(os.path.abspath(__file__))   # the bridge/ directory
def _bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    local = os.path.join(ROOT, "bin", exe)
    return os.environ.get(name.upper(), local if os.path.exists(local) else name)

AUTH = json.load(open(os.environ.get("EUFY_AUTH", os.path.join(ROOT, "auth.json"))))
STATION_SN = os.environ.get("EUFY_STATION_SN") or AUTH.get("stationSn") or "T8N005102610052C"

def _decrypt_user_id():
    if AUTH.get("userId"):
        return AUTH["userId"]
    auid = AUTH.get("auid")
    if not auid:
        raise SystemExit("auth.json has no 'userId' or 'auid' — re-run get_auth.js")
    from Crypto.Cipher import AES
    blob = base64.b64decode(base64.b64decode(auid).decode("utf-8"))
    salt, ct = blob[8:16], blob[16:]
    d = b""; prev = b""
    while len(d) < 48:
        prev = hashlib.md5(prev + b"aes" + salt).digest(); d += prev
    pt = AES.new(d[:32], AES.MODE_CBC, d[32:48]).decrypt(ct)
    return json.loads(pt[:-pt[-1]])
USER_ID = _decrypt_user_id()
DISCOVER = "--discover" in sys.argv   # connect, run cmd 9100 -> list NVR ip + cameras (ch,name), write cameras.json, exit
_carg = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "0"
CHANNELS = [0, 1, 2, 3] if _carg == "all" else [int(x) for x in _carg.split(",")]
# --rtsp <url>: publish the H.265 stream to that RTSP url via ffmpeg (go2rtc exec {output} mode).
RTSP_URL = None
if "--rtsp" in sys.argv:
    RTSP_URL = sys.argv[sys.argv.index("--rtsp") + 1]
STREAM_MODE = bool(RTSP_URL) or os.environ.get("EUFY_STDOUT") == "1"   # run indefinitely, no frame-count stop
_rest = [a for a in sys.argv[2:] if a not in ("--rtsp", RTSP_URL)]
RUN_SECS = int(_rest[0]) if _rest and _rest[0].isdigit() else (10**9 if STREAM_MODE else 70)
NODE = _bin("node")
ORACLE = os.path.join(ROOT, "sctp_oracle.js")
FFMPEG = _bin("ffmpeg")

WS_URL = "wss://security-smart.eufylife.com/v1/rtc/ws/join?reqtype=nvr"
SIGN_URL = f"https://security-smart.eufylife.com/v1/smart/nvr/ws/sign?station_sn={STATION_SN}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
HEADERS = {
    "x-auth-token": AUTH["authToken"], "gtoken": AUTH["gtoken"],
    "app-name": AUTH.get("appName", "eufy_mega"), "model-type": "WEB",
    "web-country": AUTH.get("webCountry", "US"),
    "accept": "application/json, text/plain, */*", "user-agent": UA,
    "origin": "https://security.eufy.com", "referer": "https://security.eufy.com/",
}
os.makedirs(os.path.join(ROOT, "_debug"), exist_ok=True)
VIDEO_DUMP = os.path.join(ROOT, "_debug", "video_dump.bin")
FRAMES_LOG = os.path.join(ROOT, "_debug", "frames.jsonl")
CAMERAS_JSON = os.path.join(ROOT, "cameras.json")

STDOUT_MODE = os.environ.get("EUFY_STDOUT") == "1"   # write clean Annex-B to stdout (for go2rtc/ffmpeg exec source)
def now(): return int(time.time())
def acct(): return hashlib.md5(str(random.random()).encode()).hexdigest()
def log(*a): print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True, file=sys.stderr)

def build_openlive(user_id, channels):
    payload = json.dumps({
        "account_id": user_id, "cmd": 1103,
        "payload": {"channel_info": {"array_size": len(channels), "channel_array": channels}},
    }, separators=(",", ":")).encode()
    h = bytearray(16)
    h[0:4] = b"XZYH"
    struct.pack_into("<H", h, 4, 1350)          # command_id
    struct.pack_into("<I", h, 6, len(payload))  # param_len
    h[10] = 0; h[11] = 0                          # segmen
    h[12] = 255; h[13] = 0                        # channel_id, sign_code
    h[14] = 0; h[15] = 2                          # is_response, dev_type (cloud=2)
    return bytes(h) + payload

def build_startstream(user_id, channels, stream_id=1):
    # cmd 1003 (ic / startStream) — the ACTUAL live-video trigger (cmd 1103 is only a param query).
    chn_list = [{"index": i, "chn": c, "sensor": 1} for i, c in enumerate(channels)]
    payload = json.dumps({
        "account_id": user_id, "cmd": 1003,
        "payload": {"ClientOS": "WEB", "entrytype": 1, "camera_type": 0, "streamtype": 2,
                    "key": "", "msg_id": "", "audio_chn": -1, "stitch_mode": 1, "chn_list": chn_list},
    }, separators=(",", ":")).encode()
    h = bytearray(16)
    h[0:4] = b"XZYH"
    struct.pack_into("<H", h, 4, 1350)
    struct.pack_into("<I", h, 6, len(payload))
    h[10] = 0; h[11] = 0
    h[12] = 255; h[13] = 0
    h[14] = stream_id & 0xFF                      # isResponse byte carries streamId (per pr cloud branch)
    h[15] = 2
    return bytes(h) + payload

def build_cmd(user_id, cmd, payload=None, channel_id=255):
    body = json.dumps({"account_id": user_id, "cmd": cmd, "payload": payload or {}}, separators=(",", ":")).encode()
    h = bytearray(16); h[0:4] = b"XZYH"
    struct.pack_into("<H", h, 4, 1350)
    struct.pack_into("<I", h, 6, len(body))
    h[12] = channel_id & 0xFF; h[15] = 2
    return bytes(h) + body

def build_devicelist(user_id):
    return build_cmd(user_id, 9100, {})   # response payload.dev_list = [{ch,sn,name,status,...}] (auto-discovery)

def build_ping():
    # heartbeat: 20-byte struct + 16-byte XZYH(1139), sent RAW (type Ir=99), per the web client.
    s = bytearray(20)
    s[0] = 0x00; s[1] = 0x09
    struct.pack_into("<H", s, 4, 16)   # header byteLength = 16
    s[12] = 99                          # Ir (WrtcLinkTypeInner)
    hdr = bytearray(16)
    hdr[0:4] = b"XZYH"
    struct.pack_into("<H", hdr, 4, 1139)
    hdr[15] = 2
    return bytes(s) + bytes(hdr)

async def get_sign_token():
    async with aiohttp.ClientSession() as s:
        async with s.get(SIGN_URL, headers=HEADERS) as r:
            txt = await r.text(); log("ws/sign", r.status, txt[:120])
            return json.loads(txt)["data"]

def colonize(fp): return ":".join(fp[i:i+2] for i in range(0, len(fp), 2))

def build_offer_sdp(ice, setup):
    return ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        "a=group:BUNDLE 2\r\na=msid-semantic: WMS\r\n"
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        "c=IN IP4 127.0.0.1\r\na=mid:2\r\na=ice-options:trickle\r\n"
        f"a=ice-ufrag:{ice['ufrag']}\r\na=ice-pwd:{ice['pwd']}\r\n"
        f"a=fingerprint:sha-256 {colonize(ice['fingerprint'])}\r\n"
        f"a=setup:{setup}\r\na=sctp-port:5000\r\na=max-message-size:262144\r\n")

def extract_local_ice(sdp):
    ufrag = re.search(r"a=ice-ufrag:(\S+)", sdp).group(1)
    pwd = re.search(r"a=ice-pwd:(\S+)", sdp).group(1)
    fp = re.search(r"a=fingerprint:sha-256 (\S+)", sdp).group(1).replace(":", "")
    cands = re.findall(r"a=(candidate:\S[^\r\n]*)", sdp)
    return ufrag, pwd, fp, cands

class Signal:
    def __init__(self, ws, sid): self.ws = ws; self.sid = sid
    async def send(self, inner):
        await self.ws.send(json.dumps({"msgid": str(uuid.uuid4()), "data": json.dumps(inner)}))
    async def join(self):
        await self.send({"code": 200, "action": 1, "data": self.sid, "sn": STATION_SN, "source": "WEB", "ts": now()})
    async def action3(self, dt, obj):
        await self.send({"code": 200, "action": 3, "sessionId": self.sid, "sn": STATION_SN, "subSn": "",
                         "channelId": 0, "isResponse": 0, "dataType": dt, "source": "WEB", "ts": now(),
                         "data": json.dumps(obj)})
    async def scall(self): await self.action3("scall", {"timestamp": now(), "account": acct()})
    async def ack(self): await self.action3("ack", {"timestamp": now(), "account": acct()})
    async def send_sdp(self, uf, pw, fp, setup="active"):
        sdp = {"ice": {"ufrag": uf, "pwd": pw, "fingerprint": fp.upper(), "fingerprint_type": "sha-256"}, "setup": setup}
        await self.action3("info", {"timestamp": now(), "account": acct(), "sdp": json.dumps(sdp)})
    async def send_candidate(self, c):
        await self.action3("info", {"timestamp": now(), "account": acct(), "candidate": c})

def parse_msg(raw):
    if isinstance(raw, (bytes, bytearray)): raw = raw.decode("utf-8", "replace")
    outer = json.loads(raw); inner = outer.get("data")
    if isinstance(inner, str):
        try: inner = json.loads(inner)
        except Exception: pass
    d = inner.get("data") if isinstance(inner, dict) else None
    if isinstance(d, str):
        try: d = json.loads(d)
        except Exception: pass
    return inner, d


class Oracle:
    """Bridge to the Node libsctp framing oracle."""
    def __init__(self):
        self.proc = None; self.ready = asyncio.Event()
        self.on_tx = None      # callback(bytes) -> send PTCS packet on WebrtcDataChannel
        self.on_frame = None   # callback(link:int, bytes) -> reassembled frame

    async def start(self):
        # limit must hold a whole base64'd video frame on one stdout line (1080p keyframes can be >100KB).
        self.proc = await asyncio.create_subprocess_exec(
            NODE, ORACLE, "serve",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024)
        asyncio.ensure_future(self._read_stdout())
        asyncio.ensure_future(self._read_stderr())
        await asyncio.wait_for(self.ready.wait(), timeout=15)
        log("oracle ready")

    async def _read_stdout(self):
        while True:
            try:
                line = await self.proc.stdout.readline()
            except Exception as e:
                log("oracle stdout read err:", repr(e));
                await asyncio.sleep(0.01); continue
            if not line:
                break
            try: msg = json.loads(line.decode("utf-8").strip())
            except Exception: continue
            ev = msg.get("ev")
            if ev == "ready": self.ready.set()
            elif ev == "tx":
                # Match the web client: it IGNORES the recv frame-manager's send_packet output (no NACKs).
                # Only the sender frame-manager's packets (commands) go on the wire.
                if msg.get("src") == "send" and self.on_tx:
                    self.on_tx(base64.b64decode(msg["b64"]))
            elif ev == "frame":
                if self.on_frame: self.on_frame(msg.get("channel"), base64.b64decode(msg["b64"]))
            elif ev == "error":
                log("oracle error:", msg)

    async def _read_stderr(self):
        async for line in self.proc.stderr:
            s = line.decode("utf-8", "replace").rstrip()
            if s and "SCTP Version" not in s:
                log("oracle[stderr]:", s[:200])

    def push_recv(self, ptcs_bytes):
        self._write({"op": "recv", "b64": base64.b64encode(ptcs_bytes).decode()})

    def push_send(self, link, frame_bytes):
        self._write({"op": "send", "link": link, "b64": base64.b64encode(frame_bytes).decode()})

    def _write(self, obj):
        if self.proc and self.proc.stdin:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())


async def main():
    sign_token = await get_sign_token()
    log("sign token:", sign_token[:20], "... station", STATION_SN, "channels", CHANNELS, "user_id", USER_ID[:8] + "..")

    oracle = Oracle(); await oracle.start()

    sub = {"region": AUTH.get("webCountry", "US"), "type": "NVR", "sn": STATION_SN,
           "token": AUTH["authToken"], "gtoken": AUTH["gtoken"], "sign": sign_token,
           "appName": AUTH.get("appName", "eufy_mega"), "modelType": "WEB"}
    subproto = base64.urlsafe_b64encode(json.dumps(sub, separators=(",", ":")).encode()).decode().rstrip("=")

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    chans = {}
    state = {"connected": False, "started": False, "vbytes": 0, "vframes": 0, "ptcs_in": 0,
             "cmd_dc_open": False, "frames_seen": 0, "nvr_ip": None, "discovered": False}
    dumpf = open(VIDEO_DUMP, "wb"); framelog = open(FRAMES_LOG, "w")

    # Pick the Annex-B sink: ffmpeg->RTSP (go2rtc), stdout (pipe), or a dump file.
    ffmpeg_proc = None
    if RTSP_URL:
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer",
            "-f", "hevc", "-r", "25", "-i", "pipe:",
            "-c:v", "copy", "-rtsp_transport", "tcp", "-f", "rtsp", RTSP_URL,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        sink = ffmpeg_proc.stdin
        log(f"ffmpeg publishing H.265 -> {RTSP_URL}")
    elif STDOUT_MODE:
        sink = sys.stdout.buffer
    else:
        sink = dumpf

    def send_ptcs(buf):
        ch = chans.get("WebrtcDataChannel")
        try:
            if ch and ch.readyState == "open":
                ch.send(buf)
            else:
                log("!! cannot send PTCS, cmd DC not open")
        except Exception as e:
            log("send_ptcs err:", e)
    oracle.on_tx = send_ptcs

    def on_frame(link, buf):
        state["frames_seen"] += 1
        xz = buf[:4] == b"XZYH"
        cmdid = (buf[4] | (buf[5] << 8)) if (xz and len(buf) >= 6) else -1
        payload = buf[16:] if xz else buf
        if cmdid in (1300, 1301, 1303):                      # VIDEO: strip 16B XZYH + 22B media hdr -> Annex-B
            nal = payload[22:]
            state["vbytes"] += len(nal); state["vframes"] += 1
            try:
                sink.write(nal)
                if hasattr(sink, "flush"): sink.flush()   # StreamWriter (ffmpeg.stdin) has no flush()
            except Exception as e:
                log("sink write err:", e)
            if state["vframes"] <= 8 or state["vframes"] % 30 == 0:
                log(f"VIDEO #{state['vframes']} cmd={cmdid} link={link} payload={len(payload)} "
                    f"nal={payload[:8].hex()} total={state['vbytes']}")
            if state["vframes"] <= 20:
                framelog.write(json.dumps({"video": True, "cmd": cmdid, "link": link, "n": state["vframes"],
                                           "plen": len(payload), "nal": payload[:24].hex()}) + "\n"); framelog.flush()
        elif cmdid == 1032:                                  # per-channel status (do NOT ack)
            chid = buf[12] if len(buf) > 12 else -1
            val = int.from_bytes(payload[:4], "little", signed=True) if len(payload) >= 4 else None
            framelog.write(json.dumps({"status1032": True, "channel": chid, "val": val}) + "\n"); framelog.flush()
            n = state.get("st1032", 0)
            if n < 12:
                state["st1032"] = n + 1; log(f"status cmd1032 ch={chid} val={val}")
        else:                                                # control responses (1351 params, 1003/1004 acks, 9100)
            try: txt = payload.split(b"\x00")[0].decode("utf-8", "replace")
            except Exception: txt = ""
            if DISCOVER and '"dev_list"' in txt and not state["discovered"]:
                handle_devlist(txt)
            else:
                log(f"CTRL cmd={cmdid} link={link} len={len(buf)} {txt[:170]}")
            framelog.write(json.dumps({"ctrl": True, "cmd": cmdid, "link": link, "len": len(buf), "txt": txt[:600]}) + "\n"); framelog.flush()
    oracle.on_frame = on_frame

    def handle_devlist(txt):
        try:
            obj = json.loads(txt)
        except Exception as e:
            log("devlist parse err:", e); return
        dl = obj.get("payload", {}).get("dev_list") or obj.get("dev_list") or []
        cams = [{"channel": d.get("ch"), "name": d.get("name"), "sn": d.get("sn"),
                 "status": d.get("status"), "dev_type": d.get("dev_type")} for d in dl]
        manifest = {"nvr_sn": STATION_SN, "nvr_ip": state["nvr_ip"], "cameras": cams}
        out = CAMERAS_JSON
        json.dump(manifest, open(out, "w"), indent=2)
        log(f"DISCOVERED nvr_ip={state['nvr_ip']} sn={STATION_SN}: {len(cams)} camera(s)")
        for c in cams:
            log(f"   ch {c['channel']}: {c['name']!r} (sn {c['sn']}, status {c['status']})")
        log(f"wrote {out}")
        state["discovered"] = True

    async def heartbeat_loop():
        ping = build_ping()
        await asyncio.sleep(0.5)
        while True:
            ch = chans.get("WebrtcDataChannel")
            try:
                if ch and ch.readyState == "open":
                    ch.send(ping)
            except Exception as e:
                log("ping err:", e)
            await asyncio.sleep(10)

    async def start_sequence():
        if DISCOVER:
            dl = build_devicelist(USER_ID)
            log(f"-> getDeviceList (9100) len={len(dl)}  [auto-discovery]")
            oracle.push_send(1, dl)
            return
        # openLive (1103) param query, then the REAL trigger startStream (1003).
        ol = build_openlive(USER_ID, CHANNELS)
        log(f"-> openLive (1103) len={len(ol)} channels={CHANNELS}")
        oracle.push_send(1, ol)
        await asyncio.sleep(1.0)
        ss = build_startstream(USER_ID, CHANNELS, stream_id=1)
        log(f"-> startStream (1003) len={len(ss)} streamId=1  [THE video trigger]")
        oracle.push_send(1, ss)

    async def stats_loop():
        last = (0, 0)
        while True:
            await asyncio.sleep(4)
            cur = (state["ptcs_in"], state["vframes"])
            log(f"STATS ptcs_in={state['ptcs_in']} video={state['vframes']} vbytes={state['vbytes']} "
                f"frames_seen={state['frames_seen']}  (+{cur[0]-last[0]} pkts, +{cur[1]-last[1]} vid /4s)")
            last = cur

    def maybe_start():
        if state["connected"] and state["cmd_dc_open"] and not state["started"]:
            state["started"] = True
            log(f"connected+DC open; user_id={USER_ID[:8]}.. heartbeat on; starting sequence")
            asyncio.ensure_future(heartbeat_loop())
            asyncio.ensure_future(stats_loop())
            asyncio.ensure_future(start_sequence())

    def attach(ch, mine):
        @ch.on("open")
        def _open():
            log(f"DC open: {ch.label}")
            if ch.label == "WebrtcDataChannel":
                state["cmd_dc_open"] = True; maybe_start()
        @ch.on("message")
        def on_msg(msg):
            b = msg if isinstance(msg, (bytes, bytearray)) else str(msg).encode()
            b = bytes(b)
            if len(b) >= 4 and b[0] == 0x50 and b[1] == 0x54 and b[2] == 0x43 and b[3] == 0x53:  # "PTCS"
                state["ptcs_in"] += 1
                if state["ptcs_in"] <= 3:
                    log(f"PTCS in on {ch.label}: len={len(b)} head={b[:32].hex()}")
                oracle.push_recv(b)
            else:
                # heartbeat (1139 @ off24) or other; log a few
                if state["ptcs_in"] < 2:
                    log(f"non-PTCS on {ch.label}: len={len(b)} head={b[:28].hex()}")

    @pc.on("datachannel")
    def on_dc(ch): attach(ch, False)

    @pc.on("connectionstatechange")
    async def on_cs():
        log("connectionState:", pc.connectionState)
        if pc.connectionState == "connected":
            state["connected"] = True; maybe_start()
    @pc.on("iceconnectionstatechange")
    async def on_ice(): log("iceConnectionState:", pc.iceConnectionState)

    async with websockets.connect(WS_URL, subprotocols=["v1", subproto],
                                  additional_headers={"Origin": "https://security.eufy.com"},
                                  user_agent_header=UA, max_size=2**22) as ws:
        sig = Signal(ws, sign_token); log("WSS connected; joining..."); await sig.join()
        answered = [False]; pending = []

        async def add_cand(cand):
            try:
                p = cand.split()
                if len(p) > 7 and p[6] == "typ" and p[7] == "host" and p[4].startswith("192.168.1.") and not state["nvr_ip"]:
                    state["nvr_ip"] = p[4]   # the NVR's LAN IP (direct path) from its host ICE candidate
                c = candidate_from_sdp(cand.split(":", 1)[1]); c.sdpMid = "2"; c.sdpMLineIndex = 0
                await pc.addIceCandidate(c)
            except Exception as e: log("addIceCandidate err:", e)

        async def handle(raw):
            inner, d = parse_msg(raw)
            if not isinstance(inner, dict): return
            action = inner.get("action")
            if action == 1:
                log("join ack:", d); await sig.scall()
            elif action == 3 and isinstance(d, dict):
                if "turn" in d:
                    log("scall/turn status", d.get("status"))
                elif d.get("format") == "SDP":
                    val = d["value"]
                    if isinstance(val, str): val = json.loads(val)
                    log("NVR SDP offer received")
                    if not answered[0]:
                        answered[0] = True
                        offer = build_offer_sdp(val["ice"], val.get("setup", "actpass"))
                        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
                        for lbl in ["WebrtcDataChannel", "audio", "idr", "video", "notify", "download"]:
                            chans[lbl] = pc.createDataChannel(lbl); attach(chans[lbl], True)
                        ans = await pc.createAnswer(); await pc.setLocalDescription(ans)
                        uf, pw, fp, cands = extract_local_ice(pc.localDescription.sdp)
                        log(f"answer sent; our cands={len(cands)}")
                        await sig.ack(); await sig.send_sdp(uf, pw, fp, "active")
                        for c in cands: await sig.send_candidate(c)
                        for pcand in pending: await add_cand(pcand)
                        pending.clear()
                elif d.get("format") == "CANDIDATE":
                    cand = d["value"]
                    if not answered[0] or pc.remoteDescription is None: pending.append(cand)
                    else: await add_cand(cand)

        async def discover_watchdog():
            # Discovery completes on the DataChannel (handle_devlist via on_frame), NOT on the
            # signaling WS. After ICE settles the WS goes idle, so the `async for raw in ws`
            # below blocks forever and never re-checks state["discovered"]. Poll the flag and
            # close the WS to end the loop; cap the wait so run.sh's retry can take over.
            for _ in range(80):                      # ~40s ceiling
                if state["discovered"]:
                    break
                await asyncio.sleep(0.5)
            try:
                await ws.close()
            except Exception:
                pass

        if DISCOVER:
            asyncio.create_task(discover_watchdog())

        try:
            async for raw in ws:
                await handle(raw)
                if DISCOVER and state["discovered"]:
                    log("discovery done; stopping."); break
                if not STREAM_MODE and not DISCOVER and state["vframes"] > 600:
                    log("collected plenty of video; stopping."); break
        except Exception as e:
            log("ws loop err:", repr(e))

    await pc.close(); dumpf.close(); framelog.close()
    if oracle.proc:
        try: oracle.proc.terminate()
        except Exception: pass
    if ffmpeg_proc:
        try:
            ffmpeg_proc.stdin.close(); ffmpeg_proc.terminate()
        except Exception: pass
    log(f"DONE. video frames={state['vframes']} bytes={state['vbytes']} ptcs_in={state['ptcs_in']} "
        f"frames_seen={state['frames_seen']}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
