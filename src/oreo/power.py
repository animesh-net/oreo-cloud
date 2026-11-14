"""Night off: power the Pi down at OREO_POWER_OFF_AT and let its clock wake it at
OREO_POWER_ON_AT.

The Pi 5's real-time clock can switch the board back on: an alarm written to
/sys/class/rtc/rtc0/wakealarm fires while it is off, as long as the bootloader is set to
POWER_OFF_ON_HALT=1 (it then draws almost nothing while off). The agent powers off only after
the alarm reads back correctly, so a failed alarm never leaves the Pi off for good.
"""

import fcntl
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from oreo import alerts, backup, net, notify, update
from oreo.checks import Check

WAKEALARM = "/sys/class/rtc/rtc0/wakealarm"
# Minutes after the off time during which a busy Pi (backup, OS update) may still power off.
WINDOW_MINUTES = 30
BOOT_TIME = Path("/proc/stat")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _zone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("OREO_TIMEZONE", alerts.DEFAULT_TIMEZONE))


def schedule() -> tuple[time, time] | None:
    """The off and wake times, or None when the night off is not set."""
    off = os.environ.get("OREO_POWER_OFF_AT", "").strip()
    on = os.environ.get("OREO_POWER_ON_AT", "").strip()
    if not off or not on:
        return None
    return time.fromisoformat(off), time.fromisoformat(on)


def next_wake(now: datetime, on: time) -> datetime:
    local = now.astimezone(_zone())
    wake = datetime.combine(local.date(), on, tzinfo=_zone())
    if wake <= local:
        wake += timedelta(days=1)
    return wake


def bootloader_check(run: Runner) -> Check:
    result = run(["rpi-eeprom-config"])
    if result.returncode != 0:
        return Check("bootloader", False, "could not read its settings with rpi-eeprom-config")
    lines = {line.strip() for line in result.stdout.splitlines()}
    if "POWER_OFF_ON_HALT=1" not in lines:
        return Check(
            "bootloader",
            False,
            "POWER_OFF_ON_HALT=1 is not set, so the wake alarm may not switch the Pi back on",
        )
    return Check("bootloader", True, "POWER_OFF_ON_HALT=1")


def set_alarm(run: Runner, wake: datetime) -> Check:
    """Write the wake alarm and read it back; only a matching read-back counts."""
    epoch = int(wake.timestamp())
    result = run(
        ["sh", "-c", f"echo 0 > {WAKEALARM} && echo {epoch} > {WAKEALARM} && cat {WAKEALARM}"]
    )
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        return Check("wake alarm", False, f"could not be set: {detail}")
    if result.stdout.strip() != str(epoch):
        return Check("wake alarm", False, f"reads back {result.stdout.strip() or 'empty'}")
    return Check("wake alarm", True, f"set for {alerts.clock(wake)}")


def busy(run: Runner) -> str | None:
    """Why the Pi should not power off yet, or None."""
    if run(["systemctl", "is-active", "--quiet", update.OS_UNIT]).returncode == 0:
        return "an OS update is running"
    try:
        backup.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with backup.LOCK_FILE.open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(file, fcntl.LOCK_UN)
    except BlockingIOError:
        return "a backup or Immich update is running"
    return None


def booted_at() -> datetime:
    for line in BOOT_TIME.read_text().splitlines():
        if line.startswith("btime "):
            return datetime.fromtimestamp(int(line.split()[1]), UTC)
    raise OSError("no btime in /proc/stat")


def _state_path() -> Path:
    return Path(os.environ.get("OREO_STATE_DIR", alerts.DEFAULT_STATE_DIR)) / "power.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def _send(send: Callable[..., None], message: str, **kwargs) -> None:
    try:
        send(message, **kwargs)
    except notify.NotifyError as error:
        print(f"oreo power: {error}", flush=True)


def _report_morning(state: dict, now: datetime, send: Callable[..., None]) -> None:
    """Once after a night off: say the Pi woke, or that it never powered off."""
    if not state.get("off_at") or state.get("reported"):
        return
    off_at = datetime.fromisoformat(state["off_at"])
    wake_at = datetime.fromisoformat(state["wake_at"])
    boot = booted_at()
    if boot > off_at:
        _send(
            send,
            f"☀️ Night off: back on at {alerts.clock(boot)} (off at {alerts.clock(off_at)}, "
            f"wake alarm {alerts.clock(wake_at)})",
            title="Oreo Cloud OK",
            tags="sunny",
        )
    elif now >= wake_at:
        _send(
            send,
            f"⚠️ Night off: the Pi did not power off at {alerts.clock(off_at)}; it stayed on",
            title="Oreo Cloud NEEDS ATTENTION",
            priority="high",
            tags="warning",
        )
    else:
        return
    _save({**state, "reported": True})


def run_once(
    run: Runner = net.host_run,
    *,
    send: Callable[..., None] | None = None,
    now: datetime | None = None,
) -> bool:
    """Power off if it is time; returns whether a power-off was started."""
    send = send or notify.send
    now = now or datetime.now(UTC)
    times = schedule()
    if times is None:
        return False
    off, on = times
    _report_morning(_load(), now, send)
    state = _load()
    local = now.astimezone(_zone())
    today = local.date().isoformat()
    start = datetime.combine(local.date(), off, tzinfo=_zone())
    end = start + timedelta(minutes=WINDOW_MINUTES)
    if state.get("date") == today or local < start:
        return False
    if local >= end:
        if state.get("waiting") == today:  # stayed busy for the whole window
            _send(
                send,
                f"🌙 Night off skipped: {state.get('reason', 'the Pi was busy')} until "
                f"{alerts.clock(end)}. It stays on tonight.",
                title="Oreo Cloud",
                tags="crescent_moon",
            )
            _save({"date": today})
        return False
    reason = busy(run)
    if reason:
        _save({**state, "waiting": today, "reason": reason})
        return False
    wake = next_wake(now, on)
    for step in (lambda: bootloader_check(run), lambda: set_alarm(run, wake)):
        check = step()
        if not check.ok:
            _send(
                send,
                f"⚠️ Night off skipped: {check.name} {check.detail}. It stays on tonight.",
                title="Oreo Cloud NEEDS ATTENTION",
                priority="high",
                tags="warning",
            )
            _save({"date": today})
            return False
    _save({"date": today, "off_at": now.isoformat(), "wake_at": wake.isoformat()})
    _send(
        send,
        f"🌙 Night off: powering off now, back on at {alerts.clock(wake)}",
        title="Oreo Cloud",
        tags="crescent_moon",
    )
    result = run(["systemctl", "poweroff", "--no-block"])
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        _send(
            send,
            f"⚠️ Night off: power-off failed ({detail}). It stays on tonight.",
            title="Oreo Cloud NEEDS ATTENTION",
            priority="high",
            tags="warning",
        )
        _save({"date": today})
        return False
    return True


def status(run: Runner = net.host_run, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    times = schedule()
    lines = [
        f"Schedule: off at {times[0]:%H:%M}, on at {times[1]:%H:%M}"
        if times
        else "Schedule: not set (OREO_POWER_OFF_AT and OREO_POWER_ON_AT)"
    ]
    check = bootloader_check(run)
    lines.append(f"Bootloader: {'OK  ' if check.ok else 'FAIL'} {check.detail}")
    alarm = run(["cat", WAKEALARM])
    value = alarm.stdout.strip()
    if alarm.returncode != 0:
        lines.append("Wake alarm: cannot be read")
    elif value:
        lines.append(f"Wake alarm: {alerts.clock(datetime.fromtimestamp(int(value), UTC))}")
    else:
        lines.append("Wake alarm: none")
    return lines


def try_wake(minutes: int, run: Runner = net.host_run, now: datetime | None = None) -> list[str]:
    """Set the wake alarm `minutes` ahead and power off, to try the wake-up once by hand."""
    now = now or datetime.now(UTC)
    wake = now + timedelta(minutes=minutes)
    for step in (lambda: bootloader_check(run), lambda: set_alarm(run, wake)):
        check = step()
        if not check.ok:
            return [f"FAIL  {check.name}: {check.detail}; not powering off"]
    result = run(["systemctl", "poweroff", "--no-block"])
    if result.returncode != 0:
        return ["FAIL  power-off failed"]
    return [f"OK    powering off; the Pi should switch itself on at {alerts.clock(wake)}"]
