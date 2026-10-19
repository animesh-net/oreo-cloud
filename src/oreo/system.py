"""Host resource checks: disk, memory, CPU temperature and CPU usage."""

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from oreo.checks import Check


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def check_disk(path: str, max_percent: float, require_mount: bool = False) -> Check:
    # A drive that is not mounted leaves an empty folder on the parent filesystem, so
    # disk_usage would quietly report the parent's usage as if it were the drive's.
    if require_mount and not os.path.ismount(path):
        return Check(f"disk {path}", False, "not mounted")
    try:
        usage = shutil.disk_usage(path)
    except OSError as error:
        return Check(f"disk {path}", False, str(error))
    percent = usage.used / usage.total * 100
    return Check(f"disk {path}", percent <= max_percent, f"{percent:.0f}% used")


def check_memory(max_percent: float, meminfo: Path = Path("/proc/meminfo")) -> Check:
    try:
        fields = {}
        for line in meminfo.read_text().splitlines():
            key, value = line.split(":", 1)
            fields[key] = int(value.split()[0])
        percent = (1 - fields["MemAvailable"] / fields["MemTotal"]) * 100
    except (OSError, KeyError, ValueError) as error:
        return Check("memory", False, f"cannot read {meminfo}: {error}")
    return Check("memory", percent <= max_percent, f"{percent:.0f}% used")


def check_temperature(
    max_celsius: float, sensor: Path = Path("/sys/class/thermal/thermal_zone0/temp")
) -> Check:
    try:
        celsius = int(sensor.read_text()) / 1000
    except FileNotFoundError:
        return Check("temperature", True, "no sensor")
    except (OSError, ValueError) as error:
        return Check("temperature", False, f"cannot read {sensor}: {error}")
    return Check("temperature", celsius <= max_celsius, f"{celsius:.1f}°C")


# The previous /proc/stat reading and when CPU usage first went over the limit. They live as
# long as the process, so the agent, which runs this check every minute, sees how long a
# spike has lasted.
_cpu_last: tuple[float, int, int] | None = None
_cpu_high_since: float | None = None


def _cpu_times(stat: Path) -> tuple[int, int]:
    """(busy, total) jiffies from the first line of /proc/stat."""
    fields = [int(value) for value in stat.read_text().splitlines()[0].split()[1:9]]
    idle = fields[3] + fields[4]  # idle + iowait
    return sum(fields) - idle, sum(fields)


def check_cpu(
    max_percent: float,
    sustained_seconds: float,
    stat: Path = Path("/proc/stat"),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Check:
    """CPU usage since the previous call; fails once it stays over the limit long enough."""
    global _cpu_last, _cpu_high_since
    try:
        if _cpu_last is None:
            _cpu_last = (clock(), *_cpu_times(stat))
            sleep(1)
        start, busy_before, total_before = _cpu_last
        now = clock()
        busy, total = _cpu_times(stat)
    except (OSError, ValueError, IndexError) as error:
        return Check("cpu", False, f"cannot read {stat}: {error}")
    _cpu_last = (now, busy, total)
    elapsed = total - total_before
    percent = (busy - busy_before) / elapsed * 100 if elapsed > 0 else 0.0
    if percent <= max_percent:
        _cpu_high_since = None
        return Check("cpu", True, f"{percent:.0f}% used")
    if _cpu_high_since is None:
        _cpu_high_since = start
    lasted = now - _cpu_high_since
    detail = f"{percent:.0f}% used, over {max_percent:g}% for {lasted / 60:.0f} min"
    return Check("cpu", lasted < sustained_seconds, detail)


def run() -> list[Check]:
    disk_max = _env_float("OREO_DISK_MAX_PERCENT", 90)
    paths = _env_list("OREO_DISK_PATHS", "/")
    must_mount = set(_env_list("OREO_MOUNT_PATHS", ""))
    checks = [check_disk(path, disk_max, path in must_mount) for path in paths]
    checks.append(check_memory(_env_float("OREO_MEMORY_MAX_PERCENT", 90)))
    checks.append(check_temperature(_env_float("OREO_TEMPERATURE_MAX_C", 75)))
    checks.append(
        check_cpu(
            _env_float("OREO_CPU_MAX_PERCENT", 90),
            _env_float("OREO_CPU_SUSTAINED_SECONDS", 120),
        )
    )
    return checks
