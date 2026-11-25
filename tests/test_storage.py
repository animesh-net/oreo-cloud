import re
import subprocess
from collections import namedtuple
from datetime import datetime

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


SSD = "3c2615f3-7e32-4d1c-9a42-4d1662bea820"
HOT = "Data (Hot Tier | data-hot-ssd1)"

# The 25 Nov 2026 drop-out: the data SSD fell off USB as sda and came back as sdc.
DROPOUT = f"""\
Nov 25 06:30:25 oreo-cloud kernel: EXT4-fs (sda1): mounted filesystem {SSD} r/w with ordered data mode.
Nov 25 06:30:25 oreo-cloud kernel: EXT4-fs (sdb1): mounted filesystem a5abc5d7-79df-4bfd-9d69-aba0e5d6a3fb r/w with ordered data mode.
Nov 25 13:50:16 oreo-cloud kernel: device offline error, dev sda, sector 0 op 0x1:(WRITE) flags 0x800
Nov 25 13:50:16 oreo-cloud kernel: EXT4-fs (sda1): shut down requested (2)
Nov 25 13:50:16 oreo-cloud kernel: Aborting journal on device sda1-8.
Nov 25 13:50:16 oreo-cloud kernel: device offline error, dev sda, sector 973342720 op 0x1:(WRITE)
Nov 25 13:50:16 oreo-cloud kernel: Buffer I/O error on dev sda1, logical block 121667584, lost sync page write
Nov 25 13:50:16 oreo-cloud kernel: JBD2: I/O error when updating journal superblock for sda1-8.
Nov 25 13:50:17 oreo-cloud kernel: sd 2:0:0:0: [sdc] Attached SCSI disk
Nov 25 14:07:36 oreo-cloud kernel: EXT4-fs (sdc1): mounted filesystem {SSD} r/w with ordered data mode.
"""  # noqa: E501


def known(drives=None):
    return lambda: drives or {"sdc1": (HOT, SSD), "nvme0n1p2": ("System (NVMe | nvme-root)", None)}


def test_kernel_log_without_disk_errors_passes():
    assert storage.check_kernel_log(journal("usb 1-1: device descriptor read error\n")).ok


def test_kernel_log_names_drives_with_errors():
    log = "EXT4-fs error (device sda1): bad block\nBuffer I/O error on dev sdb1\n"
    check = storage.check_kernel_log(journal(log), known({"sda1": (HOT, SSD)}))
    assert not check.ok
    assert check.detail == (
        f"{HOT}: filesystem needs a check (e2fsck) (1 disk error); /dev/sdb: 1 disk error"
    )


def test_usb_dropout_names_the_drive_and_its_new_name():
    check = storage.check_kernel_log(journal(DROPOUT), known())
    assert check.detail == (
        f"{HOT}: disconnected at 13:50, back as sdc, filesystem needs a check (e2fsck) "
        "(5 disk errors)"
    )


DROPOUT_ISO = re.sub(r"^Nov 25 (\S+)", r"2026-11-25T\1+05:30", DROPOUT, flags=re.MULTILINE)


@pytest.mark.parametrize(
    ("fsck", "ok"),
    [
        (datetime(2026, 11, 25, 15, 40), True),  # e2fsck after the drop-out clears it
        (datetime(2026, 11, 20, 10, 0), False),  # an older check does not
        (None, False),
    ],
)
def test_filesystem_check_after_the_errors_clears_them(fsck, ok):
    asked = []

    def checked(partition):
        asked.append(partition)
        return fsck

    check = storage.check_kernel_log(journal(DROPOUT_ISO), known(), checked)
    assert asked == ["sdc1"]  # the drive's current name, not the one in the errors
    assert check.ok is ok
    if ok:
        assert check.detail == "none since the last filesystem check (e2fsck)"
    else:
        assert check.detail.startswith(f"{HOT}: disconnected at 13:50, back as sdc")


def test_last_fsck_reads_dumpe2fs():
    out = "Filesystem state:         clean\nLast checked:             Wed Nov 25 15:40:12 2026\n"
    assert storage.last_fsck("sdc1", journal(out)) == datetime(2026, 11, 25, 15, 40, 12)
    assert storage.last_fsck("sdc1", journal("")) is None


def test_disk_of_handles_each_kind_of_drive():
    assert [storage.disk_of(p) for p in ("sda1", "sdc", "nvme0n1p2", "nvme0n1", "mmcblk0p1")] == [
        "sda",
        "sdc",
        "nvme0n1",
        "nvme0n1",
        "mmcblk0",
    ]


def test_filesystem_errors_recorded_by_ext4_fail(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(MOUNTINFO)
    for partition, count in (("sda1", "3"), ("nvme0n1p2", "0")):
        folder = tmp_path / "sys/fs/ext4" / partition
        folder.mkdir(parents=True)
        (folder / "errors_count").write_text(f"{count}\n")
    check = storage.check_filesystems(["/", "/mnt/data-hot-ssd1"], mountinfo, tmp_path)
    assert check == Check(
        "filesystem errors", False, "/mnt/data-hot-ssd1 (sda1) needs a check (e2fsck)"
    )
    assert storage.check_filesystems(["/"], mountinfo, tmp_path).ok


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
