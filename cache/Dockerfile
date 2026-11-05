# Immich's cache (Valkey), republished by the Image workflow as
# ghcr.io/animesh-net/oreo-cloud-cache, so the Pi pulls every image from GHCR. Starts from the
# digest Immich's compose file pins (Dependabot moves it) and adds Debian's security updates,
# which the pinned image falls behind on.
FROM docker.io/valkey/valkey:9@sha256:70739f85ad2ee01a726a965584a0f94895f01b0c60b3cc8b0aeef11eaa6888cf
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
