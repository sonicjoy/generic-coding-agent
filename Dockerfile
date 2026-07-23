# syntax=docker/dockerfile:1
# GCA worker image — build with: docker build -t gca:latest .
# Requires a mounted Docker socket to spawn per-run isolation containers.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Prefer a lockfile when present; fall back to resolving from pyproject.
COPY uv.lock* ./
RUN if [ -f uv.lock ]; then \
      uv sync --frozen --extra service --no-dev --no-install-project && \
      uv sync --frozen --extra service --no-dev; \
    else \
      uv sync --extra service --no-dev; \
    fi

FROM python:3.12-bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        tini \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:${PATH}"
ENV GCA_DATA_DIR=/var/lib/gca
RUN mkdir -p /var/lib/gca && useradd --create-home --uid 10001 gca \
    && chown -R gca:gca /app /var/lib/gca

# The docker.sock mount is typically root-owned; run as root so the CLI can talk
# to the host daemon. Tighten with a docker group mapping in production if desired.
USER root
VOLUME ["/var/lib/gca"]
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gca-service", "worker"]
