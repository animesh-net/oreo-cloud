"""Website check: each site answers through Caddy and its certificate is not about to expire.

Container health only says the containers run. This catches a Caddy config mistake, a site
that stopped answering, or a certificate renewal that failed. The sites listen only on the
Pi's Tailscale address, which their DNS names resolve to, so this is checked from the agent.
"""

import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from oreo.checks import Check

DEFAULT_SITES = (
    "https://cloud.animesh.org.in/api/server/ping,https://pi.animesh.org.in/,"
    "https://ai.animesh.org.in/health"
)
DEFAULT_CERT_MIN_DAYS = 14
TIMEOUT_SECONDS = 15


def configured() -> list[str]:
    raw = os.environ.get("OREO_SITES", DEFAULT_SITES)
    return [url.strip() for url in raw.split(",") if url.strip()]


def check_answer(url: str) -> Check:
    """Any HTTP answer below 500 means Caddy and the site behind it are up. The Pironman
    page answers 401 because it asks for a password, which is fine."""
    name = f"site {urlsplit(url).hostname}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        return Check(name, False, f"no answer: {reason}")
    if status >= 500:
        return Check(name, False, f"answers HTTP {status}")
    return Check(name, True, f"answers HTTP {status}")


def certificate_days_left(host: str, port: int = 443) -> float:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            expires = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
    return (expires - time.time()) / 86400


def check_certificate(url: str, days_left=certificate_days_left) -> Check:
    host = urlsplit(url).hostname or ""
    name = f"certificate {host}"
    minimum = float(os.environ.get("OREO_CERT_MIN_DAYS", DEFAULT_CERT_MIN_DAYS))
    try:
        days = days_left(host)
    except (OSError, ssl.SSLError, KeyError, ValueError) as error:
        return Check(name, False, f"cannot read it: {error}")
    if days < minimum:
        return Check(name, False, f"expires in {days:.0f} days; Caddy should have renewed it")
    return Check(name, True, f"valid for {days:.0f} more days")


def run() -> list[Check]:
    checks = []
    for url in configured():
        checks.append(check_answer(url))
        checks.append(check_certificate(url))
    return checks
