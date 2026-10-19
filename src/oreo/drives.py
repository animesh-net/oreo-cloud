"""Drive health from SMART: status, life left, data written, free space. One line per drive.

Port of the host's oreo-health.sh, with the same wording, so the nightly report reads the
same. Drives are found by filesystem label through the host's /dev/disk/by-label (via
/proc/1/root), and smartctl runs inside the image against the host's devices, so it needs
the privileged agent service.
"""

import json
import math
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from oreo.checks import Check

HOST_ROOT = Path("/proc/1/root")
HOST_MOUNTINFO = Path("/proc/1/mountinfo")
DEFAULT_DRIVES = "nvme-root=System (NVMe | nvme-root);data-hot-ssd1=Data (Hot Tier | data-hot-ssd1)"
UNAVAILABLE = "health check unavailable"

SmartReader = Callable[[str], dict | None]


def configured() -> list[tuple[str, str]]:
    """(label, display name) pairs from OREO_DRIVES: `label=Name;label=Name`."""
    pairs = []
    for item in os.environ.get("OREO_DRIVES", DEFAULT_DRIVES).split(";"):
        label, _, name = item.partition("=")
        if label.strip():
            pairs.append((label.strip(), name.strip() or label.strip()))
    return pairs


def find_partition(label: str) -> str | None:
    """Kernel name of the partition with this label, e.g. "sda1"."""
    link = HOST_ROOT / "dev/disk/by-label" / label
    # Not realpath: it resolves /proc/1/root to the container's own "/", which has no by-label
    # links. The link itself is "../../<partition>".
    return Path(os.readlink(link)).name if link.exists() else None


def parent_disk(partition: str, sys_block: Path = Path("/sys/class/block")) -> str:
    """The whole disk a partition is on, e.g. "sda" for "sda1"."""
    return Path(os.path.realpath(sys_block / partition)).parent.name


def read_smart(device: str) -> dict | None:
    command = ["smartctl", "-H", "-A", "-j", device]
    if "nvme" not in device:
        command[1:1] = ["-d", "sat"]  # the SSD sits behind a USB-SATA bridge
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def human(size: float) -> str:
    """Size like `df -h`: 1024-based, rounded up, one decimal below 10."""
    for unit in ("", "K", "M", "G", "T", "P"):
        if size < 1024 or unit == "P":
            break
        size /= 1024
    value = math.ceil(size * 10) / 10 if size < 10 else math.ceil(size)
    return f"{value:g}{unit}"


def free_space(partition: str) -> str:
    try:
        for line in HOST_MOUNTINFO.read_text().splitlines():
            before, _, after = line.partition(" - ")
            source = after.split()[1] if len(after.split()) > 1 else ""
            if source == f"/dev/{partition}":
                usage = shutil.disk_usage(HOST_ROOT / before.split()[4].lstrip("/"))
                return f", {human(usage.free)} free of {human(usage.total)}"
    except OSError:
        pass
    return ""


def _attribute(smart: dict, name: str) -> dict:
    for row in smart.get("ata_smart_attributes", {}).get("table", []):
        if row.get("name") == name:
            return row
    return {}


def assess(name: str, device: str, smart: dict | None, free: str) -> Check:
    if not smart or "smart_status" not in smart:
        return Check(name, False, UNAVAILABLE)
    passed = smart["smart_status"].get("passed", False)
    status = "PASSED" if passed else "FAILED"
    if "nvme" in device:
        log = smart.get("nvme_smart_health_information_log", {})
        life = 100 - log.get("percentage_used", 0)
        written = log.get("data_units_written", 0) * 512000 / 1e12
        bad = log.get("critical_warning", 0) != 0 or log.get("media_errors", 0) != 0
        powered = ""  # this NVMe's power-on counter is unreliable, so it is left out
    else:
        life = _attribute(smart, "Wear_Leveling_Count").get("value", 0)
        written = (
            _attribute(smart, "Total_LBAs_Written").get("raw", {}).get("value", 0) * 512 / 1e12
        )
        hours = _attribute(smart, "Power_On_Hours").get("raw", {}).get("value", 0)
        bad = any(
            _attribute(smart, attr).get("raw", {}).get("value", 0)
            for attr in ("Reallocated_Sector_Ct", "Reported_Uncorrect", "UDMA_CRC_Error_Count")
        )
        powered = f", powered on {hours // 24} days"
    if passed and not bad:
        return Check(
            name, True, f"healthy, {life}% life left, {written:.1f} TB written{powered}{free}"
        )
    return Check(
        name,
        False,
        f"NEEDS ATTENTION, status {status}, {life}% life left "
        f"(run: sudo smartctl -a {device}){free}",
    )


def run(reader: SmartReader = read_smart) -> list[Check]:
    checks = []
    for label, name in configured():
        try:
            partition = find_partition(label)
        except OSError:  # no host access: not the privileged agent
            checks.append(Check(name, False, UNAVAILABLE))
            continue
        if partition is None:
            checks.append(Check(name, False, "drive not found"))
            continue
        device = f"/dev/{parent_disk(partition)}"
        checks.append(assess(name, device, reader(device), free_space(partition)))
    return checks


def line(check: Check) -> str:
    icon = "✅" if check.ok else "⚠️" if check.detail == UNAVAILABLE else "❌"
    return f"{icon} {check.name}: {check.detail}"
