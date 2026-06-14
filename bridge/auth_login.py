#!/usr/bin/env python3
"""Headless auth bootstrap: email/password -> auth.json (for the engine / add-on).

Credentials are read from the ENVIRONMENT (the add-on run.sh exports them); they are
never passed on argv and never printed. Logs in via the reversed eufy passport flow
(eufy_cloud.login), then fetches the station list to learn the NVR ``station_sn`` the
engine needs, and writes auth.json in the shape eufy_stream.py expects.

Env in:
  EUFY_EMAIL, EUFY_PASSWORD              required
  EUFY_REGION                            "US" | "EU" (default US)
  EUFY_STATION_SN                        optional override / fallback
  EUFY_CAPTCHA_ID, EUFY_CAPTCHA_ANSWER   optional, if a prior run reported a captcha
  EUFY_AUTH                              output path (default <bridge>/auth.json)
Exit: 0 ok | 2 missing creds | 3 login failed | 4 no station_sn.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eufy_cloud as ec  # noqa: E402

REGION_MAP = {"US": "us-pr", "EU": "eu-pr", "IE": "ie-pr"}
_SERIAL_RE = re.compile(r"[A-Z0-9]{12,20}")


def _find_station_sn(raw) -> str:
    """Pull an NVR station serial out of a station_list response. Prefer the structured
    parse; fall back to a recursive scan for a *station*sn key holding a serial."""
    try:
        for n in ec.parse_stations(raw):
            if n.get("station_sn"):
                return n["station_sn"]
    except Exception:  # noqa: BLE001
        pass
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = k.lower()
                if (isinstance(v, str) and "sn" in kl and "station" in kl
                        and _SERIAL_RE.fullmatch(v)):
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(raw)
    return found[0] if found else ""


async def main() -> int:
    email = os.environ.get("EUFY_EMAIL", "").strip()
    password = os.environ.get("EUFY_PASSWORD", "")
    if not email or not password:
        print("auth_login: EUFY_EMAIL / EUFY_PASSWORD not set", file=sys.stderr)
        return 2
    region_opt = (os.environ.get("EUFY_REGION") or "US").strip().upper()
    region = REGION_MAP.get(region_opt, "us-pr")
    country = region_opt if region_opt in ("US", "EU", "IE") else "US"

    try:
        creds = await ec.login(
            email, password, region=region, country=country,
            captcha_id=os.environ.get("EUFY_CAPTCHA_ID", ""),
            answer=os.environ.get("EUFY_CAPTCHA_ANSWER", ""))
    except ec.EufyCloudError as exc:
        print(f"auth_login: login failed: {exc}", file=sys.stderr)
        return 3

    auth_token = creds["auth_token"]
    gtoken = creds.get("gtoken", "")
    user_id = creds.get("user_id", "")
    print(f"auth_login: login OK (user_id {user_id[:6]}..., token {len(auth_token)} chars)")

    # station_sn: explicit override wins; else discover it from the station list.
    station_sn = os.environ.get("EUFY_STATION_SN", "").strip()
    if not station_sn:
        try:
            raw = await ec.station_list(auth_token, gtoken=gtoken, region=region,
                                        web_country=country)
            station_sn = _find_station_sn(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"auth_login: station_list lookup failed: {exc}", file=sys.stderr)
    if not station_sn:
        print("auth_login: could not determine station_sn "
              "(set EUFY_STATION_SN to override)", file=sys.stderr)
        return 4
    print(f"auth_login: station_sn {station_sn}")

    out_path = os.environ.get(
        "EUFY_AUTH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json"))
    auth = {
        "authToken": auth_token,
        "gtoken": gtoken,
        "userId": user_id,
        "stationSn": station_sn,
        "webCountry": country if country in ("US", "EU") else "US",
        "appName": "eufy_mega",
    }
    old = os.umask(0o077)
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(auth, fh)
    finally:
        os.umask(old)
    print(f"auth_login: wrote {out_path} (secrets not logged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
