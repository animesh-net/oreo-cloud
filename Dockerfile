# Pinned by digest so every build starts from the same base; Dependabot moves the pin.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# smartctl for `oreo drives`, restic for `oreo backup`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends smartmontools restic \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && rm -rf /app

# Settings, and the files the Pi needs, so updating the image is all an update takes.
COPY config/settings.env /usr/share/oreo/settings.env
COPY compose.yaml .env.example /usr/share/oreo/
COPY pi/migrate.sh /usr/share/oreo/migrate.sh

RUN useradd --system --uid 10001 --no-create-home oreo \
    && mkdir -p /var/lib/oreo && chown oreo /var/lib/oreo
USER oreo

EXPOSE 8787

# Liveness only: /heartbeat returns 503 whenever any check fails, which must not
# mark this container itself unhealthy and cascade into its own health report.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, socket; h = os.environ.get('OREO_HEARTBEAT_HOST', '0.0.0.0'); socket.create_connection(('127.0.0.1' if h == '0.0.0.0' else h, int(os.environ.get('OREO_HEARTBEAT_PORT', 8787))), 5)"]

ENTRYPOINT ["oreo"]
CMD ["heartbeat"]
