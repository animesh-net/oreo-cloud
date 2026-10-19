"""Storage check: every drive mounted, writable and not nearly full, no disk errors logged.

Port of the host's oreo-storage-check.sh. It reads the host's view through /proc/1 (the
agent shares the host's process namespace), so it needs no extra mounts, and asks the host's
journal for kernel disk errors through nsenter.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from oreo import net
from oreo.checks import Check

DEFAULT_MOUNTS = "/,/boot/firmware,/mnt/data-hot-ssd1,/mnt/osbackup"
HOST_ROOT = Path("/proc/1/root")
HOST_MOUNTINFO = Path("/proc/1/mountinfo")
FULL_PERCENT = 90
DISK_ERRORS = re.compile(
    r"I/O error|EXT4-fs error|Buffer I/O|critical medium|mmc[0-9].*(error|timeout)", re.IGNORECASE
)


def _unescape(field: str) -> str:
    # mountinfo writes spaces and similar characters as octal escapes, e.g. \040
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def read_mounts(mountinfo: Path = HOST_MOUNTINFO) -> dict[str, bool]:
    """Map each mount point to whether it is read-only (per mount or per filesystem)."""
    mounts = {}
    for line in mountinfo.read_text().splitlines():
        before, _, after = line.partition(" - ")
        fields = before.split()
        super_options = after.split()[2].split(",") if len(after.split()) > 2 else []
        mounts[_unescape(fields[4])] = "ro" in fields[5].split(",") or "ro" in super_options
    return mounts


def check_mount(mount: str, mounts: dict[str, bool], host_root: Path = HOST_ROOT) -> Check:
    name = f"mount {mount}"
    if mount not in mounts:
        return Check(name, False, "not mounted")
    if mounts[mount]:
        return Check(name, False, "read-only")
    usage = shutil.disk_usage(host_root / mount.lstrip("/"))
    percent = round(usage.used / usage.total * 100)
    if percent >= FULL_PERCENT:
        return Check(name, False, f"{percent}% full")
    return Check(name, True, f"{percent}% used")


def check_kernel_log(run: net.Runner = net.host_run) -> Check:
    name = "disk errors"
    try:
        result = run(
            ["journalctl", "-k", "--since", "26 hours ago", "-p", "err", "--no-pager", "-q"]
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Check(name, False, f"cannot read the kernel log: {error}")
    if result.returncode not in (0, 1):  # 1: no entries
        return Check(name, False, f"cannot read the kernel log: {result.stderr.strip()}")
    count = sum(1 for line in result.stdout.splitlines() if DISK_ERRORS.search(line))
    if count:
        return Check(name, False, f"{count} disk error messages in the system log")
    return Check(name, True, "none in the last 26 hours")


def run(run_host: net.Runner = net.host_run) -> list[Check]:
    wanted = [m.strip() for m in os.environ.get("OREO_STORAGE_MOUNTS", DEFAULT_MOUNTS).split(",")]
    try:
        mounts = read_mounts()
    except OSError as error:
        return [Check("mounts", False, f"cannot read the host's mounts: {error}")]
    checks = [check_mount(mount, mounts) for mount in wanted if mount]
    checks.append(check_kernel_log(run_host))
    return checks


def summary(checks: list[Check]) -> str:
    """One line in the nightly report's wording."""
    problems = [
        f"{c.name.removeprefix('mount ')} {c.detail}" if c.name.startswith("mount ") else c.detail
        for c in checks
        if not c.ok
    ]
    if not problems:
        return "✅ Storage: all drives mounted, none nearly full, no disk errors logged"
    return "❌ Storage: " + "; ".join(problems)
