ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG GO2RTC_VERSION="v1.9.14"
ARG GO2RTC_SHA256_AMD64="32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6"
ARG GO2RTC_SHA256_ARM64="359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AV_LOG_FORCE_NOCOLOR=1

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

# Pin the runtime bridge to the reviewed implementation commit. The add-on folder is
# Docker's build context, so the repository root cannot be copied directly.
ARG REPO_URL="https://github.com/HallyAus/Eufy-Home-Assistant"
ARG REPO_COMMIT="2be8ba256125cca4462e8c31b0539f8f7fdf045b"
RUN [[ "${REPO_COMMIT}" =~ ^[0-9a-f]{40}$ ]] \
    && git init --quiet /tmp/repo \
    && git -C /tmp/repo remote add origin "${REPO_URL}" \
    && git -C /tmp/repo fetch --quiet --depth 1 origin "${REPO_COMMIT}" \
    && git -C /tmp/repo checkout --quiet --detach FETCH_HEAD \
    && test "$(git -C /tmp/repo rev-parse HEAD)" = "${REPO_COMMIT}" \
    && mkdir -p /opt/eufy \
    && cp -r /tmp/repo/bridge /opt/eufy/bridge \
    && rm -rf /tmp/repo

WORKDIR /opt/eufy/bridge

RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

RUN node fetch_deps.js \
    && test -s worker/libsctp_0_0_4.js \
    && test -s worker/libsctp_0_0_4.wasm \
    && node sctp_oracle.js selftest

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64)  asset="go2rtc_linux_amd64"; sha256="${GO2RTC_SHA256_AMD64}" ;; \
        arm64)  asset="go2rtc_linux_arm64"; sha256="${GO2RTC_SHA256_ARM64}" ;; \
        *) echo "unsupported arch ${arch}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/eufy/bridge/bin; \
    curl -fsSL -o /opt/eufy/bridge/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/${asset}"; \
    echo "${sha256}  /opt/eufy/bridge/bin/go2rtc" | sha256sum --check --strict; \
    chmod +x /opt/eufy/bridge/bin/go2rtc; \
    /opt/eufy/bridge/bin/go2rtc -version || true

RUN python3 -c "import av, aiortc, Crypto, cryptography; from aiortc import RTCPeerConnection; print('av', av.__version__, 'aiortc', aiortc.__version__, 'cryptography', cryptography.__version__)"

COPY run.sh /run.sh
RUN chmod a+x /run.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:1985/api || exit 1

WORKDIR /opt/eufy/bridge
CMD [ "/run.sh" ]
