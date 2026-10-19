"""Alert only when a check changes state, and say so when it recovers.

State lives in OREO_STATE_DIR, one JSON file per key. Running a check every few minutes
sends one alert when it starts failing, a reminder every OREO_ALERT_REMINDER_HOURS while it
keeps failing, and one message when it is back to normal.
"""

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from oreo import notify
from oreo.checks import Check

DEFAULT_STATE_DIR = "/var/lib/oreo"
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_REMINDER_HOURS = 6.0

Sender = Callable[..., None]


def _state_path(key: str) -> Path:
    return Path(os.environ.get("OREO_STATE_DIR", DEFAULT_STATE_DIR)) / f"{key}.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh temporary name each time: a fixed one left behind by the root agent could
    # not be overwritten by the non-root heartbeat service.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as file:
        file.write(json.dumps(state))
    Path(file.name).replace(path)


def clock(moment: datetime) -> str:
    zone = ZoneInfo(os.environ.get("OREO_TIMEZONE", DEFAULT_TIMEZONE))
    return moment.astimezone(zone).strftime("%H:%M")


def _problem_message(label: str, failures: list[Check]) -> str:
    lines = [f"⚠️ {label}: {len(failures)} problem{'s' if len(failures) != 1 else ''}"]
    lines += [f"{check.name}: {check.detail}" for check in failures]
    return "\n".join(lines)


def _recovery_message(label: str, since: datetime, now: datetime) -> str:
    minutes = max(1, round((now - since).total_seconds() / 60))
    return (
        f"✅ {label}: back to normal after about {minutes} minutes "
        f"(problem from {clock(since)}, resolved at {clock(now)})"
    )


def notify_on_change(
    key: str,
    label: str,
    checks: list[Check],
    *,
    send: Sender | None = None,
    now: datetime | None = None,
) -> None:
    """Send an alert, reminder or recovery message when `checks` warrants one.

    Raises notify.NotifyError when sending fails; the stored state is then left as it was,
    so the next run tries again.
    """
    send = send or notify.send
    now = now or datetime.now(UTC)
    path = _state_path(key)
    state = _load(path)
    failures = [check for check in checks if not check.ok]
    names = sorted(check.name for check in failures)

    if not failures:
        if state.get("failing"):
            since = datetime.fromisoformat(state["since"])
            send(
                _recovery_message(label, since, now),
                title="Oreo Cloud OK",
                tags="white_check_mark",
            )
        _save(path, {})
        return

    reminder_hours = float(os.environ.get("OREO_ALERT_REMINDER_HOURS", DEFAULT_REMINDER_HOURS))
    since = datetime.fromisoformat(state["since"]) if state.get("failing") else now
    last_alert = datetime.fromisoformat(state["alerted"]) if state.get("alerted") else None
    changed = state.get("failing") != names
    reminder_due = (
        last_alert is not None and (now - last_alert).total_seconds() >= reminder_hours * 3600
    )
    if changed or reminder_due:
        send(
            _problem_message(label, failures),
            title="Oreo Cloud NEEDS ATTENTION",
            priority="high",
            tags="warning",
        )
        last_alert = now
    _save(
        path,
        {
            "failing": names,
            "since": since.isoformat(),
            "alerted": last_alert.isoformat() if last_alert else None,
        },
    )
