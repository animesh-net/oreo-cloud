"""Container health checks."""

from oreo import docker_api
from oreo.checks import Check

IGNORE_LABEL = "oreo.ignore"


def check_containers(containers: list[dict]) -> list[Check]:
    checks = []
    for container in containers:
        if container.get("Labels", {}).get(IGNORE_LABEL) == "true":
            continue
        name = container["Names"][0].lstrip("/")
        state = container.get("State", "unknown")
        status = container.get("Status", "")
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
