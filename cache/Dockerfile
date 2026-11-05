# Immich's cache (Valkey), republished by the Image workflow as
# ghcr.io/animesh-net/oreo-cloud-cache, so the Pi pulls every image from GHCR. Starts from the
# digest Immich's compose file pins (Dependabot moves it) and adds Debian's security updates,
# which the pinned image falls behind on.
FROM docker.io/valkey/valkey:9@sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
