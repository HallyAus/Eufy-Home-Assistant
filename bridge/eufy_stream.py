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
import asyncio, json, time, uuid, os, sys, hashlib, re, base64, struct, ipaddress, signal

import aiohttp
import websockets
import eufy_cloud as ec
import stream_sensors
import keyframe_gate
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

# The eufy NVR's DTLS certificate has a signatureAlgorithm field whose encoding
# Python 'cryptography' (rust-asn1) rejects with "ExtraData in
# TbsCertificate::signature_alg", even though OpenSSL accepts it and the DTLS
# handshake completes. aiortc's peer-identity check reaches for the certificate
# via get_peer_certificate(as_cryptography=True), which triggers that strict
# parse and fails the connection right after ICE completes. Reimplement the same
# check the RFC 4572 way -- hash the exact DER OpenSSL negotiated and compare it
# to the remote SDP fingerprint -- so verification no longer depends on the
# strict ASN.1 parser. Security is unchanged: a non-matching fingerprint still
# fails the handshake.
import hashlib as _hashlib
from OpenSSL import crypto as _ossl_crypto
from aiortc import rtcdtlstransport as _dtls_mod

# Same algorithm set aiortc allows (RFC 8122): SHA-1 is deprecated and NOT accepted.
_PEER_DIGESTS = {"sha-256": _hashlib.sha256, "sha-384": _hashlib.sha384,
                 "sha-512": _hashlib.sha512}

def _validate_peer_identity_via_der(self, remoteParameters):
    # Relies on OpenSSL re-encoding the peer cert to the same DER the SDP
    # fingerprint was computed over (true for this NVR's cert). Same accept/reject
    # semantics as aiortc: every supported fingerprint must match, and there must
    # be at least one. Fails CLOSED on peer=None, empty/unknown-only fingerprints,
    # any mismatch, or any exception.
    try:
        peer = self._ssl.get_peer_certificate()  # OpenSSL X509, without strict ASN.1 parsing
        if peer is None:
            raise ValueError("peer certificate is missing")
        der = _ossl_crypto.dump_certificate(_ossl_crypto.FILETYPE_ASN1, peer)
        supported = valid = 0
        for f in remoteParameters.fingerprints:
            fn = _PEER_DIGESTS.get(f.algorithm.lower())
            if fn is None:
                continue
            supported += 1
            hx = fn(der).hexdigest().upper()
            digest = ":".join(hx[i:i + 2] for i in range(0, len(hx), 2))
            if f.value.upper() == digest:
                valid += 1
    except Exception:
        self._set_state(_dtls_mod.State.FAILED)
        print(f"[{time.strftime('%H:%M:%S')}] DTLS handshake failed (peer certificate unavailable)",
              flush=True, file=sys.stderr)
        return
    if not supported or valid != supported:
        self._set_state(_dtls_mod.State.FAILED)  # fail closed first, then diagnose
        print(f"[{time.strftime('%H:%M:%S')}] DTLS handshake failed (fingerprint mismatch)",
              flush=True, file=sys.stderr)
        return

# Fail loudly if this aiortc build lacks the private method we override, so a
# version bump can't silently fall back to the cryptography parser (which chokes on
# this NVR's cert). Patch is process-wide; the bridge only speaks DTLS to the NVR.
assert hasattr(_dtls_mod.RTCDtlsTransport, "_validate_peer_identity"), (
    "aiortc RTCDtlsTransport._validate_peer_identity is missing; pin a compatible aiortc"
)
_dtls_mod.RTCDtlsTransport._validate_peer_identity = _validate_peer_identity_via_der

ROOT = os.path.dirname(os.path.abspath(__file__))   # the bridge/ directory

def _lan_ipv4(ip):
    """True for a private (RFC 1918) IPv4 the NVR advertises as its LAN host ICE
    candidate. Works on ANY home subnet -- 10.x, 172.16-31.x, 192.168.x -- so a
    user is not tied to one hardcoded network."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_private and not addr.is_loopback and not addr.is_link_local

def _bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    local = os.path.join(ROOT, "bin", exe)
    return os.environ.get(name.upper(), local if os.path.exists(local) else name)

AUTH = json.load(open(os.environ.get("EUFY_AUTH", os.path.join(ROOT, "auth.json"))))
STATION_SN = os.environ.get("EUFY_STATION_SN") or AUTH.get("stationSn") or ""
REGION = os.environ.get("EUFY_REGION") or AUTH.get("region") or "US"

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
def parse_selection(argv):
    """Cameras requested by an engine argv: (channels, multi-view group or None).

    "g:<slot>-<channel>-<sensor>,..." requests one NVR-side multi-view (the official
    web viewer's stitch_mode 3): the NVR composites the cameras into a single video on
    a 3x3 grid, each tile placed by its slot (0-8, row-major).
    """
    carg = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else "0"
    if not carg.startswith("g:"):
        return ([0, 1, 2, 3] if carg == "all" else [int(x) for x in carg.split(",")]), None
    try:
        group = [tuple(int(v) for v in item.split("-")) for item in carg[2:].split(",") if item]
    except ValueError:
        raise SystemExit("group spec must look like g:0-0-1,1-1-1 (slot-channel-sensor)")
    if (not group or len(group) > 9 or any(len(t) != 3 for t in group)
            or len({t[0] for t in group}) != len(group)
            or not all(0 <= sl <= 8 and 0 <= ch <= 255 and 0 <= se <= 7 for sl, ch, se in group)):
        raise SystemExit("group spec needs 1-9 unique slots 0-8, channels 0-255, sensors 0-7")
    return list(dict.fromkeys(ch for _slot, ch, _sensor in group)), group


def parse_sensor(argv):
    """The explicit "--sensor N" lens selection, or None."""
    if "--sensor" not in argv:
        return None
    i = argv.index("--sensor")
    try:
        sensor = int(argv[i + 1])
    except (IndexError, ValueError):
        raise SystemExit("--sensor needs a number")
    if not 0 <= sensor <= 7:
        raise SystemExit("--sensor must be between 0 and 7")
    return sensor


def parse_rtsp(argv):
    """The "--rtsp <url>" publish target, or None."""
    if "--rtsp" not in argv:
        return None
    i = argv.index("--rtsp")
    if i + 1 >= len(argv):
        raise SystemExit("--rtsp needs a URL")
    return argv[i + 1]


CHANNELS, GROUP = parse_selection(sys.argv)

# Keep action3/control signaling anchored to the NVR primary channel (0).
# Requested camera selection remains in CHANNELS/openLive/startStream.
SIGNALING_CHANNEL = 0
CALL_TYPE = os.environ.get("EUFY_CALL_TYPE", "call").strip().lower()
if CALL_TYPE not in ("call", "scall"):
    raise SystemExit("EUFY_CALL_TYPE must be 'call' or 'scall'")
# --rtsp <url>: publish the H.265 stream to that RTSP url via ffmpeg (go2rtc exec {output} mode).
RTSP_URL = parse_rtsp(sys.argv)
STREAM_MODE = bool(RTSP_URL) or os.environ.get("EUFY_STDOUT") == "1"   # run indefinitely, no frame-count stop
_rest = [a for a in sys.argv[2:] if a not in ("--rtsp", RTSP_URL)]
# Dual-lens cameras (for example S4 Max PoE): "--sensor N" selects the lens for this
# stream. Sensor 1 stays the default; on the S4 Max it is the PTZ lens and 0 the fixed lens.
SENSOR = parse_sensor(sys.argv)
if SENSOR is not None:
    _i = sys.argv.index("--sensor")
    _rest = [a for a in _rest if a not in sys.argv[_i:_i + 2]]
RUN_SECS = int(_rest[0]) if _rest and _rest[0].isdigit() else (10**9 if STREAM_MODE else 70)
NODE = _bin("node")
ORACLE = os.path.join(ROOT, "sctp_oracle.js")
FFMPEG = _bin("ffmpeg")

WS_URL, SIGN_URL = ec.smart_urls(STATION_SN, REGION)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
HEADERS = {
    "x-auth-token": AUTH["authToken"], "gtoken": AUTH["gtoken"],
    "app-name": AUTH.get("appName", "eufy_mega"), "model-type": "WEB",
    "web-country": AUTH.get("webCountry", "US"),
    "accept": "application/json, text/plain, */*", "user-agent": UA,
    "origin": "https://security.eufy.com", "referer": "https://security.eufy.com/",
}
VIDEO_DUMP = os.path.join(ROOT, "_debug", "video_dump.bin")
FRAMES_LOG = os.path.join(ROOT, "_debug", "frames.jsonl")
CAPTURE_DEBUG = os.environ.get("EUFY_CAPTURE_DEBUG") == "1"
# Discovery manifest. Like EUFY_AUTH, the add-on points this at /data so the
# discovered camera list survives a container restart/rebuild and gen_go2rtc.py
# reads it from the exact same place; default to the bridge dir for standalone runs.
CAMERAS_JSON = os.environ.get("EUFY_CAMERAS", os.path.join(ROOT, "cameras.json"))
# Per-camera startStream sensor: single-lens PoE Cam E40 (T8P00) only streams on
# sensor 0; EUFY_STREAM_SENSORS ("channel:sensor,...") overrides any channel.
try:
    _SENSOR_OVERRIDES = stream_sensors.parse_stream_sensors(os.environ.get("EUFY_STREAM_SENSORS", ""))
except ValueError as _err:
    raise SystemExit(str(_err))
def stream_sensor_map(channels, sensor):
    """startStream sensor per channel; an explicit --sensor (extra-lens stream) wins."""
    if sensor is not None:
        return {c: sensor for c in channels}
    return stream_sensors.sensors_for_channels(
        channels, stream_sensors.load_serials(CAMERAS_JSON), _SENSOR_OVERRIDES)


STREAM_SENSORS = stream_sensor_map(CHANNELS, SENSOR)

# Session reuse: when a viewer leaves, keep the NVR WebRTC session open this many
# seconds (0 = off). The next camera request is handed to it over a local socket and
# switches with closeLive (1004) + openLive/startStream, as the official web viewer
# does, instead of a new signaling/ICE/DTLS handshake.
REUSE_SECONDS = max(0, min(int(os.environ.get("EUFY_SESSION_REUSE_SECONDS", "0") or 0), 300))
SESSION_SOCKET = os.environ.get("EUFY_SESSION_SOCKET", "/tmp/eufy_nvr_session.sock")
REUSE_FIRST_VIDEO_TIMEOUT = 10.0
REUSE_STALL_TIMEOUT = 20.0
STDOUT_MODE = os.environ.get("EUFY_STDOUT") == "1"   # write clean Annex-B to stdout (for go2rtc/ffmpeg exec source)
def now(): return int(time.time())
def signaling_account(channel, user_id, timestamp):
    """Return the account proof used by the current official NVR web client."""
    return hashlib.md5(f"{channel}{user_id}{timestamp}".encode()).hexdigest()
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

def build_startstream(user_id, channels, stream_id=1, sensors=None):
    # cmd 1003 (ic / startStream) — the ACTUAL live-video trigger (cmd 1103 is only a param query).
    if GROUP:
        # Same request the official web viewer sends for its multi-camera grid.
        payload = json.dumps({
            "account_id": user_id, "cmd": 1003,
            "payload": {"ClientOS": "WEB", "entrytype": 0, "camera_type": 0, "key": "",
                        "msg_id": 116, "audio_chn": -1, "streamtype": 2, "stitch_mode": 3,
                        "chn_list": [{"index": slot, "chn": ch, "sensor": sensor, "isUps": 1,
                                      "isClicked": True} for slot, ch, sensor in GROUP],
                        "pip_cord": {"x1": 0, "y1": 0, "x2": 0, "y2": 0},
                        "station_video_type": 6, "play_id": stream_id},
        }, separators=(",", ":")).encode()
    else:
        # sensors maps channel -> sensor index; unlisted channels keep the historical sensor 1.
        sensors = sensors or {}
        chn_list = [{"index": i, "chn": c, "sensor": sensors.get(c, stream_sensors.DEFAULT_SENSOR)}
                    for i, c in enumerate(channels)]
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
            try:
                body = await r.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                log("ws/sign status", r.status, "returned a non-JSON response")
                raise RuntimeError("ws/sign returned an invalid response") from exc

            code = body.get("code") if isinstance(body, dict) else None
            log("ws/sign status", r.status, "code", code)
            sign_token = body.get("data") if isinstance(body, dict) else None
            if r.status != 200 or not isinstance(sign_token, str) or not sign_token:
                raise RuntimeError(
                    "ws/sign rejected the current auth session; refresh auth.json or restart the add-on"
                )
            return sign_token

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
    def __init__(self, ws, sid, auth_token, user_id, channel, call_type):
        self.ws = ws; self.sid = sid; self.auth_token = auth_token
        self.user_id = user_id; self.channel = channel; self.call_type = call_type
    async def send(self, inner, *, join=False):
        msgid = "0" if join else f"{self.auth_token}_{uuid.uuid4()}"
        await self.ws.send(json.dumps({"msgid": msgid, "data": json.dumps(inner)}))
    async def join(self):
        await self.send({"code": 200, "action": 1, "data": self.sid, "sn": STATION_SN, "source": "WEB", "ts": now()}, join=True)
    async def action3(self, dt, obj):
        timestamp = now()
        data = {
            "timestamp": timestamp,
            "account": signaling_account(self.channel, self.user_id, timestamp),
            **obj,
        }
        await self.send({"code": 200, "action": 3, "sessionId": self.sid, "sn": STATION_SN, "subSn": "",
                         "channelId": self.channel, "isResponse": 0, "dataType": dt, "source": "WEB", "ts": timestamp,
                         "data": json.dumps(data)})
    async def call(self): await self.action3(self.call_type, {})
    async def ack(self): await self.action3("ack", {})
    async def send_sdp(self, native_sdp, uf, pw, fp, setup="active"):
        if self.call_type == "scall":
            compact = {"ice": {"ufrag": uf, "pwd": pw, "fingerprint": fp.upper(),
                               "fingerprint_type": "sha-256"}, "setup": setup}
            value = json.dumps(compact)
        else:
            value = native_sdp
        await self.action3("info", {"sdp": value})
    async def send_candidate(self, c):
        await self.action3("info", {"candidate": c})

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
        self.on_control_status = None  # callback(status:int) for fixed replies

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
            elif ev == "control_status":
                if self.on_control_status: self.on_control_status(msg.get("status"))
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
    perf_started = time.monotonic()
    oracle = Oracle()
    sign_task = asyncio.create_task(get_sign_token())
    await oracle.start()
    sign_token = await sign_task
    log("sign token acquired; channels", CHANNELS)
    log(f"PERF bootstrap_ms={int((time.monotonic() - perf_started) * 1000)}")

    sub = {"region": ec.signaling_region(REGION), "type": "NVR", "sn": STATION_SN,
           "token": AUTH["authToken"], "gtoken": AUTH["gtoken"], "sign": sign_token,
           "appName": AUTH.get("appName", "eufy_mega"), "modelType": "WEB"}
    subproto = base64.urlsafe_b64encode(json.dumps(sub, separators=(",", ":")).encode()).decode().rstrip("=")

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    chans = {}
    state = {"connected": False, "started": False, "vbytes": 0, "vframes": 0, "ptcs_in": 0,
             "cmd_dc_open": False, "frames_seen": 0, "nvr_ip": None, "discovered": False,
             "fatal_reason": None, "shutting_down": False, "close_sent": False}
    close_ack = asyncio.Event()
    video_ready = asyncio.Event()   # set once the current camera's first frame reaches the sink

    def on_control_status(status):
        if state["close_sent"] and status == 0:
            close_ack.set()

    oracle.on_control_status = on_control_status
    if CAPTURE_DEBUG:
        os.makedirs(os.path.join(ROOT, "_debug"), exist_ok=True)
        dumpf = open(VIDEO_DUMP, "wb")
        framelog = open(FRAMES_LOG, "w")
    else:
        dumpf = open(os.devnull, "wb")
        framelog = open(os.devnull, "w")

    # The Annex-B sink: ffmpeg->RTSP (go2rtc), stdout (pipe), or a dump file. Held in
    # io so a reused session (EUFY_SESSION_REUSE_SECONDS) can switch it per camera.
    io = {"ffmpeg": None, "sink": dumpf, "gate": None}
    reuse_enabled = bool(REUSE_SECONDS and RTSP_URL and not DISCOVER and os.name != "nt")

    def new_gate():
        # Drop the NVR's start-up placeholder keyframe and feed the first real
        # keyframe twice (ffmpeg's probe swallows one copy); see keyframe_gate.py.
        return keyframe_gate.KeyframeGate(
            duplicate_first=os.environ.get("EUFY_DUP_FIRST_KEYFRAME", "1") != "0", log=log)

    async def open_sink():
        if RTSP_URL:
            # Transcode HEVC -> H.264 so Home Assistant / browsers can render the LIVE view.
            # (H.265 only shows the still thumbnail; most browsers can't play it live.) Set
            # EUFY_VIDEO_COPY=1 to passthrough raw H.265 instead (lower CPU, thumbnail only).
            if os.environ.get("EUFY_VIDEO_COPY") == "1":
                vcodec = ["-c:v", "copy"]
                codec_label = "H.265 (copy)"
            else:
                # short GOP so a NEW consumer (HA opening live view) gets an IDR fast instead of
                # waiting a full GOP. The feed runs <25fps, so g=12 => a keyframe every ~0.5-0.8s
                # (g=25 was ~1.5-2s of keyframe-wait per open). zerolatency drops B-frames.
                vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                          "-pix_fmt", "yuv420p", "-g", "12", "-keyint_min", "12", "-sc_threshold", "0"]
                codec_label = "H.264 (transcoded)"
                if GROUP:
                    # The NVR always sends the full 3x3 grid; keep only the rows/columns in use.
                    cols = max(slot % 3 for slot, _ch, _sensor in GROUP) + 1
                    rows = max(slot // 3 for slot, _ch, _sensor in GROUP) + 1
                    if (cols, rows) != (3, 3):
                        vcodec = ["-vf", f"crop=iw*{cols}/3:ih*{rows}/3:0:0"] + vcodec
            io["ffmpeg"] = await asyncio.create_subprocess_exec(
                FFMPEG, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer",
                # The input format is known, so probing only delays go2rtc publication.
                # HA's camera proxy has a hard 10-second image timeout and the NVR's
                # WebRTC handshake already consumes most of it on a cold start.
                "-probesize", "32", "-analyzeduration", "0",
                "-f", "hevc", "-r", "25", "-i", "pipe:",
                *vcodec, "-rtsp_transport", "tcp", "-f", "rtsp", RTSP_URL,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            io["sink"] = io["ffmpeg"].stdin
            # RTSP_URL may contain the local go2rtc password; never print it.
            log(f"ffmpeg publishing {codec_label} to the configured RTSP endpoint")
        elif STDOUT_MODE:
            io["sink"] = sys.stdout.buffer
        else:
            io["sink"] = dumpf
        io["gate"] = new_gate()

    async def close_sink(proc):
        """Stop one ffmpeg publisher (go2rtc sees its RTSP producer end)."""
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except ProcessLookupError:
            pass

    await open_sink()

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
            ffmpeg_proc = io["ffmpeg"]
            if ffmpeg_proc is not None and ffmpeg_proc.returncode is not None:
                if reuse_enabled:
                    return          # ffmpeg_watchdog parks or ends the session
                if state["fatal_reason"] is None:
                    state["fatal_reason"] = f"ffmpeg exited rc={ffmpeg_proc.returncode}"
                    log(f"FFMPEG_EXIT rc={ffmpeg_proc.returncode}")
                return
            nal = payload[22:]
            state["vbytes"] += len(nal); state["vframes"] += 1
            try:
                sink = io["sink"]
                passed = io["gate"].push(nal, keyframe_gate.frame_size(payload[:22]))
                for _out in passed:
                    sink.write(_out)
                if hasattr(sink, "flush"): sink.flush()   # StreamWriter (ffmpeg.stdin) has no flush()
                if passed and not video_ready.is_set():
                    video_ready.set()
            except Exception as e:
                state["fatal_reason"] = f"video sink failed ({type(e).__name__})"
                log("sink write err:", e)
                return
            progress_now = time.monotonic()
            if state["vframes"] == 1:
                state["last_video_log"] = progress_now
                log(f"VIDEO #{state['vframes']} cmd={cmdid} link={link} payload={len(payload)} "
                    f"nal={payload[:8].hex()} total={state['vbytes']}")
                log(f"PERF first_video_ms={int((progress_now - perf_started) * 1000)}")
            elif progress_now - state.get("last_video_log", 0.0) >= 5.0:
                state["last_video_log"] = progress_now
                log(f"VIDEO_PROGRESS frames={state['vframes']} bytes={state['vbytes']}")
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
        # Write-then-rename (like auth_login.py) so gen_go2rtc.py, or a fallback that
        # reuses the persisted /data copy, never sees a half-written manifest.
        tmp = out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(tmp, out)
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
        # The NVR acknowledges openLive in tens of milliseconds. A short guard
        # is sufficient and keeps cold starts inside HA's image timeout.
        await asyncio.sleep(0.15)
        ss = build_startstream(USER_ID, CHANNELS, stream_id=1, sensors=STREAM_SENSORS)
        log(f"-> startStream (1003) len={len(ss)} streamId=1 sensors={STREAM_SENSORS}  [THE video trigger]")
        oracle.push_send(1, ss)

    async def stats_loop():
        last = (0, 0)
        while True:
            await asyncio.sleep(4)
            cur = (state["ptcs_in"], state["vframes"])
            log(f"STATS ptcs_in={state['ptcs_in']} video={state['vframes']} vbytes={state['vbytes']} "
                f"frames_seen={state['frames_seen']}  (+{cur[0]-last[0]} pkts, +{cur[1]-last[1]} vid /4s)")
            last = cur

    async def close_live():
        """Tell the NVR to retire its control/live session before WebRTC closes."""
        channel = chans.get("WebrtcDataChannel")
        if (
            state["close_sent"]
            or not state["started"]
            or channel is None
            or channel.readyState != "open"
        ):
            return
        state["close_sent"] = True
        try:
            close = build_cmd(USER_ID, 1004, {})
            log(f"-> closeLive (1004) len={len(close)}")
            oracle.push_send(1, close)
            # Keep the data channel alive until the NVR acknowledges teardown.
            # A bounded fallback still lets shutdown finish if the ack is lost.
            try:
                await asyncio.wait_for(close_ack.wait(), timeout=1.0)
                log("<- closeLive acknowledged")
            except asyncio.TimeoutError:
                log("closeLive acknowledgement timed out; closing transport")
        except Exception as error:
            log("closeLive send failed:", type(error).__name__)

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
        sig = Signal(ws, sign_token, AUTH["authToken"], USER_ID, SIGNALING_CHANNEL, CALL_TYPE)
        log(f"WSS connected; joining with {CALL_TYPE} signaling..."); await sig.join()
        answered = [False]; pending = []
        ffmpeg_watchdog_task = None

        async def ffmpeg_watchdog(proc):
            rc = await proc.wait()
            # A publisher retired on purpose (camera switch) is no longer io["ffmpeg"].
            if not state["shutting_down"] and io["ffmpeg"] is proc:
                # go2rtc closes the publisher's RTSP session when the last viewer
                # leaves, often before our supervisor's stop signal arrives. With
                # session reuse that is the cue to park, not a failure.
                if reuse_enabled and video_ready.is_set() and not state["fatal_reason"]:
                    if await park("publisher closed"):
                        return
                    if state["shutting_down"]:
                        return
                state["fatal_reason"] = f"ffmpeg exited rc={rc}"
                log(f"FFMPEG_EXIT rc={rc}")
                await ws.close()

        if io["ffmpeg"] is not None:
            ffmpeg_watchdog_task = asyncio.create_task(ffmpeg_watchdog(io["ffmpeg"]))

        # ---- session reuse (EUFY_SESSION_REUSE_SECONDS) ------------------------------
        reuse = {"parked": False, "client": None, "idle": None, "server": None,
                 "detached": False, "lock": asyncio.Lock()}

        def detach_from_supervisor():
            """Keep logging after the supervisor that started this engine exits.

            Closing our end of its stderr pipe is also the supervisor's signal that
            the session is parked and it may exit without tearing anything down.
            """
            if reuse["detached"]:
                return
            reuse["detached"] = True
            try:
                fd = os.open("/proc/1/fd/1", os.O_WRONLY)
            except OSError:
                fd = os.open(os.devnull, os.O_WRONLY)
            sys.stdout.flush(); sys.stderr.flush()
            os.dup2(fd, 1); os.dup2(fd, 2); os.close(fd)

        async def stop_current_camera():
            """closeLive (1004) for the current camera; the WebRTC session stays up."""
            channel = chans.get("WebrtcDataChannel")
            if channel is None or channel.readyState != "open":
                return False
            close_ack.clear()
            state["close_sent"] = True
            try:
                oracle.push_send(1, build_cmd(USER_ID, 1004, {}))
                log("-> closeLive (1004) [session kept for reuse]")
                try:
                    await asyncio.wait_for(close_ack.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    log("closeLive acknowledgement timed out")
            finally:
                state["close_sent"] = False
            return True

        async def idle_expire():
            await asyncio.sleep(REUSE_SECONDS)
            if reuse["parked"] and not state["shutting_down"]:
                log(f"SESSION reuse window ({REUSE_SECONDS}s) ended; closing the NVR session")
                await ws.close()

        async def park(reason):
            async with reuse["lock"]:
                if (state["shutting_down"] or state["fatal_reason"] or reuse["parked"]
                        or not state["started"] or not video_ready.is_set()):
                    return False
                detach_from_supervisor()
                reuse["client"] = None
                if not await stop_current_camera():
                    # Detached but unable to park: end the session ourselves.
                    log("SESSION could not be parked; closing the NVR session")
                    asyncio.ensure_future(ws.close())
                    return False
                proc, io["ffmpeg"] = io["ffmpeg"], None
                io["sink"], io["gate"] = dumpf, new_gate()
                video_ready.clear()
                await close_sink(proc)
                if reuse["server"] is None:
                    try:
                        os.unlink(SESSION_SOCKET)
                    except FileNotFoundError:
                        pass
                    reuse["server"] = await asyncio.start_unix_server(handle_take, path=SESSION_SOCKET)
                reuse["parked"] = True
                reuse["idle"] = asyncio.create_task(idle_expire())
                log(f"SESSION parked ({reason}); reusable for {REUSE_SECONDS}s")
                return True

        async def reply(writer, obj):
            try:
                writer.write((json.dumps(obj) + "\n").encode())
                await writer.drain()
            except Exception:
                pass

        async def watch_stall(writer):
            while reuse["client"] is writer and not state["shutting_down"]:
                before = state["vframes"]
                await asyncio.sleep(REUSE_STALL_TIMEOUT)
                if reuse["client"] is writer and state["vframes"] == before:
                    state["fatal_reason"] = "video stalled on a reused session"
                    log("SESSION video stalled; closing the NVR session")
                    await ws.close()
                    return

        async def handle_take(reader, writer):
            global CHANNELS, GROUP, SENSOR, RTSP_URL, STREAM_SENSORS
            nonlocal ffmpeg_watchdog_task
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), timeout=3))
                argv = ["eufy_stream.py"] + [str(a) for a in request["argv"]]
            except Exception:
                writer.close()
                return
            async with reuse["lock"]:
                if not reuse["parked"] or state["shutting_down"] or state["fatal_reason"]:
                    await reply(writer, {"ok": False, "reason": "busy"})
                    writer.close()
                    return
                try:
                    channels, group = parse_selection(argv)
                    sensor = parse_sensor(argv)
                    rtsp_url = parse_rtsp(argv)
                    if not rtsp_url:
                        raise SystemExit("--rtsp is required")
                except (SystemExit, ValueError) as error:
                    await reply(writer, {"ok": False, "reason": str(error)[:120]})
                    writer.close()
                    return
                reuse["parked"] = False
                if reuse["idle"] is not None:
                    reuse["idle"].cancel()
                CHANNELS, GROUP, SENSOR, RTSP_URL = channels, group, sensor, rtsp_url
                STREAM_SENSORS = stream_sensor_map(channels, sensor)
                reuse["client"] = writer
                video_ready.clear()
                took_at = time.monotonic()
                await open_sink()
                if io["ffmpeg"] is not None:
                    ffmpeg_watchdog_task = asyncio.create_task(ffmpeg_watchdog(io["ffmpeg"]))
                log(f"SESSION reused for channels {CHANNELS}")
                await start_sequence()
            try:
                await asyncio.wait_for(video_ready.wait(), timeout=REUSE_FIRST_VIDEO_TIMEOUT)
            except asyncio.TimeoutError:
                state["fatal_reason"] = "no video after a reused-session camera switch"
                log("SESSION switch produced no video; closing the NVR session")
                await reply(writer, {"ok": False, "reason": "no video"})
                writer.close()
                await ws.close()
                return
            log(f"PERF reuse_first_video_ms={int((time.monotonic() - took_at) * 1000)}")
            await reply(writer, {"ok": True})
            stall = asyncio.create_task(watch_stall(writer))
            try:
                await reader.read()          # the supervisor holds the socket while viewed
            except Exception:
                pass
            stall.cancel()
            if reuse["client"] is writer and not state["shutting_down"]:
                if not await park("viewer left"):
                    await ws.close()
            writer.close()

        if reuse_enabled:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGUSR1, lambda: asyncio.ensure_future(park("supervisor handoff")))

        async def add_cand(cand):
            try:
                p = cand.split()
                if len(p) > 7 and p[6] == "typ" and p[7] == "host" and _lan_ipv4(p[4]) and not state["nvr_ip"]:
                    state["nvr_ip"] = p[4]   # the NVR's LAN IP (direct path) from its host ICE candidate
                c = candidate_from_sdp(cand.split(":", 1)[1]); c.sdpMid = "2"; c.sdpMLineIndex = 0
                await pc.addIceCandidate(c)
            except Exception as e: log("addIceCandidate err:", e)

        async def handle(raw):
            inner, d = parse_msg(raw)
            if not isinstance(inner, dict): return
            action = inner.get("action")
            if action == 1:
                log("join ack:", d); await sig.call()
            elif action == 3 and isinstance(d, dict):
                if inner.get("dataType") in ("call", "scall") and "status" in d:
                    status = d.get("status")
                    log("scall/turn status", status, f"mode={sig.call_type}")
                    if status == 200:
                        await sig.ack()
                elif d.get("format") == "SDP":
                    val = d["value"]
                    compact = None
                    if isinstance(val, str):
                        try:
                            compact = json.loads(val)
                        except json.JSONDecodeError:
                            pass
                    elif isinstance(val, dict):
                        compact = val
                    if isinstance(compact, dict) and isinstance(compact.get("ice"), dict):
                        offer = build_offer_sdp(compact["ice"], compact.get("setup", "actpass"))
                        offer_mode = "compact"
                    elif isinstance(val, str) and val.lstrip().startswith("v=0"):
                        offer = val
                        offer_mode = "native"
                    else:
                        state["fatal_reason"] = "invalid SDP offer"
                        log("invalid SDP offer received")
                        await ws.close()
                        return
                    log(f"NVR SDP offer received ({offer_mode})")
                    if not answered[0]:
                        answered[0] = True
                        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
                        for lbl in ["WebrtcDataChannel", "audio", "idr", "video", "notify", "download"]:
                            chans[lbl] = pc.createDataChannel(lbl); attach(chans[lbl], True)
                        ans = await pc.createAnswer(); await pc.setLocalDescription(ans)
                        uf, pw, fp, cands = extract_local_ice(pc.localDescription.sdp)
                        _allow = [a.strip() for a in os.environ.get("EUFY_ICE_ALLOW", "").split(",") if a.strip()]
                        if _allow:
                            # Only advertise LAN candidates: docker/hassio/VPN interfaces on the host
                            # give the NVR unreachable pairs and ICE can sit in "checking".
                            _kept = [c for c in cands if len(c.split()) > 4 and any(c.split()[4].startswith(a) for a in _allow)]
                            log(f"ICE candidates: advertising {len(_kept)}/{len(cands)} "
                                f"({','.join(sorted({c.split()[4] for c in _kept}))})")
                            if _kept:
                                cands = _kept
                        log(f"answer sent; our cands={len(cands)}")
                        await sig.send_sdp(pc.localDescription.sdp, uf, pw, fp, "active")
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
        finally:
            state["shutting_down"] = True

            async def _graceful_close():
                await close_live()
                try:
                    await asyncio.wait_for(pc.close(), timeout=2.0)
                    log("peer connection closed cleanly")
                except Exception as error:
                    log("peer connection close failed:", type(error).__name__)

            # ffmpeg exiting and go2rtc's SIGINT often arrive together. The second
            # cancellation used to abort closeLive/pc.close half way, leaving the NVR
            # to time the call out (~60 s of scall/turn 486 for every camera).
            # Finish the teardown even if this task is cancelled again meanwhile.
            _closer = asyncio.ensure_future(_graceful_close())
            _interrupted = False
            while not _closer.done():
                try:
                    await asyncio.shield(_closer)
                except asyncio.CancelledError:
                    _interrupted = True
            if _interrupted:
                log("teardown completed despite cancellation")
            if ffmpeg_watchdog_task is not None and not ffmpeg_watchdog_task.done():
                ffmpeg_watchdog_task.cancel()
                await asyncio.gather(ffmpeg_watchdog_task, return_exceptions=True)

    await pc.close(); dumpf.close(); framelog.close()
    if oracle.proc:
        try:
            oracle.proc.terminate()
            await asyncio.wait_for(oracle.proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            oracle.proc.kill()
            await oracle.proc.wait()
        except ProcessLookupError:
            pass
    await close_sink(io["ffmpeg"])
    if REUSE_SECONDS:
        try:
            os.unlink(SESSION_SOCKET)
        except OSError:
            pass
    log(f"DONE. video frames={state['vframes']} bytes={state['vbytes']} ptcs_in={state['ptcs_in']} "
        f"frames_seen={state['frames_seen']}")
    # In --discover mode only a written manifest counts as success. Returning this lets
    # the caller (the add-on run.sh) tell a real discovery from one that merely timed
    # out — otherwise it logs "Discovery OK" and gen_go2rtc.py dies on a missing file.
    return ((not DISCOVER) or state["discovered"]) and not state["fatal_reason"]

if __name__ == "__main__":
    try:
        ok = asyncio.run(main())
    except KeyboardInterrupt:
        ok = True
    if not ok:
        log("discovery finished without a camera list; cameras.json not written")
        sys.exit(1)
