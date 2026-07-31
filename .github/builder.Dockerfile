# Image-bake builder.
#
# Two stages so the expensive layer — a decompressed ~2 GB Raspberry Pi OS image
# — is invalidated only when the OS release changes, not when we add a tool.
# When RASPIOS_URL is unchanged the whole thing comes from the layer cache and
# the bake never touches raspberrypi.com.

# ─── Stage 1: tools ───────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS tools

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        qemu-utils \
        qemu-user-static \
        binfmt-support \
        zstd \
        xz-utils \
        parted \
        e2fsprogs \
        dosfstools \
        udev \
        zip \
        unzip \
        wget \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ─── Stage 2: pre-fetch Raspberry Pi OS ───────────────────────────────────────
FROM tools AS with-raspios

# Pinned deliberately. A floating "latest" would mean the base image changes
# under us without a commit, and the radio spike's findings are tied to a
# specific driver and firmware version (docs/radio-spike.md).
ARG RASPIOS_URL=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-12-04/2025-12-04-raspios-trixie-arm64-lite.img.xz

RUN echo "Fetching Raspberry Pi OS..." \
    && wget -q -O /raspios.img.xz "${RASPIOS_URL}" \
    && echo "Decompressing..." \
    && xz -d -T0 /raspios.img.xz \
    && echo "Base OS ready at /raspios.img"

# ─── Final ────────────────────────────────────────────────────────────────────
FROM with-raspios AS builder

# Loop devices need --privileged at runtime; the label is documentation for
# whoever finds this image and wonders why it will not work without it.
LABEL wifucked.builder="true"
LABEL wifucked.requires="privileged, /dev bind mount"
