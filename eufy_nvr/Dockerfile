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
        python3 \
        python3-pip \
        python3-dev \
        python3-venv \
        nodejs \
        npm \
        ffmpeg \
        build-essential \
        pkg-config \
        libavformat-dev \
        libavcodec-dev \
        libavdevice-dev \
        libavutil-dev \
        libavfilter-dev \
        libswscale-dev \
        libswresample-dev \
        libopus-dev \
        libvpx-dev \
        libsrtp2-dev \
        libffi-dev \
        libssl-dev \
        ca-certificates \
        curl \
        git \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN if [ ! -e /usr/bin/python ]; then ln -s /usr/bin/python3 /usr/bin/python; fi

# ---------------------------------------------------------------------------
# Bridge engine (portable). Home Assistant builds the add-on directory as the
# Docker context, so the bridge is fetched from this repository. `main` is the
# safe development default: it always exists, unlike a release tag that may not
# have been created yet. Release builds should override REPO_REF with the exact
# release tag/commit after that ref exists.
# ---------------------------------------------------------------------------
ARG REPO_URL="https://github.com/HallyAus/Eufy-Home-Assistant"
ARG REPO_REF="main"
RUN git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /tmp/repo \
    && mkdir -p /opt/eufy \
    && cp -r /tmp/repo/bridge /opt/eufy/bridge \
    && rm -rf /tmp/repo

WORKDIR /opt/eufy/bridge

RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt paho-mqtt

RUN node fetch_deps.js \
    && test -s worker/libsctp_0_0_4.js \
    && test -s worker/libsctp_0_0_4.wasm \
    && node sctp_oracle.js selftest

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

RUN python3 -c "import av, aiortc, Crypto, cryptography; from aiortc import RTCPeerConnection; print('av', av.__version__, 'aiortc', aiortc.__version__, 'cryptography', cryptography.__version__)"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:1985/api || exit 1

WORKDIR /opt/eufy/bridge
CMD [ "/run.sh" ]
