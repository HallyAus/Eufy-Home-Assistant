#!/usr/bin/with-contenv bashio
set -e
cd /opt/eufy/bridge

# Build auth.json from the add-on options (v0.2 = paste a session token from get_auth.js).
# Roadmap v0.3: replace this with a headless email/password login.
cat > auth.json <<JSON
{
  "authToken": "$(bashio::config 'auth_token')",
  "gtoken": "$(bashio::config 'gtoken')",
  "userId": "$(bashio::config 'user_id')",
  "stationSn": "$(bashio::config 'station_sn')",
  "webCountry": "$(bashio::config 'region')",
  "appName": "eufy_mega"
}
JSON

if ! bashio::config.has_value 'auth_token'; then
  bashio::log.error "No auth_token set. Run bridge/get_auth.js once on a PC and paste the values into the add-on options."
  bashio::log.error "(Headless email/password login is coming in v0.3.)"
  sleep 10; exit 1
fi

bashio::log.info "Auto-discovering NVR + cameras (cmd 9100)..."
python3 eufy_stream.py --discover || bashio::log.warning "discovery failed; check auth_token/station_sn"
python3 gen_go2rtc.py "127.0.0.1" || bashio::log.warning "go2rtc config generation failed"

bashio::log.info "Starting go2rtc (RTSP :8554, WebRTC :8555, API :1984)..."
exec ./bin/go2rtc -config go2rtc.yaml
