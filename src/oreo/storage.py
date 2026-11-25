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

from oreo import drives, net
from oreo.checks import Check

DEFAULT_MOUNTS = "/,/boot/firmware,/mnt/data-hot-ssd1,/mnt/osbackup"
HOST_ROOT = Path("/proc/1/root")
HOST_MOUNTINFO = Path("/proc/1/mountinfo")
FULL_PERCENT = 90
DISK_ERRORS = re.compile(
    r"I/O error|EXT4-fs error|Buffer I/O|critical medium|mmc[0-9].*(error|timeout)"
    r"|device offline error|Aborting journal",
    re.IGNORECASE,
)
# A USB drive that drops off comes back under a new kernel name (sda -> sdc), so names in the
# log are tied to drives through the filesystem UUID in ext4's mount lines.
MOUNTED = re.compile(r"EXT4-fs \((\w+)\): mounted filesystem ([0-9a-f-]{36})", re.IGNORECASE)
DEVICE = re.compile(r"\b(sd[a-z]{1,2}\d*|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?)\b")
FILESYSTEM_STOPPED = re.compile(r"EXT4-fs error|Aborting journal|shut down requested", re.I)
TIME = re.compile(r"\b(\d{2}:\d{2}):\d{2}\b")


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
    try:
        usage = shutil.disk_usage(host_root / mount.lstrip("/"))
    except OSError as error:  # no host access: not the privileged agent
        return Check(name, False, f"cannot read it: {error}")
    percent = round(usage.used / usage.total * 100)
    if percent >= FULL_PERCENT:
        return Check(name, False, f"{percent}% full")
    return Check(name, True, f"{percent}% used")


def disk_of(partition: str) -> str:
    """Whole disk for a kernel partition name: sda1 -> sda, nvme0n1p2 -> nvme0n1."""
    if partition.startswith("sd"):
        return partition.rstrip("0123456789")
    return re.sub(r"p\d+$", "", partition)


def _host_links(folder: str) -> dict[str, str]:
    """Partition -> link name for the host's /dev/disk/by-<folder> entries."""
    path = HOST_ROOT / "dev/disk" / folder
    try:
        return {Path(os.readlink(link)).name: link.name for link in path.iterdir()}
    except OSError:
        return {}


def named_drives() -> dict[str, tuple[str, str | None]]:
    """Partition -> (display name, filesystem UUID) for the drives configured in OREO_DRIVES."""
    labels, uuids = _host_links("by-label"), _host_links("by-uuid")
    names = dict(drives.configured())
    return {
        partition: (names[label], uuids.get(partition))
        for partition, label in labels.items()
        if label in names
    }


def describe_disk_errors(lines: list[str], known: dict[str, tuple[str, str | None]]) -> str:
    """One phrase per disk that logged errors, named by tier and label where it is known."""
    mounted = []  # (line index, partition, uuid) of every ext4 mount in the log
    for index, line in enumerate(lines):
        if match := MOUNTED.search(line):
            mounted.append((index, match.group(1), match.group(2)))
    uuid_of = {partition: uuid for _, partition, uuid in mounted}
    by_uuid = {uuid: name for name, uuid in known.values() if uuid}
    disks: dict[str, dict] = {}
    for index, line in enumerate(lines):
        if not DISK_ERRORS.search(line) or not (match := DEVICE.search(line)):
            continue
        partition = match.group(1)
        disk = disks.setdefault(
            disk_of(partition), {"count": 0, "offline": None, "fs": False, "uuid": None}
        )
        disk["count"] += 1
        disk["uuid"] = disk["uuid"] or uuid_of.get(partition)
        if "device offline" in line and disk["offline"] is None:
            disk["offline"] = index
        if FILESYSTEM_STOPPED.search(line):
            disk["fs"] = True
    phrases = []
    for kernel_name, disk in sorted(disks.items()):
        name = by_uuid.get(disk["uuid"]) or next(
            (n for p, (n, _) in known.items() if disk_of(p) == kernel_name), f"/dev/{kernel_name}"
        )
        events = []
        if disk["offline"] is not None:
            when = TIME.search(lines[disk["offline"]])
            events.append("disconnected" + (f" at {when.group(1)}" if when else ""))
            back = next(
                (
                    disk_of(p)
                    for i, p, u in mounted
                    if i > disk["offline"] and u == disk["uuid"] and disk_of(p) != kernel_name
                ),
                None,
            )
            if back:
                events.append(f"back as {back}")
        if disk["fs"]:
            events.append("filesystem needs a check (e2fsck)")
        errors = f"{disk['count']} disk error{'s' if disk['count'] != 1 else ''}"
        phrases.append(f"{name}: {', '.join(events)} ({errors})" if events else f"{name}: {errors}")
    return "; ".join(phrases)


def check_kernel_log(run: net.Runner = net.host_run, known=named_drives) -> Check:
    name = "disk errors"
    try:
        # All priorities: the mount lines that tie a kernel name to a drive are not errors.
        result = run(["journalctl", "-k", "--since", "26 hours ago", "--no-pager", "-q"])
    except (OSError, subprocess.SubprocessError) as error:
        return Check(name, False, f"cannot read the kernel log: {error}")
    if result.returncode not in (0, 1):  # 1: no entries
        return Check(name, False, f"cannot read the kernel log: {result.stderr.strip()}")
    lines = result.stdout.splitlines()
    count = sum(1 for line in lines if DISK_ERRORS.search(line))
    if not count:
        return Check(name, True, "none in the last 26 hours")
    detail = describe_disk_errors(lines, known())
    return Check(name, False, detail or f"{count} disk error messages in the system log")


def check_filesystems(
    wanted: list[str], mountinfo: Path = HOST_MOUNTINFO, host_root: Path = HOST_ROOT
) -> Check:
    """Errors ext4 has recorded on a mounted drive; they stay until e2fsck clears them."""
    name = "filesystem errors"
    found = []
    try:
        for line in mountinfo.read_text().splitlines():
            before, _, after = line.partition(" - ")
            fields = after.split()
            mount = _unescape(before.split()[4])
            if mount not in wanted or len(fields) < 2 or fields[0] != "ext4":
                continue
            partition = Path(fields[1]).name
            count_file = host_root / "sys/fs/ext4" / partition / "errors_count"
            if count_file.exists() and int(count_file.read_text().strip() or 0):
                found.append(f"{mount} ({partition}) needs a check (e2fsck)")
    except (OSError, ValueError) as error:
        return Check(name, False, f"cannot read them: {error}")
    return Check(name, not found, "; ".join(found) or "none recorded")


def run(run_host: net.Runner = net.host_run) -> list[Check]:
    wanted = [m.strip() for m in os.environ.get("OREO_STORAGE_MOUNTS", DEFAULT_MOUNTS).split(",")]
    try:
        mounts = read_mounts()
    except OSError as error:
        return [Check("mounts", False, f"cannot read the host's mounts: {error}")]
    checks = [check_mount(mount, mounts) for mount in wanted if mount]
    checks.append(check_kernel_log(run_host))
    checks.append(check_filesystems(wanted))
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
