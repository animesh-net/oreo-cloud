"""Host resource checks: disk, memory, CPU temperature and load."""

import os
import shutil
from pathlib import Path

from oreo.checks import Check


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def check_disk(path: str, max_percent: float) -> Check:
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
    per_cpu = os.getloadavg()[0] / (os.cpu_count() or 1)
    return Check("load", per_cpu <= max_per_cpu, f"{per_cpu:.2f} per CPU (1 min)")


def run() -> list[Check]:
    disk_max = _env_float("OREO_DISK_MAX_PERCENT", 90)
    paths = os.environ.get("OREO_DISK_PATHS", "/").split(",")
    checks = [check_disk(path.strip(), disk_max) for path in paths if path.strip()]
    checks.append(check_memory(_env_float("OREO_MEMORY_MAX_PERCENT", 90)))
    checks.append(check_temperature(_env_float("OREO_TEMPERATURE_MAX_C", 75)))
    checks.append(check_load(_env_float("OREO_LOAD_MAX_PER_CPU", 2)))
    return checks
