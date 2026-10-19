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
# Immich's own nightly dumps. `oreo update immich` saves pre-update-*.sql.gz in the same
# folder; those are copied too but must not count as proof that Immich still dumps.
IMMICH_DUMP_PREFIX = "immich-db-backup-"
DUMP_KEEP_DAYS = 14
DEFAULT_TIME = "03:30"
DEFAULT_MAX_AGE_HOURS = 26
SEND_ATTEMPTS = 3
SEND_RETRY_SECONDS = 30
# A backup cut short (the agent restarted mid-run) is tried again once the same day.
MAX_ATTEMPTS_PER_DAY = 2
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
            # By host only: the old host script's snapshots (/etc, ...) and these (/host/etc,
            # ...) are then one group, so the old ones age out instead of staying forever.
            "--group-by",
            "host",
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
    own = [dump for dump in dumps if dump.name.startswith(IMMICH_DUMP_PREFIX)]
    if not own or now.timestamp() - own[0].stat().st_mtime > DUMP_MAX_AGE_HOURS * 3600:
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
    newest = own[0]
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


def _send(send: Callable[..., None], message: str, sleep=time.sleep, **kwargs) -> bool:
    """Send, retrying a brief ntfy or network failure. Returns whether it was delivered."""
    for attempt in range(SEND_ATTEMPTS):
        try:
            send(message, **kwargs)
            return True
        except notify.NotifyError as error:
            print(f"oreo backup: {error}", flush=True)
            if attempt + 1 < SEND_ATTEMPTS:
                sleep(SEND_RETRY_SECONDS)
    return False


def _alert(message: str, send: Callable[..., None], sleep=time.sleep) -> None:
    _send(send, message, sleep, title="Oreo Cloud FAILED", priority="high", tags="x")


def _success_path() -> Path:
    return Path(os.environ.get("OREO_STATE_DIR", alerts.DEFAULT_STATE_DIR)) / "backup-success.json"


def _record_success(now: datetime) -> None:
    path = _success_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"time": now.isoformat()}))


def freshness_check(now: datetime | None = None, legacy: Check | None = None) -> Check:
    """Fails when the agent's last successful backup is older than OREO_BACKUP_MAX_AGE_HOURS.
    While the host's old timer still does the backups, there is nothing to check yet."""
    now = now or datetime.now(UTC)
    legacy = legacy or legacy_timer_check()
    if not legacy.ok:
        return Check("backup", True, "the host's oreo-osbackup.timer still runs the backup")
    limit = float(os.environ.get("OREO_BACKUP_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS))
    try:
        last = datetime.fromisoformat(json.loads(_success_path().read_text())["time"])
    except (FileNotFoundError, ValueError, KeyError):
        return Check("backup", False, "no successful backup recorded by the agent yet")
    hours = (now - last).total_seconds() / 3600
    whole = round(hours)
    detail = f"last successful backup {whole} hour{'' if whole == 1 else 's'} ago"
    return Check("backup", hours <= limit, detail)


def run(
    run_cmd: Runner = _run,
    *,
    send: Callable[..., None] | None = None,
    now: datetime | None = None,
    lock_timeout: float = 3600,
    sleep: Callable[[float], None] = time.sleep,
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
        _alert(str(error), send, sleep)
        return False
    _record_success(now)
    health = [drives.line(check) for check in drives.run()]
    store = storage.summary(storage.run())
    report = "\n".join(
        [settings, database, *health, store, f"💾 Flash drive: {_flash_free()} free"]
    )
    # The backup itself succeeded either way; a report that could not be sent is only logged.
    if "❌" in report or "⚠" in report:
        _send(
            send, report, sleep, title="Oreo Cloud NEEDS ATTENTION", priority="high", tags="warning"
        )
    else:
        _send(send, report, sleep, title="Oreo Cloud OK", tags="white_check_mark")
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
    the Pi was off then, like the host timer's Persistent=true. A run that failed is not
    retried until the next day (its alert says what to fix), but one that was cut short,
    because the agent stopped mid-run, is tried once more."""
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
        state = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    today = local.date().isoformat()
    if state.get("date") == today and (
        state.get("finished") or state.get("attempts", 0) >= MAX_ATTEMPTS_PER_DAY
    ):
        return
    attempts = state.get("attempts", 0) + 1 if state.get("date") == today else 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today, "finished": False, "attempts": attempts}))
    run(send=send, now=now)
    path.write_text(json.dumps({"date": today, "finished": True, "attempts": attempts}))
