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
# Optional raw H.265 passthrough (video_copy=true): lower CPU, but the browser
# live view shows only the still thumbnail. Exported here so the go2rtc-exec'd
# eufy_stream.py inherits it. Default is the H.264 transcode (browser-playable).
# -----------------------------------------------------------------------------
if bashio::config.true 'video_copy'; then
    export EUFY_VIDEO_COPY=1
    bashio::log.warning "video_copy=true -> publishing raw H.265 (live view will be thumbnail-only)."
fi

# -----------------------------------------------------------------------------
# keep-warm (opt-in, DEFAULT OFF): hold each ONLINE camera's producer warm so HA
# live-view opens instantly (no 5-13s WebRTC cold start). Each warmer is a
# consumer that pulls the stream to /dev/null, keeping go2rtc's eufy_stream.py
# producer (and its NVR WebRTC session) alive. With the H.264 transcode this is
# one continuous software encode PER online camera, so it is off by default —
# enable only on a host with CPU headroom (pair with video_copy for a cheap
# warm). A periodic re-login refreshes auth.json so a dropped warmer reconnects
# past the ~1-day eufy token; cadence via 'token_refresh_hours'.
# -----------------------------------------------------------------------------
KEEP_WARM="$(bashio::config 'keep_warm' 'false')"
WARM_PIDS=()
RELOGIN_PID=""

start_warmers() {
    local s streams i=0
    mapfile -t streams < <(grep -E '^[[:space:]]+eufy_[a-z0-9_]+:' "${BRIDGE_DIR}/go2rtc.yaml" \
        | grep -v 'offline at discovery' | sed 's/:.*$//' | tr -d '[:space:]')
    if [ "${#streams[@]}" -eq 0 ]; then
        bashio::log.warning "keep-warm: no online streams in go2rtc.yaml; nothing to warm."
        return 0
    fi
    bashio::log.info "keep-warm: warming ${#streams[@]} camera(s) (staggered)."
    for s in "${streams[@]}"; do
        [ -n "${s}" ] || continue
        # Stagger each warmer's FIRST cold-start (i*6s + 3s for go2rtc to bind) INSIDE the
        # subshell, so the main shell reaches `wait` immediately and we don't hit the NVR at once.
        ( sleep "$(( i * 6 + 3 ))"
          while true; do
            ffmpeg -hide_banner -loglevel error -rtsp_transport tcp \
                -i "rtsp://127.0.0.1:8554/${s}" -an -f null - >/dev/null 2>&1 || true
            sleep 4
          done ) &
        WARM_PIDS+=("$!")
        bashio::log.info "keep-warm: ${s} (pid $!)."
        i=$(( i + 1 ))
    done
}

start_relogin_timer() {
    local hours; hours="$(bashio::config 'token_refresh_hours' '6')"
    if ! [ "${hours}" -gt 0 ] 2>/dev/null; then
        bashio::log.warning "token_refresh_hours='${hours}' invalid/<=0; periodic re-login disabled."
        return 0
    fi
    ( while true; do
        sleep "$(( hours * 3600 ))"
        bashio::log.info "Refreshing eufy auth token (periodic, every ${hours}h)..."
        if EUFY_PASSWORD="$(bashio::config 'password')" python3 auth_login.py >/dev/null 2>&1; then
            chmod 600 "${BRIDGE_DIR}/auth.json" 2>/dev/null || true
            bashio::log.info "auth.json refreshed."
        else
            bashio::log.warning "Periodic token refresh failed; will retry next cycle."
        fi
      done ) &
    RELOGIN_PID=$!
}

term() {
    bashio::log.info "Received stop signal; shutting down go2rtc (pid ${GO2RTC_PID:-?}) + warmers."
    [ -n "${RELOGIN_PID:-}" ] && kill "${RELOGIN_PID}" 2>/dev/null || true
    for p in "${WARM_PIDS[@]:-}"; do [ -n "${p}" ] && kill "${p}" 2>/dev/null || true; done
    # best-effort: reap any warmer ffmpeg children still pulling the warm streams
    if command -v pkill >/dev/null 2>&1; then pkill -f 'rtsp://127.0.0.1:8554/eufy_' 2>/dev/null || true; fi
    [ -n "${GO2RTC_PID:-}" ] && kill -TERM "${GO2RTC_PID}" 2>/dev/null || true
    exit 0
}
trap term SIGTERM SIGINT

# -----------------------------------------------------------------------------
# 4) Supervise loop: keep go2rtc up. The Supervisor watchdog (tcp://[HOST]:1984)
#    bounces the whole container if the API dies; this inner loop recovers faster
#    from a plain crash and applies a capped backoff to avoid hammering the NVR.
# -----------------------------------------------------------------------------
WARMERS_STARTED=0
backoff=2
while true; do
    bashio::log.info "Starting go2rtc (RTSP :8554, WebRTC :8555, API/UI :1984, log=${LOG_LEVEL})..."
    started=$(date +%s)

    # Run in the background so the trap can forward SIGTERM promptly during HA shutdown.
    ./bin/go2rtc -config go2rtc.yaml &
    GO2RTC_PID=$!

    # Start warmers + token refresher once. They self-heal across go2rtc restarts (the warmer
    # ffmpeg retries until go2rtc is back); start_warmers returns at once (stagger is in-warmer).
    if [ "${WARMERS_STARTED}" -eq 0 ] && [ "${KEEP_WARM}" = 'true' ]; then
        start_warmers
        start_relogin_timer
        WARMERS_STARTED=1
    fi

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
