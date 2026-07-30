# The fabric server. Ships at the same version as the appliance image — they are
# two ends of one protocol and must stay compatible (ADR-005).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends wireguard-tools iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# Runs unprivileged. Adding WireGuard peers needs NET_ADMIN, granted at run time
# to the capability rather than to the whole container.
RUN useradd --system --uid 10001 fabric
USER fabric

EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/health')"

CMD ["gunicorn", "--bind", "0.0.0.0:8081", "--workers", "2", "fabric.app:app"]
