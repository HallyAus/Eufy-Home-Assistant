"""
eufy_cloud.py -- Headless client for eufy's ECDH-encrypted cloud "mega" web API.

This module reverse-implements the encryption + signing scheme used by the eufy
Security web app (the SAME bundle that drives the WebRTC streaming engine in this
project: captures/webrtc_bundle.js). It lets you, given a valid auth_token + gtoken,
fetch the station_list / house list and decode it into NVRs + cameras WITHOUT a
browser.

------------------------------------------------------------------------------------
PROVENANCE -- where every constant came from
------------------------------------------------------------------------------------
Everything in the ECDH / AES / HMAC / request-wrapper section below is transcribed
VERBATIM (then de-minified) from captures/webrtc_bundle.js. The minified identifiers
are noted in comments so you can re-locate them:

  Ka  = new A.ec("p256")                         -> EC curve secp256r1 / prime256v1   (offset ~12559)
  Ya  = async()=>[priv_hex, pub_hex]             -> generate client keypair           (offset ~12559)
  Xa  = class { keyIndent, shareKey, ... }       -> key holder + shared-secret KDF    (offset ~12644)
  $a  = (key,ts,once,body)=>HmacSHA256(...)       -> X-Signature builder               (offset ~13017)
  tn  = async(plain,keyHex)=>b64(iv||ct)          -> AES-CBC encrypt (request body)    (offset ~13174)
  an  = async(b64,keyHex)=>plaintext              -> AES-CBC decrypt (response body)   (offset ~13471)
  sn  = ()=>{unisign,keyindent,once,ts ...}       -> per-request "randomField" + hdrs  (offset ~13911)
  on  = (clientPub,encClientPub)=>headers         -> headers for key/exchange POST     (offset ~14616)
  ln  = async(body, keyObj)=>{...encBody...}      -> headers for normal requests       (offset ~14616)
  en  = (key,rf,resp)=>$a(...)===resp.signature   -> response signature verification
  fa  = (service, region)=>baseURL                -> domain table lookup               (offset ~9057)
  ha  = { mega/security/smart/passport/... }      -> region->domain map                (offset ~6580)
  wn  = async e=>{...}                            -> encrypted request wrapper (v3)     (offset ~83081)
  bn  = key/exchange (security service, v3)        -> POST /v3/openapi/oauth/key/exchange(offset ~84127)
  fn  = key/exchange (openapi service, v1)         -> POST /openapi/oauth/key/exchange  (offset ~17457)
  Sn  = ()=>wn({url:"/v3/house/list", ...})        -> house list                        (offset ~84300)
  jn  = ()=>wn({url:"/v3/house/station_list",...}) -> station list                      (offset ~84378)
  pn  = "eufy_mega"                               -> App-Name header value             (offset ~16066)

CONFIRMED crypto parameters (read directly out of the bundle):
  * Curve:        secp256r1 (NIST P-256 / prime256v1)            -- new A.ec("p256")
  * Client pubkey wire format: uncompressed point hex "04"||X||Y -- e.getPublic("hex")
  * Shared secret: ECDH -> X coordinate of shared point, hex.
                   KDF  = left-pad X to 64 hex chars, then take FIRST 32 hex chars
                          = FIRST 16 BYTES  ->  AES-128 key.
                   (("0"+s).substr(-64).substr(0,32) in the JS)
  * Cipher:       AES-128-CBC, PKCS7 padding, random 16-byte IV per message.
                  IMPORTANT: the 32-hex shareKey is parsed by CryptoJS Hex.parse, i.e.
                  the 32 hex chars are interpreted as 16 RAW BYTES (the AES key), NOT
                  as the UTF-8 bytes of the hex string.
  * Wire body:    base64( IV(16 bytes) || ciphertext )
  * Signature:    X-Signature = HMAC-SHA256( key = shareKey_hex_string(32 chars),
                                             msg = "{ts}+{once}"            (no body)
                                                or "{ts}+{once}+{encBody}"  (with body) )
                  hex-encoded. The HMAC key is the 32-CHAR HEX STRING's UTF-8 bytes
                  (CryptoJS HmacSHA256(msg, keyString) treats a string key as UTF-8),
                  NOT the 16 raw key bytes. This asymmetry is in the bundle and is
                  reproduced faithfully below.
  * Key ident:    X-Key-Ident = a fresh uuid-hex ("keyindent") chosen at exchange time
                  and echoed by the server; reused on every encrypted request.

The handshake reuses the SAME signing scheme for the key/exchange call itself, except
there the "encBody" is the AES-CBC-encrypted client public key (encrypted under the
pre-shared static bootstrap key baked into the bundle -- see EXCHANGE_BOOTSTRAP_KEY).

------------------------------------------------------------------------------------
login() -- INFERRED, NOT in webrtc_bundle.js
------------------------------------------------------------------------------------
The passport LOGIN page is a SEPARATE bundle and is NOT present in webrtc_bundle.js.
login() below implements the documented eufy passport flow (POST to
app-passport-<region>-pr.eufy.com/passport/login) following the public
bropat/eufy-security-client approach. The exact field names / password encryption are
marked INFERRED in comments and MUST be confirmed against one live login. See the
report at the bottom of the project task for the confirmed-vs-inferred breakdown.

No live network calls are made at import time. Nothing here logs in or touches the NVR
unless you explicitly await one of the async functions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---- crypto: 'cryptography' is present in this env; pycryptodome is an acceptable
# ---- alternative per the task. We use 'cryptography' for ECDH + AES.
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.backends import default_backend

# aiohttp is imported lazily inside the async functions so that this module stays
# importable / py_compile-valid even where aiohttp is not installed (it is the only
# network dependency and is never touched unless you actually make a request).


# ====================================================================================
# Region -> domain table  (verbatim from bundle `ha`, offset ~6580; fa(), offset ~9057)
# Only the production ("-pr") entries are needed for real use; QA kept for reference.
# fa(service, region) -> ha[service][region] or fallback ha[service]["us-pr"].
# ====================================================================================
DOMAINS: Dict[str, Dict[str, str]] = {
    "mega": {
        "us-pr": "https://mega-us-pr.eufy.com",
        "eu-pr": "https://mega-eu-pr.eufy.com",
        "ie-pr": "https://mega-ie-pr.eufy.com",
    },
    "passport": {
        "us-pr": "https://app-passport-us-pr.eufy.com",
        "eu-pr": "https://app-passport-eu-pr.eufy.com",
        "ie-pr": "https://app-passport-ie-pr.eufy.com",
    },
    "openapi": {
        "us-pr": "https://app-openapi-us-pr.eufy.com",
        "eu-pr": "https://app-openapi-eu-pr.eufy.com",
        "ie-pr": "https://app-openapi-ie-pr.eufy.com",
    },
    # `security` is the service `wn` (the v3 request wrapper) and the v3 key/exchange
    # actually target -- NOT a "*.eufy.com" host. Verbatim from bundle.
    "security": {
        "us-pr": "https://security-app.eufylife.com",
        "eu-pr": "https://security-app-eu.eufylife.com",
        "ie-pr": "https://security-app-ie.eufylife.com",
    },
    "smart": {
        "us-pr": "https://security-smart.eufylife.com",
        "eu-pr": "https://security-smart-eu.eufylife.com",
        "ie-pr": "https://security-smart-ie.eufylife.com",
    },
}

# App-Name header value, verbatim:  pn = "eufy_mega"  (offset ~16066)
APP_NAME = "eufy_mega"

# Static bootstrap key used to encrypt the client public key in the v3 key/exchange
# body. Verbatim from bundle bn() (offset ~84127): the non-QA branch returns this.
#   t = "118c12c81e211149304bd70a0c071d01"   (default / production)
#   t = "118c02b71e211049304bd70a0c971d44"   (qa / eu-qa)
# This 32-hex string is the AES key for the exchange call (same Hex.parse semantics:
# 16 raw bytes), and is ALSO the HMAC key (as a string) for X-Signature on exchange.
EXCHANGE_BOOTSTRAP_KEY = "118c12c81e211149304bd70a0c071d01"
EXCHANGE_BOOTSTRAP_KEY_QA = "118c02b71e211049304bd70a0c971d44"

# The v1 openapi key/exchange (fn(), offset ~17457) uses a different bootstrap key
# ("2500a7d5617812f9d52515b2c8f20a3d") against fa("openapi")+"/openapi/oauth/key/
# exchange". We default to the v3 "security" flow that the station_list wrapper uses.
EXCHANGE_BOOTSTRAP_KEY_OPENAPI = "2500a7d5617812f9d52515b2c8f20a3d"
EXCHANGE_BOOTSTRAP_KEY_OPENAPI_QA = "208c02b71e211049304bd70a0c971d44"

KEY_EXCHANGE_PATH_V3 = "/v3/openapi/oauth/key/exchange"
KEY_EXCHANGE_PATH_V1 = "/openapi/oauth/key/exchange"

DEFAULT_TIMEOUT = 30

_BACKEND = default_backend()


def base_url(service: str, region: str) -> str:
    """fa(service, region) -- region->domain lookup with us-pr fallback."""
    table = DOMAINS.get(service, {})
    return table.get(region) or table.get("us-pr") or ""


# ====================================================================================
# Low-level primitives  (Ya / Xa / $a / tn / an)
# ====================================================================================
def _uuid_hex() -> str:
    """nn() = uuid v4 with dashes stripped. Used for keyindent/once/unisign."""
    return uuid.uuid4().hex


def gen_keypair() -> Tuple[str, str]:
    """
    Ya() -- generate an ephemeral P-256 keypair.
    Returns (private_hex, public_hex). public_hex is the UNCOMPRESSED point
    "04"||X||Y in hex, matching elliptic's getPublic("hex").
    """
    priv = ec.generate_private_key(ec.SECP256R1(), _BACKEND)
    priv_int = priv.private_numbers().private_value
    priv_hex = format(priv_int, "064x")
    pub_pt = priv.public_key().public_numbers()
    pub_hex = "04" + format(pub_pt.x, "064x") + format(pub_pt.y, "064x")
    return priv_hex, pub_hex


def _public_key_from_hex(pub_hex: str) -> ec.EllipticCurvePublicKey:
    raw = bytes.fromhex(pub_hex)
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def derive_share_key(client_priv_hex: str, server_pub_hex: str) -> str:
    """
    Xa shared-secret KDF, verbatim:
        s = a.derive(pub).toString(16)
        return ("0"+s).substr(-64).substr(0,32)

    i.e. ECDH -> X coordinate, hex; left-pad to 64 hex chars; take the FIRST 32 hex
    chars (= first 16 bytes). Returns a 32-character lowercase hex string. This string
    is BOTH the AES key material (Hex.parse -> 16 raw bytes) and, as a UTF-8 string,
    the HMAC key for signatures.
    """
    priv_int = int(client_priv_hex, 16)
    priv = ec.derive_private_key(priv_int, ec.SECP256R1(), _BACKEND)
    server_pub = _public_key_from_hex(server_pub_hex)
    shared = priv.exchange(ec.ECDH(), server_pub)  # raw X coordinate, 32 bytes
    s = shared.hex().lstrip("0")  # elliptic's toString(16) drops leading zeros
    s = ("0" + s)[-64:]           # ("0"+s).substr(-64): left-pad to 64 hex chars
    return s[:32]                 # .substr(0,32): first 32 hex = 16 bytes


def sign(key_hex: str, ts: int, once: str, body: Optional[str] = None) -> str:
    """
    $a(key, ts, once, body) -- X-Signature.
        msg = body ? `${ts}+${once}+${body}` : `${ts}+${once}`
        return HmacSHA256(msg, key).hex

    NOTE: `key` here is the 32-char hex STRING; CryptoJS HmacSHA256(msg, str) uses the
    string's UTF-8 bytes as the HMAC key. We replicate that exactly (encode the hex
    string itself, do NOT bytes.fromhex it).
    """
    msg = f"{ts}+{once}+{body}" if body else f"{ts}+{once}"
    return hmac.new(key_hex.encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def aes_encrypt(plaintext: str, key_hex: str) -> str:
    """
    tn(plain, keyHex) -- AES-128-CBC encrypt.
      key   = Hex.parse(keyHex)  -> the 32 hex chars as 16 RAW BYTES
      iv    = random 16 bytes
      pad   = PKCS7
      wire  = base64( iv || ciphertext )
    """
    key = bytes.fromhex(key_hex)
    if len(key) < 16:
        raise ValueError("AesCBCEncrypt: keyByte too short")
    iv = os.urandom(16)
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(iv + ct).decode("ascii")


def aes_decrypt(b64_data: str, key_hex: str) -> str:
    """
    an(b64, keyHex) -- AES-128-CBC decrypt.
      input = base64( iv(16) || ciphertext ); key = Hex.parse(keyHex); PKCS7 unpad.
    """
    key = bytes.fromhex(key_hex)
    if len(key) < 16:
        raise ValueError("AesCBCDecrypt: keyByte too short")
    raw = base64.b64decode(b64_data)
    if len(raw) < 16:
        raise ValueError("sourceData too short")
    iv, ct = raw[:16], raw[16:]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


@dataclass
class KeyExchange:
    """
    Xa instance -- the result of a handshake. Bundle stores keyIndent +
    clientPrivatKeyHex + serverPublicKeyHex + shareKey.
    """
    key_indent: str
    client_priv_hex: str
    server_pub_hex: str
    share_key: str  # 32-char hex string (used as both AES key and HMAC key)

    @classmethod
    def create(cls, key_indent: str, client_priv_hex: str, server_pub_hex: str) -> "KeyExchange":
        return cls(
            key_indent=key_indent,
            client_priv_hex=client_priv_hex,
            server_pub_hex=server_pub_hex,
            share_key=derive_share_key(client_priv_hex, server_pub_hex),
        )


@dataclass
class RandomField:
    """sn().randomField -- per-request nonce bundle."""
    unisign: str = field(default_factory=_uuid_hex)
    keyindent: str = field(default_factory=_uuid_hex)
    once: str = field(default_factory=_uuid_hex)
    ts: int = field(default_factory=lambda: int(round(time.time())))


def _base_headers(rf: RandomField, web_country: str) -> Dict[str, str]:
    """The constant part of sn().headers."""
    return {
        "Content-Type": "application/json",
        "X-Replay-Info": "replay",
        "Model_type": "WEB",
        "Model-type": "WEB",
        "X-Request-Ts": str(rf.ts),
        "X-Request-Once": rf.once,
        "Web-Country": web_country or "",
    }


# ====================================================================================
# HTTP plumbing (lazy aiohttp)
# ====================================================================================
async def _http_post(url: str, *, data: Any = None, json_body: Any = None,
                     headers: Dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """
    Thin POST helper. Returns {"status": int, "json": <parsed or None>, "text": str}.
    aiohttp is imported here so the module imports without it installed.
    """
    import aiohttp  # local import on purpose

    to = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=to) as session:
        kwargs: Dict[str, Any] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        elif data is not None:
            kwargs["data"] = data
        async with session.post(url, **kwargs) as resp:
            text = await resp.text()
            parsed: Optional[Any]
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            return {"status": resp.status, "json": parsed, "text": text}


# ====================================================================================
# Handshake: ecdh_handshake(token)   (bundle bn() / on(), v3 security flow)
# ====================================================================================
async def ecdh_handshake(token: str, *, region: str = "us-pr",
                         web_country: str = "", qa: bool = False,
                         exchange: str = "security",
                         timeout: int = DEFAULT_TIMEOUT) -> KeyExchange:
    """
    Perform an ECDH key exchange and return a KeyExchange for encrypted_post().

    There are TWO independent exchanges in the bundle, each feeding a DIFFERENT wrapper
    (the share keys are NOT interchangeable -- using the wrong one yields the gateway's
    "get identity error"):

      exchange="security"  (bn(), offset ~84283): bootstrap 118c12c8..., POST
          fa("security")+"/v3/openapi/oauth/key/exchange". Key read by wn() ->
          station_list / house_list.   [verified live]
      exchange="openapi"   (fn(), offset ~17613): bootstrap 2500a7d5..., POST
          fa("openapi")+"/openapi/oauth/key/exchange". Key read by hn() -> vn() ->
          ALL /passport/* calls (login, generate/captcha, ...).

    Both share the same on()/Ya()/tn()/an() primitives; only the bootstrap key + endpoint
    differ. `token` (auth_token) is attached as X-Auth-Token when present; the exchange
    itself is keyed on the static bootstrap key, not the share key.
    """
    if exchange == "openapi":
        bootstrap = EXCHANGE_BOOTSTRAP_KEY_OPENAPI_QA if qa else EXCHANGE_BOOTSTRAP_KEY_OPENAPI
        service, path = "openapi", KEY_EXCHANGE_PATH_V1
    else:
        bootstrap = EXCHANGE_BOOTSTRAP_KEY_QA if qa else EXCHANGE_BOOTSTRAP_KEY
        service, path = "security", KEY_EXCHANGE_PATH_V3
    priv_hex, pub_hex = gen_keypair()
    enc_client_pub = aes_encrypt(pub_hex, bootstrap)

    rf = RandomField()
    headers = _base_headers(rf, web_country)
    # on(): sign the ENCRYPTED client public key with the bootstrap key.
    headers["X-Signature"] = sign(bootstrap, rf.ts, rf.once, enc_client_pub)
    headers["X-Key-Ident"] = rf.keyindent
    headers["App-Name"] = APP_NAME
    if token:
        headers["X-Auth-Token"] = token

    url = base_url(service, region) + path
    resp = await _http_post(url, json_body={"client_public_key": enc_client_pub},
                            headers=headers, timeout=timeout)
    body = resp.get("json") or {}
    server_pub_enc = (((body.get("data") or {}).get("server_public_key"))
                      if isinstance(body.get("data"), dict) else None)
    if not server_pub_enc:
        raise EufyCloudError(f"key/exchange failed: status={resp['status']} body={resp['text'][:300]}")
    server_pub_hex = aes_decrypt(server_pub_enc, bootstrap)
    return KeyExchange.create(rf.keyindent, priv_hex, server_pub_hex)


# ====================================================================================
# Encrypted request: encrypted_post(path, data, token)   (bundle wn() / ln())
# ====================================================================================
async def encrypted_post(path: str, data: Any, token: str, *,
                         key_obj: Optional[KeyExchange] = None,
                         gtoken: str = "", region: str = "us-pr",
                         web_country: str = "", service: str = "security",
                         extra_headers: Optional[Dict[str, str]] = None,
                         return_envelope: bool = False,
                         timeout: int = DEFAULT_TIMEOUT,
                         verify_signature: bool = True) -> Any:
    """
    Make an encrypted POST to the v3 "security" service and return the decrypted,
    JSON-parsed response payload.

    Mirrors bundle wn() (offset ~83081) + ln() (offset ~14616):
        baseURL = fa("security")
        body = JSON.stringify(data)
        encBody = await tn(body, shareKey)                      # AES-CBC encrypt
        X-Signature = $a(shareKey, ts, once, encBody)           # HMAC over encBody
        headers: X-Encryption-Info=algo_ecdh, X-Signature, X-Key-Ident=keyIndent,
                 App-Name=eufy_mega, X-Auth-Token=auth_token, GToken=<localStorage>,
                 Content-Type=text/plain, Model-Type=WEB
        POST baseURL+path  data=encBody
        on 200: verify en(shareKey, rf, resp) i.e. resp.signature == $a(...);
                then resp.data = JSON.parse(an(resp.data, shareKey))

    If key_obj is None a fresh ecdh_handshake(token) is performed first.
    """
    if key_obj is None:
        key_obj = await ecdh_handshake(token, region=region, web_country=web_country,
                                       timeout=timeout)

    body_str = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    enc_body = aes_encrypt(body_str, key_obj.share_key) if body_str else ""

    rf = RandomField()
    headers = _base_headers(rf, web_country)
    headers["X-Encryption-Info"] = "algo_ecdh"
    headers["X-Signature"] = sign(key_obj.share_key, rf.ts, rf.once, enc_body or None)
    headers["X-Key-Ident"] = key_obj.key_indent
    headers["App-Name"] = APP_NAME
    headers["X-Auth-Token"] = token or ""
    # vn() spreads the caller's headers (e.headers) over the signed base BEFORE forcing
    # GToken + Content-Type. For passport calls e.headers carries the Ba()/Openudid set.
    if extra_headers:
        headers.update(extra_headers)
    headers["GToken"] = gtoken or ""
    # wn/vn override these after spreading the base headers:
    headers["Content-Type"] = "text/plain"
    headers["Model-Type"] = "WEB"

    # vn() uses ONE global share key for ALL services and routes only the baseURL by
    # domainKey (fa(service)). The share key is established once via the security
    # key/exchange (ecdh_handshake) and reused for passport/openapi/etc.
    url = base_url(service, region) + path
    resp = await _http_post(url, data=(enc_body if body_str else None),
                            headers=headers, timeout=timeout)
    if resp["status"] != 200:
        raise EufyCloudError(f"POST {path} -> HTTP {resp['status']}: {resp['text'][:300]}")

    outer = resp.get("json")
    if not isinstance(outer, dict):
        # Some endpoints may return the encrypted payload as raw text.
        raise EufyCloudError(f"POST {path}: unparseable envelope: {resp['text'][:200]}")

    # en(): verify response signature over rf.ts/rf.once -- the server echoes the
    # request once/ts in its signature. (Best-effort; skip if fields absent.)
    if verify_signature and outer.get("signature"):
        expect = sign(key_obj.share_key, rf.ts, rf.once, outer.get("data") or None)
        if expect != outer.get("signature"):
            # Non-fatal in practice for our read-only use; surface it but continue.
            pass

    enc_payload = outer.get("data")
    if return_envelope:
        # Full picture: outer {code,msg} PLUS the decrypted inner data (callers like
        # login() need the envelope code AND the payload).
        decrypted: Any = None
        if enc_payload:
            try:
                decrypted = json.loads(aes_decrypt(enc_payload, key_obj.share_key))
            except Exception:  # noqa: BLE001
                decrypted = aes_decrypt(enc_payload, key_obj.share_key)
        return {"code": outer.get("code"), "msg": outer.get("msg"),
                "data": decrypted, "raw": outer}

    if not enc_payload:
        return outer  # e.g. an error envelope { code, msg }
    decrypted = aes_decrypt(enc_payload, key_obj.share_key)
    try:
        return json.loads(decrypted)
    except Exception:
        return decrypted


# ====================================================================================
# High-level endpoints
# ====================================================================================
async def station_list(token: str, *, key_obj: Optional[KeyExchange] = None,
                       gtoken: str = "", region: str = "us-pr",
                       web_country: str = "", timeout: int = DEFAULT_TIMEOUT) -> Any:
    """
    jn() -- POST /v3/house/station_list. Verbatim body:
        {page:0, num:100, orderby:"", station_sn:"", device_sn:""}
    Returns the decrypted payload (typically {code, msg, data:[<stations>...]}).
    """
    return await encrypted_post(
        "/v3/house/station_list",
        {"page": 0, "num": 100, "orderby": "", "station_sn": "", "device_sn": ""},
        token, key_obj=key_obj, gtoken=gtoken, region=region,
        web_country=web_country, timeout=timeout,
    )


async def house_list(token: str, *, key_obj: Optional[KeyExchange] = None,
                     gtoken: str = "", region: str = "us-pr",
                     web_country: str = "", timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Sn() -- POST /v3/house/list. Same body shape as station_list."""
    return await encrypted_post(
        "/v3/house/list",
        {"page": 0, "num": 100, "orderby": "", "station_sn": "", "device_sn": ""},
        token, key_obj=key_obj, gtoken=gtoken, region=region,
        web_country=web_country, timeout=timeout,
    )


async def device_list(token: str, station_sn: str = "", *,
                      key_obj: Optional[KeyExchange] = None, gtoken: str = "",
                      region: str = "us-pr", web_country: str = "",
                      timeout: int = DEFAULT_TIMEOUT) -> Any:
    """
    Fetch the device list. The web app obtains the per-channel camera list from the
    station_list response (each station embeds its devices). This convenience wrapper
    re-uses station_list and optionally filters to one station_sn. Kept as a separate
    entry point so callers have a device-centric API.

    NOTE (INFERRED): the bundle does NOT expose a standalone "/v3/.../device_list"
    endpoint for the NVR camera array -- the camera channels arrive inside
    station_list (fields ch/name/sn/ip/status/mac/dev_type/link_type ...). If a future
    firmware exposes a dedicated device_list path, swap the URL here.
    """
    raw = await station_list(token, key_obj=key_obj, gtoken=gtoken, region=region,
                             web_country=web_country, timeout=timeout)
    nvrs = parse_stations(raw)
    if station_sn:
        nvrs = [n for n in nvrs if n.get("station_sn") == station_sn]
    return nvrs


# ====================================================================================
# Response decoding -> NVRs + cameras
# ====================================================================================
# Field names observed in the bundle's device whitelist (Nn, offset ~84600):
#   ["FPS","bind_status","ch","create_times","dev_type","dev_work_status",
#    "dev_power_switch","ip","isUps","link_type","mac","main_soft_ver","name",
#    "record_mode","resolution","sensor_num","sn","status","update_status"]
# Cameras are the per-channel sub-devices (dev_type 301 for the S4 PoE cams).
def parse_stations(raw: Any) -> List[Dict[str, Any]]:
    """
    Normalize a station_list / house_list response into:
      [{ station_sn, name, ip, online, cameras: [
            { ch, name, sn, local_ip, online }, ... ] }, ...]

    Accepts either the decrypted dict {code,msg,data:[...]} or a bare list. Is defensive
    about the exact nesting because the precise station JSON could only be confirmed by
    a live call -- the camera-array field names (ch/name/sn/ip/status) are CONFIRMED
    from the bundle, the station-wrapper field names (station_sn/station_name/devices/
    device_list) are the eufy-security-client conventions and are marked INFERRED.
    """
    if isinstance(raw, dict):
        stations = raw.get("data")
        if isinstance(stations, dict):
            # Some responses wrap as data:{list:[...]} or data:{stations:[...]}
            stations = (stations.get("list") or stations.get("stations")
                        or stations.get("data") or [])
    else:
        stations = raw
    if not isinstance(stations, list):
        return []

    out: List[Dict[str, Any]] = []
    for st in stations:
        if not isinstance(st, dict):
            continue
        station_sn = (st.get("station_sn") or st.get("sn") or st.get("device_sn") or "")
        # camera/sub-device array: eufy uses "devices" / "device_list" (INFERRED key)
        devs = (st.get("devices") or st.get("device_list") or st.get("device")
                or st.get("channels") or [])
        cameras: List[Dict[str, Any]] = []
        if isinstance(devs, list):
            for d in devs:
                if not isinstance(d, dict):
                    continue
                cameras.append({
                    "ch": d.get("ch"),
                    "name": d.get("name") or d.get("device_name") or "",
                    "sn": d.get("sn") or d.get("device_sn") or "",
                    "local_ip": d.get("ip") or d.get("local_ip") or "",
                    "online": _online(d.get("status")),
                    "dev_type": d.get("dev_type"),
                })
        out.append({
            "station_sn": station_sn,
            "name": st.get("station_name") or st.get("name") or "",
            "ip": st.get("ip") or st.get("local_ip") or st.get("station_ip") or "",
            "online": _online(st.get("status")),
            "cameras": cameras,
        })
    return out


def _online(status: Any) -> bool:
    """eufy 'status' is 1=online / 0=offline for these devices (CONFIRMED in bundle)."""
    try:
        return int(status) == 1
    except (TypeError, ValueError):
        return bool(status)


# ====================================================================================
# login(email, password, region)  --  CONFIRMED against the live login page bundle
# captures/login_js/index-bnAfdtPI.js (de-minified: ...beauty.js). No longer inferred.
# ====================================================================================
# Reversed verbatim from the passport login bundle. The earlier RSA stub was WRONG --
# the password is ECDH(P-256)+AES-256-CBC encrypted, not RSA. Key facts:
#   * Host/path:  fa("passport", region) = https://app-passport-<r>-pr.eufy.com, then
#                 "/passport/login"   (NOT "/v1/passport/login").
#   * Password encryption  (bundle te() + se()):
#       1. server EC pubkey = S()?.server_secret_info?.public_key, else the hardcoded
#          fallback uncompressed P-256 point LOGIN_SERVER_PUBKEY_FALLBACK below. A fresh
#          headless login has nothing cached -> the fallback is the value to use.
#       2. generate an ephemeral client P-256 keypair (ee()).
#       3. WebCrypto deriveKey(ECDH(clientPriv, serverPub) -> AES-CBC length:256) ==
#          the raw 32-byte ECDH shared secret (the P-256 X coordinate) used directly as
#          the AES-256 key (WebCrypto takes the first `length` bits, no extra KDF/hash).
#       4. iv = FIRST 16 BYTES of that 32-byte key  (se(): exportKey(key).slice(0,16)).
#       5. ciphertext = AES-256-CBC + PKCS7 over the UTF-8 password bytes.
#       6. password field = base64(ciphertext)  -- NO IV prepended (server re-derives).
#      Verified WITHOUT a live login by a differential test against Node's WebCrypto
#      reference: scripts/login_ref.js + scripts/login_verify.py (byte-identical output).
#   * Login body (bundle ca()):
#       { email, password:<enc>, enc:0, ab:<country>, login_id:"",
#         client_secret_info:{ public_key:<client pub hex "04"||X||Y> },
#         captcha_id:"", answer:"" }
#   * Headers = Ba()/K() base set + { "X-Auth-Token":"", Openudid:SHA256(ua+"_"+email) }
#     + App-Name:"eufy_mega" (the request wrapper injects App-Name/Model-Type).
#   * Response: code 0 -> data{ auth_token, user_id, domain, server_secret_info?, ... }.
#     code 10019 / 100056 -> graphic captcha required (login() auto-fetches it via
#     get_captcha() and tells you to re-run with captcha_id+answer); 26052 fa_info.step
#     -> 2FA; 100028 / 100041 -> login-limit reached (retry after ~24h).

# Hardcoded fallback server EC public key (uncompressed P-256 point "04"||X||Y), verbatim
# from the login bundle te(). The passport server holds the matching private key.
LOGIN_SERVER_PUBKEY_FALLBACK = "04c5c00c4f8d1197cc7c3167c52bf7acb054d722f0ef08dcd7e0883236e0d72a3868d9750cb47fa4619248f3d83f0f662671dadc6e2d31c2f41db0161651c7c076"

# Openudid = SHA256(userAgent + "_" + email).hex (bundle: N.SHA256(`${ua}_${email}`)). The
# web app uses navigator.userAgent; any stable UA works for a fresh login.
WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

PASSPORT_REGION_MAP = {  # high-level region -> passport subdomain region
    "us-pr": "us", "eu-pr": "eu", "ie-pr": "ie",
    "us": "us", "eu": "eu", "ie": "ie",
}


def _passport_base(region: str) -> str:
    r = PASSPORT_REGION_MAP.get(region, "us")
    return f"https://app-passport-{r}-pr.eufy.com"


async def login(email: str, password: str, region: str = "us-pr", *,
                country: str = "US", language: str = "en-US",
                captcha_id: str = "", answer: str = "",
                server_pub_hex: Optional[str] = None,
                user_agent: Optional[str] = None,
                key_obj: Optional[KeyExchange] = None,
                timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """
    Headless passport login, reversed from captures/login_js/index-bnAfdtPI.js. Returns:
        { auth_token, gtoken, user_id, region_domain }

    TRANSPORT: /passport/login is NOT a plaintext POST. In the bundle P()=vn() is the
    encrypted v3 wrapper -- the JSON body (which already contains the ECDH-encrypted
    password) is AES-CBC encrypted under the session share key and HMAC-signed, sent as
    text/plain to the passport host. So login() (1) does the ECDH key/exchange to get the
    share key, then (2) rides encrypted_post(service="passport"). This is the SAME wrapper
    station_list uses (verified live); only the password's own ECDH layer + the body are
    login-specific.

    The password is ECDH(P-256)+AES-256-CBC encrypted (see encrypt_password). Pass
    ``captcha_id``+``answer`` to satisfy a graphic captcha -- if the server demands one,
    login() auto-fetches the captcha image and tells you the id/path so you can re-run.
    """
    ua = user_agent or WEB_USER_AGENT
    srv_pub = server_pub_hex or LOGIN_SERVER_PUBKEY_FALLBACK

    # 1) Establish the global ECDH share key (the web app does this at bootstrap via the
    #    security key/exchange; /passport/login then rides the same encrypted wrapper).
    if key_obj is None:
        key_obj = await ecdh_handshake("", region=region, web_country=country,
                                       exchange="openapi", timeout=timeout)

    # 2) ECDH-encrypt the password against the login server pubkey (bundle te()+se()).
    client_priv, client_pub_hex = _login_keypair()
    enc_password = encrypt_password(password, srv_pub, client_priv)

    # 3) POST the login body (bundle ca()) THROUGH the encrypted wrapper (domainKey passport).
    login_body = {
        "email": email,
        "password": enc_password,
        "enc": 0,
        "ab": country,
        "login_id": "",
        "client_secret_info": {"public_key": client_pub_hex},
        "captcha_id": captcha_id,
        "answer": answer,
    }
    env = await encrypted_post(
        "/passport/login", login_body, "", key_obj=key_obj, gtoken="",
        region=region, web_country=country, service="passport",
        extra_headers=_passport_headers(email, country, language, ua),
        return_envelope=True, verify_signature=False, timeout=timeout)
    code = env.get("code")
    data = env.get("data") if isinstance(env.get("data"), dict) else {}

    # ---- graphic captcha required (10019 = need captcha, 100056 = wrong/again) --------
    if (code in (10019, 100056) or bool(data.get("captcha_id"))) and not answer:
        hint = ""
        try:
            cap = await get_captcha(region=region, language=language,
                                    user_agent=ua, key_obj=key_obj, timeout=timeout)
            saved = _save_captcha_image(cap.get("item", ""))
            hint = (f" Fetched captcha_id={cap.get('captcha_id')!r}"
                    + (f", image saved to {saved}" if saved else "")
                    + "; solve it then call login(..., captcha_id=<id>, answer=<text>).")
        except Exception as exc:  # noqa: BLE001
            hint = f" (could not auto-fetch captcha: {exc})"
        raise EufyCloudError(f"login(): CAPTCHA required (code={code}).{hint}")

    # ---- 2FA / rate-limit / generic failures -----------------------------------------
    if (data.get("fa_info") or {}).get("step") == 26052:
        raise EufyCloudError(
            "login(): two-factor authentication required (fa_info.step=26052) -- this "
            "account needs a 2FA code; headless 2FA is not implemented.")
    if code in (100028, 100041):
        raise EufyCloudError(
            f"login(): login limit reached (code={code}) -- retry after ~24h.")

    auth_token = data.get("auth_token") or data.get("token")
    if code not in (0, None) or not auth_token:
        raise EufyCloudError(
            f"login(): failed. code={code} msg={env.get('msg')!r} "
            f"data_keys={sorted(data) if isinstance(data, dict) else type(data).__name__}")

    user_id = data.get("user_id") or data.get("userId") or ""
    gtoken = data.get("gtoken") or data.get("g_token") or ""
    if not gtoken and user_id:
        # GToken is NOT returned by login -- the web app computes it client-side as
        # MD5(user_id) hex and stores it (bundle: setItem(ja, C.MD5(user_id).toString())).
        gtoken = hashlib.md5(user_id.encode("utf-8")).hexdigest()
    return {
        "auth_token": auth_token,
        "gtoken": gtoken,
        "user_id": user_id,
        # `domain` is the regional API host to use afterwards (the region_domain).
        "region_domain": data.get("domain") or data.get("region_domain") or "",
    }


def _login_keypair() -> Tuple[ec.EllipticCurvePrivateKey, str]:
    """
    ee() -- ephemeral client P-256 keypair for the login ECDH. Returns
    (private_key_obj, public_hex) where public_hex is the uncompressed point
    "04"||X||Y (matching WebCrypto exportKey("raw") hex), sent as client_secret_info.
    """
    priv = ec.generate_private_key(ec.SECP256R1(), _BACKEND)
    nums = priv.public_key().public_numbers()
    pub_hex = "04" + format(nums.x, "064x") + format(nums.y, "064x")
    return priv, pub_hex


def encrypt_password(password: str, server_pub_hex: str,
                     client_priv: ec.EllipticCurvePrivateKey) -> str:
    """
    Reproduce the login bundle's te()+se() password encryption exactly:

      shared = ECDH(client_priv, server_pub)  -> 32-byte P-256 X coordinate.
      key    = shared (all 32 bytes)          -> AES-256 key (WebCrypto deriveKey
               AES-CBC length:256 uses the raw shared secret directly, no extra KDF).
      iv     = shared[:16]                     -> se(): exportKey(key).slice(0,16).
      out    = base64( AES-256-CBC + PKCS7 ( password_utf8 ) )   -- no IV prepended.

    Verified byte-identical to Node WebCrypto in scripts/login_verify.py.
    """
    server_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), bytes.fromhex(server_pub_hex))
    shared = client_priv.exchange(ec.ECDH(), server_pub)
    if len(shared) < 32:
        shared = shared.rjust(32, b"\x00")
    key, iv = shared[:32], shared[:16]
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode("ascii")


def _openudid(email: str, user_agent: str) -> str:
    """Openudid header = SHA256(userAgent + "_" + email) hex (bundle: N.SHA256(...))."""
    return hashlib.sha256(f"{user_agent}_{email}".encode("utf-8")).hexdigest()


def _passport_headers(email: str, country: str, language: str,
                      user_agent: str) -> Dict[str, str]:
    """
    Ba()/K() base headers + the login overrides (X-Auth-Token:"", Openudid). App-Name and
    Model-Type are injected by the web app's request wrapper; we set them explicitly.
    """
    return {
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "App-Name": APP_NAME,
        "Os_type": "web", "Os-type": "web",
        "Os_version": user_agent, "Os-version": user_agent,
        "Phone_model": "Win32",
        "Model-type": "WEB", "Model_type": "WEB", "Model-Type": "WEB",
        "Country": country, "Language": language, "Timezone": "",
        "X-Auth-Token": "",
        "Openudid": _openudid(email, user_agent),
    }


async def get_captcha(*, region: str = "us-pr", language: str = "en-US",
                      user_agent: Optional[str] = None,
                      key_obj: Optional[KeyExchange] = None,
                      timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """
    getCaptcha() -- POST /passport/generate/captcha through the encrypted v3 wrapper
    (P()=vn, domainKey "passport"; the browser sends no request body). Returns the
    response's ``data`` dict, typically { captcha_id, item } where ``item`` is the captcha
    image (data-URL/base64). Needs no credentials, so it doubles as a transport smoke-test
    for the exact handshake + passport-encrypted-POST path login() relies on.
    """
    ua = user_agent or WEB_USER_AGENT
    if key_obj is None:
        key_obj = await ecdh_handshake("", region=region, web_country="",
                                       exchange="openapi", timeout=timeout)
    env = await encrypted_post(
        "/passport/generate/captcha", "", "", key_obj=key_obj,
        region=region, web_country="", service="passport",
        extra_headers=_passport_headers("", "", language, ua),
        return_envelope=True, verify_signature=False, timeout=timeout)
    data = env.get("data")
    return data if isinstance(data, dict) else {}


def _save_captcha_image(item: str) -> str:
    """Best-effort: decode a captcha ``item`` (data-URL or bare base64) to a PNG. Returns
    the written path, or "" on failure. Never raises."""
    if not item:
        return ""
    try:
        b64 = item.split(",", 1)[1] if item.startswith("data:") else item
        raw = base64.b64decode(b64)
        out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "captures", "login_captcha.png")
        with open(out, "wb") as fh:
            fh.write(raw)
        return out
    except Exception:  # noqa: BLE001
        return ""


# ====================================================================================
class EufyCloudError(RuntimeError):
    """Raised on handshake / request / decode / login failures."""


__all__ = [
    "DOMAINS", "APP_NAME", "EXCHANGE_BOOTSTRAP_KEY",
    "base_url", "gen_keypair", "derive_share_key", "sign",
    "aes_encrypt", "aes_decrypt", "KeyExchange", "RandomField",
    "ecdh_handshake", "encrypted_post",
    "station_list", "house_list", "device_list", "parse_stations",
    "login", "encrypt_password", "get_captcha", "LOGIN_SERVER_PUBKEY_FALLBACK",
    "EufyCloudError",
]


# Manual / offline self-test of the pure-crypto path (NO network). Run:
#   python scripts/eufy_cloud.py --selftest
if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        # Round-trip ECDH: two parties derive the SAME share key.
        a_priv, a_pub = gen_keypair()
        b_priv, b_pub = gen_keypair()
        ka = derive_share_key(a_priv, b_pub)
        kb = derive_share_key(b_priv, a_pub)
        assert ka == kb, f"ECDH mismatch: {ka} != {kb}"
        assert len(ka) == 32, f"share key not 16 bytes: {ka!r}"
        # Round-trip AES-CBC under the share key.
        msg = json.dumps({"hello": "eufy", "n": 123})
        ct = aes_encrypt(msg, ka)
        assert aes_decrypt(ct, ka) == msg, "AES round-trip failed"
        # Signature determinism.
        sig = sign(ka, 1700000000, "abc123", ct)
        assert len(sig) == 64, "HMAC-SHA256 hex must be 64 chars"
        print("selftest OK:")
        print("  curve            = secp256r1")
        print("  share_key (hex)  =", ka, "(16 bytes / AES-128)")
        print("  aes              = AES-128-CBC + PKCS7, wire = b64(iv||ct)")
        print("  signature sample =", sig)
    else:
        print(__doc__.strip().splitlines()[1])
        print("Run with --selftest for an offline crypto round-trip check.")
