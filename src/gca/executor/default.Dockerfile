# Default isolation image for target repos without Dockerfile.agent.
# Provides a minimal sandbox (shell + git). Language SDKs belong in the repo image.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
