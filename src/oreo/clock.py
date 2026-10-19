"""Whether the host's clock is synchronised, asked through the host's own timedatectl."""

import subprocess

from oreo import net
from oreo.checks import Check


def check(run: net.Runner = net.host_run) -> Check:
    try:
        result = run(["timedatectl", "show", "--property=NTPSynchronized", "--value"])
    except (OSError, subprocess.SubprocessError) as error:
        return Check("clock", False, f"cannot ask the host: {error}")
    if result.returncode != 0:
        return Check("clock", False, f"cannot ask the host: {result.stderr.strip()}")
    synced = result.stdout.strip() == "yes"
    return Check("clock", synced, "synchronised" if synced else "not synchronised with NTP")
