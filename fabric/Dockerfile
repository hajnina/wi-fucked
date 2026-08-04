# The fabric server. Ships at the same version as the appliance image — they are
# two ends of one protocol and must stay compatible (ADR-005).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# nftables + procps (for sysctl) are needed by FabricWireGuard.ensure_ready()
# to enable IPv4 forwarding and NAT tunnel-peer traffic egressing the
# fabric's own WAN (ADR-019) — LAN client traffic rides the tunnel all the
# way here now, not just tunnel-management traffic.
RUN apt-get update \
    && apt-get install -y --no-install-recommends wireguard-tools iproute2 nftables procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baked in at build time so fabric.__version__ (served at /health) reflects
# the real release instead of a placeholder. Declared after the pip-install
# layer and used only in the RUN below, so a version-only change (identical
# tree, different tag) busts just this layer onward, not the dependency
# install. Defaults to the pre-existing placeholder so a bare `docker build`
# with no --build-arg still works for local dev.
ARG FABRIC_VERSION=0.0.0-dev

COPY src/ src/
RUN sed -i "s/^__version__ = .*/__version__ = \"${FABRIC_VERSION}\"/" src/fabric/__init__.py

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Runs unprivileged. Adding WireGuard peers needs NET_ADMIN, granted at run time
# to the capability rather than to the whole container.
RUN useradd --system --uid 10001 fabric
USER fabric

EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/health')"

# Run with `docker run -it` on first boot if FABRIC_ADDRESS/FABRIC_USERNAME/
# FABRIC_PASSWORD aren't set — the entrypoint walks you through a setup
# wizard before gunicorn starts. Provide them as env vars to skip it.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8081", "--workers", "2", "fabric.app:app"]
