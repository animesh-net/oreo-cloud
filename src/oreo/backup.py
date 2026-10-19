"""Nightly backup: system settings to restic on the flash drive, Immich database dump copied
and verified, then one report with drive health and storage.

Port of the host's oreo-osbackup.sh with the same steps, checks and wording. Sources are the
host folders mounted read-only under /host in compose.yaml, so snapshots record /host/etc and
so on. The flash drive and the SSD are reached through /proc/1/root, the host's live view, so
a drive that is not mounted is caught instead of written to the empty folder beneath it.
"""

import fcntl
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from oreo import alerts, clock, drives, net, notify, storage
from oreo.checks import Check

HOST = Path("/proc/1/root")
SOURCES = (
    "/host/etc",
    "/host/home/animesh",
    "/host/usr/local/bin",
    "/host/var/lib/tailscale",
    "/host/boot/firmware",
    "/host/var/spool/cron",
)
EXCLUDES = ("/host/home/animesh/.cache",)
FLASH_MOUNT = "/mnt/osbackup"
REPOSITORY = HOST / "mnt/osbackup/restic"
PASSWORD_FILE = HOST / "root/.restic-pass"
PACKAGE_LIST = HOST / "home/animesh/packages.txt"
DUMPS = HOST / "mnt/data-hot-ssd1/library/backups"
DUMP_COPIES = HOST / "mnt/osbackup/immich-db"
LOCK_FILE = Path("/run/lock/oreo-storage.lock")  # shared with the host's oreo-update
DUMP_MAX_AGE_HOURS = 36
DUMP_KEEP_DAYS = 14
DEFAULT_TIME = "03:30"
LEGACY_TIMER = "oreo-osbackup.timer"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class BackupError(Exception):
    """A step failed; the message is the alert text."""


def _run(command: Sequence[str]) -> subprocess.CompletedProcess:
    # Lowest CPU and disk priority, as the host service had (Nice=19, IOSchedulingClass=idle).
    return subprocess.run(
        ["ionice", "-c3", "nice", "-n19", *command], capture_output=True, text=True, check=False
    )


def _restic(run: Runner, *args: str) -> subprocess.CompletedProcess:
    # The container's root filesystem is read-only, so restic's cache lives in the state volume.
    cache = Path(os.environ.get("OREO_STATE_DIR", alerts.DEFAULT_STATE_DIR)) / "restic-cache"
    command = [
        "restic", "-r", str(REPOSITORY), "--password-file", str(PASSWORD_FILE),
        "--cache-dir", str(cache), *args,
    ]  # fmt: skip
    result = run(command)
    if result.returncode not in (0, 3):  # 3: some source files could not be read
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise BackupError(f"osbackup: restic {args[0]} failed: {detail}")
    return result


def _local_now(now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(os.environ.get("OREO_TIMEZONE", alerts.DEFAULT_TIMEZONE)))


def back_up_settings(run: Runner, now: datetime) -> str:
    for source in SOURCES:
        if not Path(source).exists():
            raise BackupError(f"osbackup: source {source} is missing, job skipped")
    packages = net.host_run(["apt-mark", "showmanual"])
    if packages.returncode == 0:
        PACKAGE_LIST.write_text(packages.stdout)
    # A backup cut short (the agent restarted mid-run) leaves restic's lock behind, and every
    # later run would fail on it. Plain `unlock` removes only locks whose process is gone.
    _restic(run, "unlock")
    args = ["backup", "--json", "--host", "oreo-cloud", "--exclude-caches", *SOURCES]
    for exclude in EXCLUDES:
        args += ["--exclude", exclude]
    output = _restic(run, *args).stdout
    summary = next(
        (m for m in map(_json_line, output.splitlines()) if m.get("message_type") == "summary"),
        None,
    )
    if summary is None:
        raise BackupError("osbackup: restic finished without a summary")
    local = _local_now(now)
    if local.isoweekday() == 7:
        _restic(
            run,
            "forget",
            "--keep-daily",
            "7",
            "--keep-weekly",
            "4",
            "--keep-monthly",
            "6",
            "--prune",
        )
    if local.day == 1:
        _restic(run, "check", "--read-data-subset=5%")
    versions = len(json.loads(_restic(run, "snapshots", "--json").stdout or "[]"))
    return (
        f"✅ System settings: {len(SOURCES)} of {len(SOURCES)} folders saved "
        f"({summary.get('total_files_processed', 0)} files, {summary.get('files_new', 0)} new, "
        f"{summary.get('files_changed', 0)} changed), {versions} versions kept"
    )


def _json_line(line: str) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def copy_database_dump(now: datetime) -> str:
    dumps = sorted(DUMPS.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not dumps or now.timestamp() - dumps[0].stat().st_mtime > DUMP_MAX_AGE_HOURS * 3600:
        raise BackupError(
            "osbackup: settings backup done, but DB dump problem: "
            f"no Immich database dump newer than {DUMP_MAX_AGE_HOURS} hours in "
            "/mnt/data-hot-ssd1/library/backups"
        )
    DUMP_COPIES.mkdir(parents=True, exist_ok=True)
    try:
        for dump in dumps:
            copy = DUMP_COPIES / dump.name
            if not copy.exists() or copy.stat().st_size != dump.stat().st_size:
                shutil.copy2(dump, copy)
    except OSError as error:
        raise BackupError(
            "osbackup: settings backup done, but DB dump problem: "
            f"copying the database dump to the flash drive failed: {error}"
        ) from error
    newest = dumps[0]
    if (DUMP_COPIES / newest.name).stat().st_size != newest.stat().st_size:
        raise BackupError(
            "osbackup: settings backup done, but DB dump problem: "
            "copied dump size does not match the original"
        )
    for copy in DUMP_COPIES.glob("*.sql.gz"):
        if now.timestamp() - copy.stat().st_mtime > DUMP_KEEP_DAYS * 86400:
            copy.unlink()
    kept = len(list(DUMP_COPIES.glob("*.sql.gz")))
    size = drives.human(newest.stat().st_size)
    return (
        f"✅ Immich database: {size} dump, copied to flash drive and verified, {kept} copies kept"
    )


def _flash_free() -> str:
    return drives.human(shutil.disk_usage(HOST / FLASH_MOUNT.lstrip("/")).free)


def _alert(message: str, send: Callable[..., None]) -> None:
    send(message, title="Oreo Cloud FAILED", priority="high", tags="x")


def run(
    run_cmd: Runner = _run,
    *,
    send: Callable[..., None] | None = None,
    now: datetime | None = None,
    lock_timeout: float = 3600,
) -> bool:
    """One backup. Sends the report or the failure alert; returns whether it succeeded."""
    send = send or notify.send
    now = now or datetime.now(UTC)
    try:
        with storage_lock(lock_timeout):
            if not clock.check().ok:
                raise BackupError("oreo backup: clock not synchronised, job skipped")
            mounts = storage.read_mounts()
            if FLASH_MOUNT not in mounts:
                raise BackupError(f"oreo backup: {FLASH_MOUNT} is not mounted, job skipped")
            if not REPOSITORY.is_dir():
                raise BackupError("osbackup: restic repository missing, job skipped")
            settings = back_up_settings(run_cmd, now)
            database = copy_database_dump(now)
    except BackupError as error:
        _alert(str(error), send)
        return False
    health = [drives.line(check) for check in drives.run()]
    store = storage.summary(storage.run())
    report = "\n".join(
        [settings, database, *health, store, f"💾 Flash drive: {_flash_free()} free"]
    )
    if "❌" in report or "⚠" in report:
        send(report, title="Oreo Cloud NEEDS ATTENTION", priority="high", tags="warning")
    else:
        send(report, title="Oreo Cloud OK", tags="white_check_mark")
    return True


class storage_lock:
    """Hold the storage lock the host's oreo-update also takes, so the two never overlap."""

    def __init__(self, timeout: float):
        self.timeout = timeout

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.file = LOCK_FILE.open("a")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise BackupError(
                        "oreo backup: another storage job held the lock for an hour, job skipped"
                    ) from None
                time.sleep(5)

    def __exit__(self, *exc):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()
        return False


def legacy_timer_check(run: net.Runner | None = None) -> Check:
    active = (run or net.host_run)(
        ["systemctl", "is-active", "--quiet", LEGACY_TIMER]
    ).returncode == 0
    return Check(
        LEGACY_TIMER,
        not active,
        "still active on the host, so the agent is not running the backup; "
        f"run: sudo systemctl disable --now {LEGACY_TIMER}"
        if active
        else "disabled",
    )


def _state_path() -> Path:
    return Path(os.environ.get("OREO_STATE_DIR", alerts.DEFAULT_STATE_DIR)) / "backup.json"


def nightly(*, send: Callable[..., None] | None = None, now: datetime | None = None) -> None:
    """Run the backup once a day at OREO_BACKUP_TIME, or as soon as possible after it if
    the Pi was off then, like the host timer's Persistent=true. A failed run is not retried
    until the next day; its alert says what to fix."""
    now = now or datetime.now(UTC)
    legacy = legacy_timer_check()
    try:
        alerts.notify_on_change("backup-legacy", "Nightly backup", [legacy], send=send, now=now)
    except notify.NotifyError as error:
        print(f"oreo backup: {error}", flush=True)
    if not legacy.ok:
        return
    local = _local_now(now)
    hour, minute = (
        int(part) for part in os.environ.get("OREO_BACKUP_TIME", DEFAULT_TIME).split(":")
    )
    if (local.hour, local.minute) < (hour, minute):
        return
    path = _state_path()
    try:
        last = json.loads(path.read_text()).get("last_run")
    except (FileNotFoundError, ValueError):
        last = None
    today = local.date().isoformat()
    if last == today:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_run": today}))
    run(send=send, now=now)
