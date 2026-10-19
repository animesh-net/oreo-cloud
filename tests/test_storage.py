import subprocess
from collections import namedtuple

import pytest

from oreo import storage
from oreo.checks import Check

Usage = namedtuple("Usage", "total used free")

MOUNTINFO = """\
25 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
26 25 259:1 / /boot/firmware rw,relatime shared:2 - vfat /dev/nvme0n1p1 rw
27 25 8:1 / /mnt/data-hot-ssd1 rw,relatime shared:3 - ext4 /dev/sda1 rw
28 25 8:17 / /mnt/osbackup rw,relatime shared:4 - ext4 /dev/sdb1 ro,errors=remount-ro
29 25 0:5 / /mnt/with\\040space rw - tmpfs tmpfs rw
"""


@pytest.fixture
def mounts(tmp_path):
    path = tmp_path / "mountinfo"
    path.write_text(MOUNTINFO)
    return storage.read_mounts(path)


@pytest.fixture
def usage(monkeypatch):
    used = {}
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda path: Usage(100, used.get(str(path), 40), 0)
    )
    return used


def test_reads_mount_points_and_read_only_filesystems(mounts):
    assert mounts["/"] is False
    assert mounts["/mnt/osbackup"] is True  # remounted read-only by the filesystem
    assert "/mnt/with space" in mounts


def test_mounted_writable_drive_passes(mounts, usage):
    assert storage.check_mount("/mnt/data-hot-ssd1", mounts) == Check(
        "mount /mnt/data-hot-ssd1", True, "40% used"
    )


def test_missing_mount_fails(mounts, usage):
    assert storage.check_mount("/mnt/archive-hdd1", mounts).detail == "not mounted"


def test_read_only_mount_fails(mounts, usage):
    assert storage.check_mount("/mnt/osbackup", mounts).detail == "read-only"


def test_nearly_full_drive_fails(mounts, usage):
    usage["/proc/1/root/boot/firmware"] = 92
    assert storage.check_mount("/boot/firmware", mounts).detail == "92% full"


def journal(stdout="", code=0):
    return lambda command: subprocess.CompletedProcess(command, code, stdout, "")


def test_kernel_log_without_disk_errors_passes():
    assert storage.check_kernel_log(journal("usb 1-1: device descriptor read error\n")).ok


def test_kernel_log_counts_disk_errors():
    log = "EXT4-fs error (device sda1): bad block\nBuffer I/O error on dev sdb1\n"
    check = storage.check_kernel_log(journal(log))
    assert not check.ok
    assert check.detail == "2 disk error messages in the system log"


def test_unreadable_kernel_log_fails():
    def no_host(command):
        raise FileNotFoundError("nsenter")

    assert not storage.check_kernel_log(no_host).ok


def test_summary_matches_the_nightly_report():
    good = [Check("mount /", True, "40% used"), Check("disk errors", True, "none")]
    assert storage.summary(good) == (
        "✅ Storage: all drives mounted, none nearly full, no disk errors logged"
    )
    bad = [
        Check("mount /mnt/osbackup", False, "not mounted"),
        Check("mount /", False, "93% full"),
        Check("disk errors", False, "2 disk error messages in the system log"),
    ]
    assert storage.summary(bad) == (
        "❌ Storage: /mnt/osbackup not mounted; / 93% full; 2 disk error messages in the system log"
    )


def test_unreadable_mount_fails_instead_of_crashing(mounts, monkeypatch):
    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(storage.shutil, "disk_usage", denied)
    check = storage.check_mount("/", mounts)
    assert not check.ok
    assert check.detail.startswith("cannot read it")
