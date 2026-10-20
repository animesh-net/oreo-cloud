"""What could be updated: OS packages and the images of the running containers.

Only looks; nothing is installed or pulled. The nightly report and `oreo update status` show
the result, and updating stays a manual `oreo update` command. OS packages come from the
package lists Ubuntu refreshes every day; images are compared with their registry by digest.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from oreo import net

HOST = Path("/proc/1/root")
# Caddy is built on the Pi, so its own image has no registry to compare with; its base images do.
CADDY_DOCKERFILE = HOST / "home/animesh/caddy/Dockerfile"
# Registry logins, for private images such as this one.
DOCKER_CONFIGS = (HOST / "home/animesh/.docker/config.json", HOST / "root/.docker/config.json")
DOCKER_PACKAGES = ("docker", "containerd", "runc")
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
TIMEOUT = 15

Opener = Callable[..., object]
RemoteDigest = Callable[[str], str]


class UpdateCheckError(Exception):
    pass


def os_packages(run: net.Runner = net.host_run) -> tuple[list[str], list[str]]:
    """Names of the packages an upgrade would install, and those coming from a security pocket."""
    result = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"])
    if result.returncode != 0:
        raise UpdateCheckError("apt-get could not simulate an upgrade")
    names, security = [], []
    for line in result.stdout.splitlines():
        match = re.match(r"Inst (\S+) (?:\[[^\]]*\] )?\((.*)\)", line)
        if match:
            names.append(match[1])
            if "-security" in match[2]:
                security.append(match[1])
    return names, security


def parse_reference(ref: str) -> tuple[str, str, str] | None:
    """Registry, repository and tag of an image reference; None when it is pinned to a digest,
    as Immich pins its database images on purpose."""
    if "@" in ref:
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return first, rest, tag
    return "docker.io", name if "/" in name else f"library/{name}", tag


def _credentials(registry: str) -> str | None:
    keys = [registry, f"https://{registry}"]
    if registry == "docker.io":
        keys += ["https://index.docker.io/v1/", "index.docker.io"]
    for path in DOCKER_CONFIGS:
        try:
            auths = json.loads(path.read_text()).get("auths", {})
        except (OSError, ValueError):
            continue
        for key in keys:
            if auths.get(key, {}).get("auth"):
                return auths[key]["auth"]
    return None


def _head(url: str, headers: dict, opener: Opener) -> str:
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    with opener(request, timeout=TIMEOUT) as response:
        return response.headers["Docker-Content-Digest"]


def remote_digest(ref: str, opener: Opener = urllib.request.urlopen) -> str:
    """The digest the registry serves for `ref` now, logging in with a token when it asks."""
    registry, repository, tag = parse_reference(ref)
    host = "registry-1.docker.io" if registry == "docker.io" else registry
    url = f"https://{host}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": ACCEPT}
    try:
        return _head(url, headers, opener)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        challenge = error.headers.get("WWW-Authenticate", "")
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    token_request = urllib.request.Request(
        params.pop("realm") + "?" + urllib.parse.urlencode(params)
    )
    login = _credentials(registry)
    if login:
        token_request.add_header("Authorization", f"Basic {login}")
    with opener(token_request, timeout=TIMEOUT) as response:
        body = json.load(response)
    headers["Authorization"] = f"Bearer {body.get('token') or body['access_token']}"
    return _head(url, headers, opener)


def _base_images() -> list[str]:
    try:
        text = CADDY_DOCKERFILE.read_text()
    except OSError:
        return []
    refs, stages = [], set()
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].upper() != "FROM":
            continue
        words = [word for word in words[1:] if not word.startswith("--")]
        if not words:
            continue
        if words[0] not in stages and words[0] != "scratch" and "$" not in words[0]:
            refs.append(words[0])
        if len(words) == 3 and words[1].upper() == "AS":
            stages.add(words[2])
    return refs


def _local_digests(run: net.Runner, ref: str) -> set[str]:
    result = run(["docker", "image", "inspect", "-f", "{{json .RepoDigests}}", ref])
    if result.returncode != 0:
        return set()
    try:
        return {digest.split("@", 1)[1] for digest in json.loads(result.stdout) or []}
    except (ValueError, IndexError):
        return set()


def newer_images(
    run: net.Runner = net.host_run, remote: RemoteDigest = remote_digest
) -> tuple[list[str], list[str]]:
    """Images, by short name, with a newer version in their registry, and those that could
    not be checked. Images built on the Pi are skipped; their base images are checked."""
    result = run(["docker", "ps", "--format", "{{.Image}}"])
    if result.returncode != 0:
        raise UpdateCheckError("docker ps failed")
    refs = list(dict.fromkeys([*result.stdout.split(), *_base_images()]))
    newer, unchecked = [], []
    for ref in refs:
        if parse_reference(ref) is None:
            continue
        local = _local_digests(run, ref)
        if not local:
            continue
        name = parse_reference(ref)[1].rsplit("/", 1)[-1]
        try:
            if remote(ref) not in local:
                newer.append(name)
        except (OSError, KeyError, ValueError) as error:
            unchecked.append(f"{name} ({getattr(error, 'reason', error)})")
    return sorted(set(newer)), unchecked


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def summary(run: net.Runner = net.host_run, remote: RemoteDigest = remote_digest) -> list[str]:
    """One line for the OS and one for the images. Never raises: a check that cannot run says so."""
    lines = []
    try:
        names, security = os_packages(run)
        if names:
            parts = [_plural(len(names), "update") + " available"]
            if security:
                parts.append(f"{len(security)} security")
            docker = [name for name in names if name.startswith(DOCKER_PACKAGES)]
            if docker:
                parts.append("Docker: " + ", ".join(docker))
            lines.append(f"⬆️ OS: {', '.join(parts)} (update os)")
        else:
            lines.append("✅ OS: up to date")
    except Exception as error:  # the report must go out whatever this check does
        lines.append(f"❔ OS: could not check for updates ({error})")
    try:
        newer, unchecked = newer_images(run, remote)
        if newer:
            lines.append(
                f"⬆️ Images: newer {', '.join(newer)} (update everything: README, Everyday use)"
            )
        else:
            lines.append("✅ Images: up to date")
        if unchecked:
            lines.append(f"❔ Images not checked: {', '.join(unchecked)}")
    except Exception as error:
        lines.append(f"❔ Images: could not check for updates ({error})")
    return lines


def reboot_needed() -> bool:
    try:
        return (HOST / "var/run/reboot-required").exists()
    except OSError:  # the host is out of reach, as outside the agent container
        return False


def report() -> list[str]:
    """The lines the nightly report adds: available updates, and a reboot if one is pending."""
    lines = summary()
    if reboot_needed():
        lines.append("🔁 Reboot needed after earlier updates (sudo reboot)")
    return lines
