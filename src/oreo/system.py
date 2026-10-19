"""Host resource checks: disk, memory, CPU temperature and load."""

import os
import shutil
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


def check_load(max_per_cpu: float) -> Check:
    # The 5-minute average, so a short burst (the backup, Immich's machine learning) is not
    # reported as a problem.
    per_cpu = os.getloadavg()[1] / (os.cpu_count() or 1)
    return Check("load", per_cpu <= max_per_cpu, f"{per_cpu:.2f} per CPU (5 min)")


def run() -> list[Check]:
    disk_max = _env_float("OREO_DISK_MAX_PERCENT", 90)
    paths = _env_list("OREO_DISK_PATHS", "/")
    must_mount = set(_env_list("OREO_MOUNT_PATHS", ""))
    checks = [check_disk(path, disk_max, path in must_mount) for path in paths]
    checks.append(check_memory(_env_float("OREO_MEMORY_MAX_PERCENT", 90)))
    checks.append(check_temperature(_env_float("OREO_TEMPERATURE_MAX_C", 75)))
    checks.append(check_load(_env_float("OREO_LOAD_MAX_PER_CPU", 3)))
    return checks
