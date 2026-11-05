"""On-demand updates: Immich (database dump first), the proxy, unused images and the OS.

Port of the host's oreo-update. Nothing here runs on a schedule; each update is started by hand.
Commands run on the host through nsenter, in the Pi's oreo-cloud compose project, where Immich
is the photos services and Caddy the proxy service. Every option is safe to run again.
"""

import gzip
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from oreo import backup, drives, net, notify, updates

PROJECT = "/home/animesh/oreo-cloud"
# Only these pull a new Immich; db is pinned by digest, cache comes from this repo.
PHOTOS = ("photos", "photos-ml")
PHOTOS_CONTAINERS = ("oreo-cloud-photos", "oreo-cloud-photos-ml")
DB_CONTAINER = "oreo-cloud-db"
HOST = Path("/proc/1/root")
# The old script wrote to /srv/immich/library/backups, from before the library moved to the
# SSD; the dumps belong next to Immich's own, which the nightly backup copies off the SSD.
DUMPS = HOST / "mnt/data-hot-ssd1/library/backups"
MIN_DUMP_BYTES = 5_000_000
KEEP_PRE_UPDATE_DUMPS = 3
HEALTHY_WAIT_SECONDS = 180
# The OS upgrade runs as a host service, not in this container: upgrading Docker restarts it,
# which would end this container but must not end apt halfway through.
OS_UNIT = "oreo-os-update"
OS_SCRIPT = (
    "export DEBIAN_FRONTEND=noninteractive; apt-get update && "
    "apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
    "--with-new-pkgs upgrade && apt-get -y autoremove"
)

Runner = Callable[..., subprocess.CompletedProcess]  # run(command, cwd=None)
Output = Callable[[str], None]


class UpdateError(Exception):
    pass


def host(command: Sequence[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run on the host with no time limit: pulling a new Immich release can take many
    minutes. `cwd` starts it inside that host folder, where compose finds its files."""
    if cwd:
        # Change folder after entering the host: nsenter's --wd opens the folder inside this
        # container, where the host's folders do not exist.
        command = ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd, *command]
    return subprocess.run([*net.HOST_PREFIX, *command], capture_output=True, text=True, check=False)


def _error(result: subprocess.CompletedProcess) -> str:
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit code {result.returncode}"


def _compose(run: Runner, *args: str) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "--project-directory", PROJECT, *args], cwd=PROJECT)


def _inspect(run: Runner, *args: str) -> str:
    result = run(["docker", *args])
    return result.stdout.strip() if result.returncode == 0 else ""


def _immich_has_new_image(run: Runner) -> bool:
    for container in PHOTOS_CONTAINERS:
        running = _inspect(run, "inspect", "-f", "{{.Image}}", container)
        image = _inspect(run, "inspect", "-f", "{{.Config.Image}}", container)
        pulled = _inspect(run, "image", "inspect", "-f", "{{.Id}}", image) if image else ""
        if not running or running != pulled:
            return True
    return False


def _database_user() -> str:
    try:
        for line in (HOST / PROJECT.lstrip("/") / ".env").read_text().splitlines():
            if line.startswith("DB_USERNAME="):
                return line.split("=", 1)[1].strip() or "postgres"
    except OSError:
        pass
    return "postgres"


def dump_database(path: Path, user: str) -> None:
    """Stream pg_dumpall from the Postgres container into a gzip file."""
    command = [
        *net.HOST_PREFIX,
        "docker", "exec", DB_CONTAINER,
        "pg_dumpall", "--clean", "--if-exists", f"--username={user}",
    ]  # fmt: skip
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process, gzip.open(path, "wb") as out:
        shutil.copyfileobj(process.stdout, out)
    if process.returncode != 0:
        raise UpdateError(f"pg_dumpall exited with {process.returncode}")


def immich(
    run: Runner = host,
    out: Output = print,
    *,
    dump: Callable[[Path, str], None] = dump_database,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> bool:
    out("== IMMICH ==")
    pulled = _compose(run, "pull", "-q", *PHOTOS)
    if pulled.returncode != 0:
        out(f"Pull failed ({_error(pulled)}). Nothing changed.")
        return False
    if not _immich_has_new_image(run):
        out("Immich is already up to date. Nothing to do.")
        return True
    out("A newer Immich is available. Taking a database dump first.")
    stamp = (now or datetime.now()).strftime("%Y-%m-%d-%H%M")
    path = DUMPS / f"pre-update-{stamp}.sql.gz"
    try:
        dump(path, _database_user())
    except (OSError, UpdateError) as error:
        out(f"Database dump failed ({error}). Update cancelled, nothing changed.")
        path.unlink(missing_ok=True)
        return False
    if path.stat().st_size < MIN_DUMP_BYTES:
        out("Database dump is too small. Update cancelled, nothing changed.")
        path.unlink()
        return False
    old = sorted(DUMPS.glob("pre-update-*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in old[KEEP_PRE_UPDATE_DUMPS:]:
        stale.unlink()
    out(f"Dump saved: /mnt/data-hot-ssd1/library/backups/{path.name}")
    _compose(run, "up", "-d", *PHOTOS)
    status = ""
    for _ in range(HEALTHY_WAIT_SECONDS // 5):
        status = _inspect(run, "inspect", "-f", "{{.State.Health.Status}}", PHOTOS_CONTAINERS[0])
        if status == "healthy":
            break
        sleep(5)
    if status != "healthy":
        out("Immich server is NOT healthy after 3 minutes.")
        try:
            notify.send(
                "Immich update: server not healthy after 3 minutes",
                title="Oreo Cloud FAILED",
                priority="high",
                tags="x",
            )
        except notify.NotifyError as error:
            out(f"oreo: {error}")
        return False
    out("Immich is healthy.")
    out(_compose(run, "images", *PHOTOS).stdout.rstrip())
    out(f"To go back, restore the dump above and set an older IMMICH_VERSION in {PROJECT}/.env.")
    return True


# Built by this repo's workflow besides the oreo-cloud image itself.
REPO_IMAGES = ("proxy", "cache")


def caddy(run: Runner = host, out: Output = print) -> bool:
    out("== CADDY AND CACHE (pull the proxy and cache images built by GitHub) ==")
    pulled = _compose(run, "pull", "-q", *REPO_IMAGES)
    if pulled.returncode != 0:
        out(f"Pull failed ({_error(pulled)}). Nothing changed.")
        return False
    return _compose(run, "up", "-d", *REPO_IMAGES).returncode == 0


def prune(run: Runner = host, out: Output = print) -> bool:
    out("== REMOVE UNUSED DOCKER IMAGES ==")
    result = run(["docker", "image", "prune", "-f"])
    out(result.stdout.rstrip())
    return result.returncode == 0


def _reboot_line(out: Output) -> None:
    reboot = updates.reboot_needed()
    out(f"Reboot needed: {'yes (run: sudo reboot; never done automatically)' if reboot else 'no'}")


def os_update(
    run: Runner = host, out: Output = print, *, now: Callable[[], float] = time.time
) -> bool:
    out("== OS ==")
    out(f"Running apt as the host service {OS_UNIT}. This can take several minutes.")
    out("If Docker restarts during it, this command ends but the upgrade carries on.")
    since = f"@{int(now())}"
    result = run(
        ["systemd-run", f"--unit={OS_UNIT}", "--collect", "--wait", "--quiet",
         "/bin/sh", "-c", OS_SCRIPT]
    )  # fmt: skip
    log = run(["journalctl", "--unit", OS_UNIT, "--since", since, "--output", "cat", "--no-pager"])
    out("\n".join(log.stdout.splitlines()[-15:]))
    if result.returncode != 0:
        if "already" in result.stderr:
            out("An OS update is already running. Nothing more was started.")
        else:
            out(f"OS update FAILED. Full log: journalctl -u {OS_UNIT}")
        return False
    out("OS update finished.")
    _reboot_line(out)
    return True


def status(run: Runner = host, out: Output = print) -> bool:
    out("== STATUS ==")
    _reboot_line(out)
    out("Containers:")
    out(_inspect(run, "ps", "--format", "  {{.Names}}  {{.Image}}  {{.Status}}"))
    out(f"Free space on /: {drives.human(shutil.disk_usage(HOST).free)}")
    out("Available updates (nothing installs by itself):")
    for line in updates.summary():
        out(f"  {line}")
    return True


ACTIONS = {
    "status": [status],
    "immich": [immich],
    "caddy": [caddy],
    "prune": [prune],
    "os": [os_update],
}
ACTIONS["all"] = [immich, caddy, prune]


def run(action: str, out: Output = print) -> bool:
    """Run one action while holding the storage lock, so a backup never runs alongside."""
    try:
        with backup.storage_lock(600):
            return all([step(out=out) for step in ACTIONS[action]])
    except backup.BackupError:
        out("A backup or another job is running. Try again later.")
        return False
    except (OSError, subprocess.SubprocessError) as error:
        out(f"Could not run a command on the host ({error}). Nothing more was changed.")
        return False
