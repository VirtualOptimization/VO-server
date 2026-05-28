FROM --platform=linux/amd64 ubuntu:24.04

ARG BLENDER_VERSION=4.1.1
ARG BLENDER_MAJOR_MINOR=4.1
ARG BLENDER_DOWNLOAD_URL=https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/blender-${BLENDER_VERSION}-linux-x64.tar.xz

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BLENDER_HOME=/opt/blender \
    PATH=/opt/blender:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libx11-6 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrender1 \
        libxxf86vm1 \
        python3 \
        python3-pip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt \
    && curl -fsSL "${BLENDER_DOWNLOAD_URL}" -o /tmp/blender.tar.xz \
    && tar -xJf /tmp/blender.tar.xz -C /opt \
    && mv /opt/blender-${BLENDER_VERSION}-linux-x64 "${BLENDER_HOME}" \
    && ln -s "${BLENDER_HOME}/blender" /usr/local/bin/blender \
    && rm /tmp/blender.tar.xz

WORKDIR /app

COPY workers/converter/requirements.txt /app/requirements.txt
RUN python3 -m pip install --break-system-packages -r /app/requirements.txt

COPY workers/converter /app/workers/converter

ENTRYPOINT ["python3", "/app/workers/converter/run_fbx_conversion.py"]
