import os
from collections import namedtuple

import pytest

from oreo import cli, drives
from oreo.checks import Check

Usage = namedtuple("Usage", "total used free")

NVME = {
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "percentage_used": 1,
        "data_units_written": 40_000_000,  # x 512,000 bytes = 20.48 TB
        "power_on_hours": 20,
        "critical_warning": 0,
        "media_errors": 0,
    },
}


def ata(wear=97, lbas=2_000_000_000, hours=4800, reallocated=0, crc=0, passed=True):
    def attr(name, value=100, raw=0):
        return {"name": name, "value": value, "raw": {"value": raw}}

    return {
        "smart_status": {"passed": passed},
        "ata_smart_attributes": {
            "table": [
                attr("Wear_Leveling_Count", value=wear),
                attr("Total_LBAs_Written", raw=lbas),
                attr("Power_On_Hours", raw=hours),
                attr("Reallocated_Sector_Ct", raw=reallocated),
                attr("Reported_Uncorrect"),
                attr("UDMA_CRC_Error_Count", raw=crc),
            ]
        },
    }


def test_healthy_nvme_leaves_out_power_on_time():
    check = drives.assess("System (NVMe | nvme-root)", "/dev/nvme0n1", NVME, ", 823G free of 916G")
    assert check == Check(
        "System (NVMe | nvme-root)",
        True,
        "healthy, 99% life left, 20.5 TB written, 823G free of 916G",
    )


def test_healthy_ssd_includes_power_on_days():
    check = drives.assess("Data", "/dev/sda", ata(), "")
    assert check.detail == "healthy, 97% life left, 1.0 TB written, powered on 200 days"


def test_reallocated_sectors_need_attention():
    check = drives.assess("Data", "/dev/sda", ata(reallocated=8), "")
    assert not check.ok
    assert (
        check.detail
        == "NEEDS ATTENTION, status PASSED, 97% life left (run: sudo smartctl -a /dev/sda)"
    )


def test_failed_smart_status_needs_attention():
    assert "status FAILED" in drives.assess("Data", "/dev/sda", ata(passed=False), "").detail


def test_nvme_media_errors_need_attention():
    smart = {**NVME, "nvme_smart_health_information_log": {"media_errors": 3}}
    assert not drives.assess("System", "/dev/nvme0n1", smart, "").ok


def test_missing_smart_data_is_unavailable():
    check = drives.assess("Data", "/dev/sda", None, "")
    assert drives.line(check) == "⚠️ Data: health check unavailable"


def test_lines_use_the_report_icons():
    assert drives.line(Check("Data", True, "healthy")) == "✅ Data: healthy"
    assert drives.line(Check("Data", False, "drive not found")) == "❌ Data: drive not found"


@pytest.mark.parametrize(
    ("size", "text"), [(916 * 1024**3, "916G"), (5.2 * 1024**3, "5.2G"), (512 * 1024**2, "512M")]
)
def test_human_sizes_match_df(size, text):
    assert drives.human(size) == text


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host: /dev/disk/by-label links, /sys/class/block and mountinfo."""
    root = tmp_path / "root"
    (root / "dev/disk/by-label").mkdir(parents=True)
    (root / "dev/sda1").touch()
    os.symlink("../../sda1", root / "dev/disk/by-label/data-hot-ssd1")
    sys_block = tmp_path / "sys/class/block"
    sys_block.mkdir(parents=True)
    (tmp_path / "sys/devices/usb/sda/sda1").mkdir(parents=True)
    os.symlink(tmp_path / "sys/devices/usb/sda/sda1", sys_block / "sda1")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("27 25 8:1 / /mnt/data-hot-ssd1 rw - ext4 /dev/sda1 rw\n")
    monkeypatch.setattr(drives, "HOST_ROOT", root)
    monkeypatch.setattr(drives, "HOST_MOUNTINFO", mountinfo)
    monkeypatch.setattr(
        drives, "parent_disk", lambda p: drives.Path(os.path.realpath(sys_block / p)).parent.name
    )
    monkeypatch.setattr(
        drives.shutil, "disk_usage", lambda path: Usage(916 * 1024**3, 0, 823 * 1024**3)
    )
    monkeypatch.setenv(
        "OREO_DRIVES", "data-hot-ssd1=Data (Hot Tier | data-hot-ssd1);archive-hdd1=Cold"
    )


def test_run_finds_drives_by_label(host):
    asked = []
    data, cold = drives.run(lambda device: asked.append(device) or ata())
    assert asked == ["/dev/sda"]
    assert data.name == "Data (Hot Tier | data-hot-ssd1)"
    assert data.detail.endswith(", 823G free of 916G")
    assert cold == Check("Cold", False, "drive not found")


def test_drives_command_prints_report_lines(host, monkeypatch, capsys):
    monkeypatch.setattr(drives, "read_smart", lambda device: ata())
    monkeypatch.setattr(drives, "run", lambda: [Check("Data", True, "healthy")])
    assert cli.main(["drives"]) == 0
    assert capsys.readouterr().out == "✅ Data: healthy\n"
