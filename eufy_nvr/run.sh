#!/usr/bin/with-contenv bashio
# =============================================================================
# Eufy NVR Local — add-on entrypoint
#
#   1. Build auth.json from the add-on options (one-time session token, v0.3).
#   2. Auto-discover the NVR + cameras (cmd 9100) -> cameras.json.
#   3. Generate go2rtc.yaml from the discovered cameras.
#   4. Run go2rtc under a supervise loop (restart-on-crash with backoff).
#
# go2rtc binds RTSP :8554 / API :1984 / WebRTC :8555 on the HOST network, so HA
# (same host) reaches the cameras at rtsp://127.0.0.1:8554/eufy_<name> and the
# Supervisor watchdog can poll tcp://[HOST]:1984.
# =============================================================================
set -o errexit
set -o nounset
set -o pipefail

BRIDGE_DIR="/opt/eufy/bridge"
cd "${BRIDGE_DIR}"

# --- map the add-on log_level option onto go2rtc's log level -----------------
LOG_LEVEL="$(bashio::config 'log_level' 'info')"
export EUFY_LOG_LEVEL="${LOG_LEVEL}"

# -----------------------------------------------------------------------------
# 1) Auth: headless email/password login -> auth.json (the engine reads it).
#    auth_login.py logs into the eufy passport, derives the NVR station_sn from the
#    station list, and writes auth.json. Credentials are passed via env (never argv),
#    scrubbed afterwards, and never logged. The file is chmod 600.
# -----------------------------------------------------------------------------
if ! bashio::config.has_value 'email' || ! bashio::config.has_value 'password'; then
    bashio::log.fatal "Set 'email' and 'password' (your eufy account) in the add-on configuration."
    # Exit non-zero but slowly, so the Supervisor doesn't crash-loop the UI.
    sleep 15
    exit 1
fi

export EUFY_EMAIL="$(bashio::config 'email')"
export EUFY_PASSWORD="$(bashio::config 'password')"
export EUFY_REGION="$(bashio::config 'region' 'US')"
export EUFY_AUTH="${BRIDGE_DIR}/auth.json"
if bashio::config.has_value 'station_sn'; then export EUFY_STATION_SN="$(bashio::config 'station_sn')"; fi
if bashio::config.has_value 'captcha_id'; then export EUFY_CAPTCHA_ID="$(bashio::config 'captcha_id')"; fi
if bashio::config.has_value 'captcha_answer'; then export EUFY_CAPTCHA_ANSWER="$(bashio::config 'captcha_answer')"; fi

umask 077
if ! python3 auth_login.py; then
    bashio::log.fatal "Headless login failed. Verify email / password / region. If the log above shows"
    bashio::log.fatal "a CAPTCHA, set captcha_id + captcha_answer in the add-on options and restart."
    rm -f "${BRIDGE_DIR}/auth.json"
    unset EUFY_PASSWORD
    sleep 15
    exit 1
fi
unset EUFY_PASSWORD
chmod 600 "${BRIDGE_DIR}/auth.json" 2>/dev/null || true
bashio::log.info "Logged in; wrote auth.json (region $(bashio::config 'region' 'US')). Credentials kept out of logs."

# Sanity-check the worker WASM the SCTP oracle needs (fetched at build time).
if [ ! -f "${BRIDGE_DIR}/worker/libsctp_0_0_1.wasm" ]; then
    bashio::log.warning "bridge/worker/libsctp_0_0_1.wasm is missing — eufy may have bumped the"
    bashio::log.warning "libsctp version. Streaming will fail until fetch_deps.js is re-run with the new version."
fi
if [ ! -x "${BRIDGE_DIR}/bin/go2rtc" ]; then
    bashio::log.fatal "go2rtc binary not found at bin/go2rtc — the image build did not complete."
    sleep 15
    exit 1
fi

# -----------------------------------------------------------------------------
# 2) Discovery + 3) go2rtc.yaml generation.
#    Discovery briefly opens the single NVR live session, so we retry a few times
#    rather than giving up on the first transient failure.
# -----------------------------------------------------------------------------
discover_and_generate() {
    local attempt
    for attempt in 1 2 3; do
        bashio::log.info "Auto-discovering NVR + cameras (cmd 9100), attempt ${attempt}/3..."
        if python3 eufy_stream.py --discover; then
            bashio::log.info "Discovery OK -> cameras.json"
            if python3 gen_go2rtc.py "127.0.0.1"; then
                bashio::log.info "Generated go2rtc.yaml from discovered cameras."
                return 0
            fi
            bashio::log.warning "gen_go2rtc.py failed; will retry."
        else
            bashio::log.warning "Discovery failed (check auth_token / station_sn / region)."
        fi
        sleep 5
    done
    return 1
}

if ! discover_and_generate; then
    if [ -f "${BRIDGE_DIR}/go2rtc.yaml" ]; then
        bashio::log.warning "Discovery failed but a previous go2rtc.yaml exists — starting with it."
    else
        bashio::log.fatal "Could not discover cameras and no cached go2rtc.yaml is present. Aborting."
        bashio::log.fatal "Most common cause: an expired session token. Re-run get_auth.js and re-paste."
        sleep 15
        exit 1
    fi
fi

# Inject the operator's chosen log level into the generated config (gen_go2rtc hardcodes 'info').
if command -v sed >/dev/null 2>&1; then
    sed -i "s/^  level: .*/  level: ${LOG_LEVEL}/" "${BRIDGE_DIR}/go2rtc.yaml" || true
fi

bashio::log.info "Discovered streams:"
# List only the stream slugs (the lines under 'streams:'), never the exec command/secrets.
grep -E '^[[:space:]]+eufy_[a-z0-9_]+:' "${BRIDGE_DIR}/go2rtc.yaml" | sed 's/:.*$//' | sed 's/^/    /' || true

# -----------------------------------------------------------------------------
# 4) Supervise loop: keep go2rtc up. The Supervisor watchdog (tcp://[HOST]:1984)
#    bounces the whole container if the API dies; this inner loop recovers faster
#    from a plain crash and applies a capped backoff to avoid hammering the NVR.
# -----------------------------------------------------------------------------
term() {
    bashio::log.info "Received stop signal; shutting down go2rtc (pid ${GO2RTC_PID:-?})."
    [ -n "${GO2RTC_PID:-}" ] && kill -TERM "${GO2RTC_PID}" 2>/dev/null || true
    exit 0
}
trap term SIGTERM SIGINT

backoff=2
while true; do
    bashio::log.info "Starting go2rtc (RTSP :8554, WebRTC :8555, API/UI :1984, log=${LOG_LEVEL})..."
    started=$(date +%s)

    # Run in the background so the trap can forward SIGTERM promptly during HA shutdown.
    ./bin/go2rtc -config go2rtc.yaml &
    GO2RTC_PID=$!
    set +o errexit
    wait "${GO2RTC_PID}"
    rc=$?
    set -o errexit

    # Clean exit (stopped by HA) -> leave the loop.
    if [ "${rc}" -eq 0 ] || [ "${rc}" -eq 143 ]; then
        bashio::log.info "go2rtc exited cleanly (rc=${rc}). Done."
        break
    fi

    # Reset backoff if it ran for a healthy while (a real crash, not a config error).
    now=$(date +%s)
    if [ "$((now - started))" -ge 60 ]; then
        backoff=2
    fi

    bashio::log.warning "go2rtc exited unexpectedly (rc=${rc}); restarting in ${backoff}s."
    sleep "${backoff}"
    # Exponential backoff capped at 60s.
    backoff=$(( backoff * 2 ))
    [ "${backoff}" -gt 60 ] && backoff=60
done
