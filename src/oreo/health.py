"""Container health checks."""

from oreo import docker_api
from oreo.checks import Check

IGNORE_LABEL = "oreo.ignore"
ONE_OFF_LABEL = "com.docker.compose.oneoff"
# Compose services that run once and exit: oreo-ai's migrations, which stay as a stopped
# container after its `up -d`. Only a clean exit is skipped; a failed one still alerts.
ONE_SHOT_CONTAINERS = ("oreo-ai-db-migrate",)


def check_containers(containers: list[dict]) -> list[Check]:
    checks = []
    for container in containers:
        labels = container.get("Labels") or {}
        if labels.get(IGNORE_LABEL) == "true":
            continue
        # A finished `docker compose run` (without --rm) is not a service that went down.
        if labels.get(ONE_OFF_LABEL) == "True" and container.get("State") != "running":
            continue
        name = container["Names"][0].lstrip("/")
        state = container.get("State", "unknown")
        status = container.get("Status", "")
        if name in ONE_SHOT_CONTAINERS and state == "exited" and status.startswith("Exited (0)"):
            continue
        if state != "running":
            checks.append(Check(f"container {name}", False, f"{state}: {status}"))
        elif "(unhealthy)" in status:
            checks.append(Check(f"container {name}", False, f"unhealthy: {status}"))
        else:
            checks.append(Check(f"container {name}", True, status))
    if not checks:
        checks.append(Check("containers", True, "no containers to check"))
    return checks


def run() -> list[Check]:
    try:
        containers = docker_api.list_containers()
    except docker_api.DockerError as error:
        return [Check("docker", False, str(error))]
    return check_containers(containers)
