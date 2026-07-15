# Runs tests/e2e/e2e.sh in an isolated container -- same tools CI uses
# (rclone, tailscale, the built package), without touching the host.
#
# Build from repo root (needs the full source tree as context):
#   docker build -f tests/e2e/Dockerfile -t runtipi-companion-e2e .
# Run:
#   docker run --rm runtipi-companion-e2e
#
# `tailscale up` needs a TUN device + NET_ADMIN, which the e2e suite never
# invokes (it only checks `tailscale status`/`install --dry-run`), so no
# extra --cap-add/--device flags are required.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        rclone \
        curl \
        ca-certificates \
        sudo \
        tar \
    && curl -fsSL https://tailscale.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

RUN python3 -m pip install --no-cache-dir ".[dev]"

CMD ["bash", "tests/e2e/e2e.sh"]
