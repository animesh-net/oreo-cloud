"""On-demand updates of the containers: Immich (database dump first), Caddy, unused images.

Port of the container part of the host's oreo-update. OS updates stay a manual host command:
upgrading the OS can restart Docker itself, which would end this container mid-way.
Commands run on the host through nsenter, so Immich and Caddy keep their own compose files.
Every option is safe to run again.
"""

import gzip
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from oreo import backup, drives, net, notify

IMMICH = "/home/animesh/immich"
CADDY = "/home/animesh/caddy"
HOST = Path("/proc/1/root")
# The old script wrote to /srv/immich/library/backups, from before the library moved to the
# SSD; the dumps belong next to Immich's own, which the nightly backup copies off the SSD.
DUMPS = HOST / "mnt/data-hot-ssd1/library/backups"
MIN_DUMP_BYTES = 5_000_000
KEEP_PRE_UPDATE_DUMPS = 3
HEALTHY_WAIT_SECONDS = 180

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]
Output = Callable[[str], None]


class UpdateError(Exception):
    pass


def _compose(run: Runner, project: str, *args: str) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "--project-directory", project, *args])


def _inspect(run: Runner, *args: str) -> str:
    result = run(["docker", *args])
    return result.stdout.strip() if result.returncode == 0 else ""


def _immich_has_new_image(run: Runner) -> bool:
    for container in ("immich_server", "immich_machine_learning"):
        running = _inspect(run, "inspect", "-f", "{{.Image}}", container)
        image = _inspect(run, "inspect", "-f", "{{.Config.Image}}", container)
        pulled = _inspect(run, "image", "inspect", "-f", "{{.Id}}", image) if image else ""
        if not running or running != pulled:
            return True
    return False


def _database_user() -> str:
    try:
        for line in (HOST / IMMICH.lstrip("/") / ".env").read_text().splitlines():
            if line.startswith("DB_USERNAME="):
                return line.split("=", 1)[1].strip() or "postgres"
    except OSError:
        pass
    return "postgres"


def dump_database(path: Path, user: str) -> None:
    """Stream pg_dumpall from the Postgres container into a gzip file."""
    command = [
        *net.HOST_PREFIX,
        "docker", "exec", "immich_postgres",
        "pg_dumpall", "--clean", "--if-exists", f"--username={user}",
    ]  # fmt: skip
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process, gzip.open(path, "wb") as out:
        shutil.copyfileobj(process.stdout, out)
    if process.returncode != 0:
        raise UpdateError(f"pg_dumpall exited with {process.returncode}")


def immich(
    run: Runner = net.host_run,
    out: Output = print,
    *,
    dump: Callable[[Path, str], None] = dump_database,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> bool:
    out("== IMMICH ==")
    if _compose(run, IMMICH, "pull", "-q").returncode != 0:
        out("Pull failed. Nothing changed.")
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
    _compose(run, IMMICH, "up", "-d")
    status = ""
    for _ in range(HEALTHY_WAIT_SECONDS // 5):
        status = _inspect(run, "inspect", "-f", "{{.State.Health.Status}}", "immich_server")
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
    out(_compose(run, IMMICH, "images").stdout.rstrip())
    out(f"To go back, restore the dump above and set an older IMMICH_VERSION in {IMMICH}/.env.")
    return True


def caddy(run: Runner = net.host_run, out: Output = print) -> bool:
    out("== CADDY (rebuild with latest base image) ==")
    built = _compose(run, CADDY, "build", "--pull")
    out("\n".join(built.stdout.splitlines()[-3:]))
    if built.returncode != 0:
        out("Build failed. Nothing changed.")
        return False
    return _compose(run, CADDY, "up", "-d").returncode == 0


def prune(run: Runner = net.host_run, out: Output = print) -> bool:
    out("== REMOVE UNUSED DOCKER IMAGES ==")
    result = run(["docker", "image", "prune", "-f"])
    out(result.stdout.rstrip())
    return result.returncode == 0


def status(run: Runner = net.host_run, out: Output = print) -> bool:
    out("== STATUS ==")
    reboot = (HOST / "var/run/reboot-required").exists()
    out(f"Reboot needed: {'yes (run: sudo reboot; never done automatically)' if reboot else 'no'}")
    out("Containers:")
    out(_inspect(run, "ps", "--format", "  {{.Names}}  {{.Image}}  {{.Status}}"))
    out(f"Free space on /: {drives.human(shutil.disk_usage(HOST).free)}")
    out("OS updates are manual: sudo apt update && sudo apt upgrade")
    return True


ACTIONS = {"status": [status], "immich": [immich], "caddy": [caddy], "prune": [prune]}
ACTIONS["all"] = [immich, caddy, prune]


def run(action: str, out: Output = print) -> bool:
    """Run one action while holding the storage lock, so a backup never runs alongside."""
    try:
        with backup.storage_lock(600):
            return all([step(out=out) for step in ACTIONS[action]])
    except backup.BackupError:
        out("A backup or another job is running. Try again later.")
        return False
