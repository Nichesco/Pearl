FROM nvidia/cuda:12.8.2-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl jq tar libgomp1 libstdc++6 ocl-icd-libopencl1 \
    && mkdir -p /etc/OpenCL/vendors \
    && echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /miner

ARG OZM_URL="https://github.com/OneZeroMiner/onezerominer/releases/download/v1.8.6/onezerominer-linux-1.8.6.tar.gz"
ARG OZM_SHA256="47ea652dcc7bacd49102203aab149dd6f678471e5fd69e68622363e0747c0f00"

RUN curl -fsSL "$OZM_URL" -o /tmp/ozm.tar.gz \
    && echo "${OZM_SHA256}  /tmp/ozm.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/ozm.tar.gz --strip-components=1 -C /miner \
    && rm -f /tmp/ozm.tar.gz \
    && chmod +x /miner/onezerominer \
    && test -x /miner/onezerominer

ARG WILDRIG_URL="https://github.com/andru-kun/wildrig-multi/releases/download/0.52.3/wildrig-multi-linux-0.52.3.tar.gz"
ARG WILDRIG_SHA256="7308c95f7c8c4c0d82aade61bb4ab3366decae2b9d04ac47b8c1dc6d1bdd2c44"

RUN curl -fsSL "$WILDRIG_URL" -o /tmp/wildrig.tar.gz \
    && echo "${WILDRIG_SHA256}  /tmp/wildrig.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/wildrig.tar.gz -C /miner wildrig-multi \
    && rm -f /tmp/wildrig.tar.gz \
    && chmod +x /miner/wildrig-multi \
    && test -x /miner/wildrig-multi

COPY entrypoint.sh /miner/entrypoint.sh
RUN sed -i 's/\r$//' /miner/entrypoint.sh && chmod +x /miner/entrypoint.sh && bash -n /miner/entrypoint.sh

ENTRYPOINT ["/miner/entrypoint.sh"]
