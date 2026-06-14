ARG BUILD_FROM
FROM ${BUILD_FROM}

# Engine deps: python (aiortc), node (eufy libsctp WASM oracle), ffmpeg.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-dev build-essential \
      nodejs npm ffmpeg ca-certificates git curl \
    && rm -rf /var/lib/apt/lists/*

# Vendor the bridge from this repo (build context can't reach repo root, so fetch it).
RUN git clone --depth 1 https://github.com/HallyAus/Eufy-Home-Assistant /tmp/repo \
    && mkdir -p /opt/eufy && cp -r /tmp/repo/bridge /opt/eufy/bridge && rm -rf /tmp/repo

RUN pip3 install --no-cache-dir --break-system-packages \
      -r /opt/eufy/bridge/requirements.txt paho-mqtt \
    && node /opt/eufy/bridge/fetch_deps.js

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
