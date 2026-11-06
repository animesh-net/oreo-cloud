"""Files built into the image: the settings, and the compose.yaml, .env.example and migrate.sh
for the Pi.

The settings in config/settings.env are defaults: a variable that is already set, from the
Pi's .env, wins. Set OREO_SETTINGS_FILE to an empty value to skip them, as the integration
tests do on a machine that is not the Pi.
"""

import os
from pathlib import Path

from oreo.checks import Check

SHARE_DIR = Path("/usr/share/oreo")
FILES = {"compose": "compose.yaml", "env-example": ".env.example", "migrate": "migrate.sh"}
HOST_COMPOSE_FILE = "/host/home/animesh/oreo-cloud/compose.yaml"
REFRESH = (
    "image-update, or: cd ~/oreo-cloud && docker run --rm ghcr.io/animesh-net/oreo-cloud "
    "show compose > compose.yaml && docker compose up -d"
)


def shared(name: str) -> Path:
    return Path(os.environ.get("OREO_SHARE_DIR", SHARE_DIR)) / name


def load() -> None:
    path = os.environ.get("OREO_SETTINGS_FILE", str(shared("settings.env")))
    if not path or not Path(path).is_file():
        return
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def compose_check() -> Check:
    """Whether the Pi's compose.yaml matches the one this image was built with."""
    host = Path(os.environ.get("OREO_HOST_COMPOSE_FILE", HOST_COMPOSE_FILE))
    try:
        current = host.read_text()
        shipped = shared("compose.yaml").read_text()
    except OSError as error:
        return Check("compose.yaml", True, f"not compared: {error.strerror}")
    if current == shipped:
        return Check("compose.yaml", True, "matches the image")
    return Check("compose.yaml", False, f"differs from the image's; refresh it: {REFRESH}")
