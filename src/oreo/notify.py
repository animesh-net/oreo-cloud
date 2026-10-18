"""Push notifications through ntfy (https://ntfy.sh)."""

import os
import urllib.error
import urllib.request


class NotifyError(Exception):
    pass


def send(
    message: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    url: str | None = None,
    token: str | None = None,
    timeout: float = 10,
) -> None:
    url = url or os.environ.get("OREO_NTFY_URL")
    if not url:
        raise NotifyError("OREO_NTFY_URL is not set")
    token = token or os.environ.get("OREO_NTFY_TOKEN")

    headers = {}
    if title:
        headers["Title"] = title
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise NotifyError(f"ntfy request failed: {error}") from error
