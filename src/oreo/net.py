"""Network watchdog: when the gateway stops answering, renew the lease, then restart Wi-Fi.

Born from the 18 Oct outage: Wi-Fi stayed associated but the DHCP lease was lost, and only
`netplan apply` brought the Pi back. The commands run in the host's namespaces through
nsenter, which needs the privileged, host-PID agent service in compose.yaml.

One call to run_once() is one check. While the network is down no alert can be sent, so the
only message is "back online" once it recovers.
"""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from oreo import alerts, notify
from oreo.alerts import DEFAULT_TIMEZONE, clock
from oreo.checks import Check

DEFAULT_INTERFACE = "wlan0"
# The router restarts itself every day at 06:00. An outage inside this window is expected:
# no repair is attempted and, if it ends inside the window too, no message is sent.
DEFAULT_QUIET_WINDOW = "05:55-06:15"
# The host script this command replaces. While its timer is active the two would repair the
# network independently, so this one stands aside and says so.
LEGACY_TIMER = "oreo-netwatch.timer"
HOST_PREFIX = ("nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--")

# Failed checks in a row before each recovery step. The first is cheap, the second restarts
# Wi-Fi and is repeated every FAILS_BETWEEN_RESTARTS checks while the outage lasts.
FAILS_BEFORE_RENEW = 2
FAILS_BEFORE_RESTART = 3
FAILS_BETWEEN_RESTARTS = 5

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def host_run(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*HOST_PREFIX, *command], capture_output=True, text=True, timeout=60, check=False
    )


def _state_path() -> Path:
    return Path(os.environ.get("OREO_STATE_DIR", "/var/lib/oreo")) / "net.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh temporary name each time: a fixed one left behind by the root agent could
    # not be overwritten by the non-root heartbeat service.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as file:
        file.write(json.dumps(state))
    Path(file.name).replace(path)


def in_quiet_window(moment: datetime) -> bool:
    window = os.environ.get("OREO_NET_QUIET_WINDOW", DEFAULT_QUIET_WINDOW).strip()
    if not window:
        return False
    start, end = (time.fromisoformat(part.strip()) for part in window.split("-"))
    zone = ZoneInfo(os.environ.get("OREO_TIMEZONE", DEFAULT_TIMEZONE))
    local = moment.astimezone(zone).time()
    return start <= local < end if start <= end else local >= start or local < end


def _gateway(run: Runner) -> str | None:
    result = run(["ip", "-4", "route", "show", "default"])
    match = re.search(r"default via (\S+)", result.stdout) if result.returncode == 0 else None
    return match.group(1) if match else None


def _gateway_answers(run: Runner, gateway: str | None) -> bool:
    if gateway is None:
        return False
    return run(["ping", "-c", "2", "-W", "2", gateway]).returncode == 0


def _address(run: Runner, interface: str) -> str:
    result = run(["ip", "-4", "-br", "addr", "show", interface])
    fields = result.stdout.split()
    return fields[2] if result.returncode == 0 and len(fields) > 2 else "none"


def _report_legacy_timer(active: bool, send: Callable[..., None], now: datetime) -> None:
    # Never let a failed alert block a repair: during an outage ntfy is unreachable.
    try:
        alerts.notify_on_change(
            "net-legacy",
            "Network watchdog",
            [
                Check(
                    LEGACY_TIMER,
                    not active,
                    "still active on the host, so the agent is not repairing the network; "
                    f"run: sudo systemctl disable --now {LEGACY_TIMER}"
                    if active
                    else "disabled",
                )
            ],
            send=send,
            now=now,
        )
    except notify.NotifyError as error:
        print(f"oreo net: {error}", flush=True)


def run_once(
    run: Runner = host_run,
    *,
    send: Callable[..., None] | None = None,
    now: datetime | None = None,
) -> bool:
    """Check the gateway once, act if it is down, and return whether it answered."""
    send = send or notify.send
    now = now or datetime.now(UTC)
    interface = os.environ.get("OREO_NET_INTERFACE", DEFAULT_INTERFACE)
    legacy_active = run(["systemctl", "is-active", "--quiet", LEGACY_TIMER]).returncode == 0
    _report_legacy_timer(legacy_active, send, now)
    if legacy_active:
        return _gateway_answers(run, _gateway(run))
    state = _load()

    if _gateway_answers(run, _gateway(run)):
        if state.get("down_since"):
            since = datetime.fromisoformat(state["down_since"])
            if in_quiet_window(since) and in_quiet_window(now):
                _save({})  # the daily router restart, over before the window closed
                return True
            minutes = max(1, round((now - since).total_seconds() / 60))
            unit = "minute" if minutes == 1 else "minutes"
            recovery = state.get("action") or "none needed, it came back by itself"
            send(
                f"✅ Network: back online after about {minutes} {unit} "
                f"(down from {clock(since)}, back at {clock(now)})\n"
                f"🔧 Recovery: {recovery}\n"
                f"📶 Wi-Fi address: {_address(run, interface)}",
                title="Oreo Cloud OK",
                tags="white_check_mark",
            )
            _save({})
        return True

    if in_quiet_window(now):
        # Count from zero once the window closes, so a real outage still gets repaired.
        _save({**state, "fails": 0, "down_since": state.get("down_since") or now.isoformat()})
        return False

    fails = state.get("fails", 0) + 1
    state = {
        "fails": fails,
        "down_since": state.get("down_since") or now.isoformat(),
        "action": state.get("action"),
    }
    if fails == FAILS_BEFORE_RENEW:
        state["action"] = "automatic address renewal"
        _save(state)
        run(["networkctl", "reconfigure", interface])
    elif (
        fails >= FAILS_BEFORE_RESTART
        and (fails - FAILS_BEFORE_RESTART) % FAILS_BETWEEN_RESTARTS == 0
    ):
        state["action"] = "automatic Wi-Fi restart"
        _save(state)
        run(["netplan", "apply"])
    else:
        _save(state)
    return False
