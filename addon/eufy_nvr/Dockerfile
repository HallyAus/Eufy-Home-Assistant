ARG BUILD_FROM
FROM ${BUILD_FROM}

# go2rtc version is injected from build.yaml (args:) so the build is reproducible (no "latest").
ARG GO2RTC_VERSION="v1.9.9"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Keep PyAV/aiortc from spawning more ffmpeg threads than the NVR needs.
    AV_LOG_FORCE_NOCOLOR=1

# ---------------------------------------------------------------------------
# System packages
#   python3 + node (engine), ffmpeg (RTSP publish), plus the C build deps that
#   aiortc / PyAV (av) / pycryptodome need IF a wheel is unavailable for the arch.
#   On amd64+aarch64/bookworm, aiortc and av ship manylinux wheels, so these mostly
#   exist as a safety net (and to satisfy PyAV's link-time FFmpeg headers).
# ---------------------------------------------------------------------------
RUN \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        # --- core runtimes ---
        python3 \
        python3-pip \
        python3-dev \
        python3-venv \
        nodejs \
        npm \
        ffmpeg \
        # --- toolchain for building aiortc/av/pycryptodome from source if needed ---
        build-essential \
        pkg-config \
        # --- FFmpeg dev libs PyAV links against ---
        libavformat-dev \
        libavcodec-dev \
        libavdevice-dev \
        libavutil-dev \
        libavfilter-dev \
        libswscale-dev \
        libswresample-dev \
        # --- aiortc media/crypto deps ---
        libopus-dev \
        libvpx-dev \
        libsrtp2-dev \
        libffi-dev \
        libssl-dev \
        # --- misc ---
        ca-certificates \
        curl \
        git \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# The generated go2rtc.yaml and the engine call bare `python` (not python3); make sure it resolves.
RUN if [ ! -e /usr/bin/python ]; then ln -s /usr/bin/python3 /usr/bin/python; fi

# ---------------------------------------------------------------------------
# Bridge engine (portable). The add-on build context is the addon/ folder only,
# so the bridge is vendored from the repo at build time. Pinned to a tag/branch
# keeps the image reproducible; bump on release.
# ---------------------------------------------------------------------------
ARG REPO_URL="https://github.com/HallyAus/Eufy-Home-Assistant"
ARG REPO_REF="main"
RUN git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /tmp/repo \
    && mkdir -p /opt/eufy \
    && cp -r /tmp/repo/bridge /opt/eufy/bridge \
    && rm -rf /tmp/repo

WORKDIR /opt/eufy/bridge

# Python deps. --break-system-packages because the HA base is an externally-managed
# (PEP 668) environment; we are the only app in this container so installing into the
# system site-packages is fine and avoids a venv indirection for the go2rtc exec calls.
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt paho-mqtt

# Fetch runtime binaries the engine needs:
#   - eufy libsctp WASM + worker shims  -> bridge/worker/   (from eufy's public CDN)
#   - ffmpeg is already installed above; fetch_deps will detect it on PATH and skip it
# We fetch go2rtc EXPLICITLY (pinned) below rather than via fetch_deps' "latest".
RUN node fetch_deps.js || echo "fetch_deps.js: non-fatal failures (go2rtc fetched separately below)"

# go2rtc: pinned download straight from the release tag, into bridge/bin/go2rtc.
# Map Docker/Debian arch -> go2rtc release asset name.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64)  asset="go2rtc_linux_amd64" ;; \
        arm64)  asset="go2rtc_linux_arm64" ;; \
        *) echo "unsupported arch ${arch}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/eufy/bridge/bin; \
    curl -fsSL -o /opt/eufy/bridge/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/${asset}"; \
    chmod +x /opt/eufy/bridge/bin/go2rtc; \
    /opt/eufy/bridge/bin/go2rtc -version || true

# Verify the heavy native imports actually load at build time (fail fast on a bad wheel/link).
# `cryptography` is what eufy_cloud.py (ECDH/AES login + station_list) needs; assert it too.
RUN python3 -c "import av, aiortc, Crypto, cryptography; from aiortc import RTCPeerConnection; print('av', av.__version__, 'aiortc', aiortc.__version__, 'cryptography', cryptography.__version__)"

# ---------------------------------------------------------------------------
# Add-on entrypoint
# ---------------------------------------------------------------------------
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Drop a build-time healthcheck against go2rtc's API (the same port the watchdog uses).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:1984/api || exit 1

WORKDIR /opt/eufy/bridge
CMD [ "/run.sh" ]
