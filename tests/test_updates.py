import io
import json
import subprocess
import urllib.error

import pytest

from oreo import updates

APT = (
    (
        "NOTE: This is only a simulation!\n"
        "Inst libc6 [2.39-0ubuntu8.3] "
        "(2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [arm64])\n"
    )
    + """Inst docker-ce [5:28.5.0-1] (5:28.5.1-1 Docker CE:noble [arm64])
Inst linux-image-6.8.0-1040-raspi (6.8.0-1040.44 Ubuntu:24.04/noble-updates [arm64])
Conf libc6 (2.39-0ubuntu8.4 Ubuntu:24.04/noble-updates [arm64])
"""
)

RUNNING = (
    "ghcr.io/immich-app/immich-server:release\n"
    "ghcr.io/animesh-net/oreo-cloud:latest\n"
    "ghcr.io/animesh-net/oreo-cloud-proxy:latest\n"
)
PINNED = "ghcr.io/immich-app/postgres:14@sha256:abc\n"
LOCAL = {
    "ghcr.io/immich-app/immich-server:release": ["ghcr.io/immich-app/immich-server@sha256:old"],
    "ghcr.io/animesh-net/oreo-cloud:latest": ["ghcr.io/animesh-net/oreo-cloud@sha256:same"],
    "ghcr.io/animesh-net/oreo-cloud-proxy:latest": [
        "ghcr.io/animesh-net/oreo-cloud-proxy@sha256:proxy"
    ],
    "local-build": [],
}


class FakeHost:
    def __init__(self, apt=APT, apt_code=0, ps=RUNNING + PINNED + "local-build\n"):
        self.apt, self.apt_code, self.ps = apt, apt_code, ps

    def __call__(self, command):
        if command[0] == "apt-get":
            return subprocess.CompletedProcess(command, self.apt_code, self.apt, "")
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, self.ps, "")
        ref = command[-1]
        if ref in LOCAL:
            return subprocess.CompletedProcess(command, 0, json.dumps(LOCAL[ref]), "")
        return subprocess.CompletedProcess(command, 1, "", "No such image")


REMOTE = {
    "ghcr.io/immich-app/immich-server:release": "sha256:new",
    "ghcr.io/animesh-net/oreo-cloud:latest": "sha256:same",
    "ghcr.io/animesh-net/oreo-cloud-proxy:latest": "sha256:proxy",
}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "HOST", tmp_path)
    return tmp_path


def test_os_packages_counts_upgrades_and_security_ones():
    names, security = updates.os_packages(FakeHost())
    assert names == ["libc6", "docker-ce", "linux-image-6.8.0-1040-raspi"]
    assert security == ["libc6"]


@pytest.mark.parametrize(
    ("ref", "parsed"),
    [
        ("caddy:2", ("docker.io", "library/caddy", "2")),
        ("caddy", ("docker.io", "library/caddy", "latest")),
        ("valkey/valkey:8", ("docker.io", "valkey/valkey", "8")),
        ("ghcr.io/immich-app/immich-server:v2", ("ghcr.io", "immich-app/immich-server", "v2")),
        ("localhost:5000/app", ("localhost:5000", "app", "latest")),
        ("ghcr.io/immich-app/postgres:14@sha256:abc", None),
    ],
)
def test_parse_reference(ref, parsed):
    assert updates.parse_reference(ref) == parsed


def test_newer_images_skips_pinned_and_local_only_images(host):
    asked = []

    def remote(ref):
        asked.append(ref)
        return REMOTE[ref]

    newer, unchecked = updates.newer_images(FakeHost(), remote)
    assert newer == ["immich-server"]
    assert unchecked == []
    assert sorted(asked) == sorted(REMOTE)


def test_an_image_that_cannot_be_checked_is_named(host):
    def remote(ref):
        if ref.endswith("/oreo-cloud:latest"):
            raise urllib.error.URLError("timed out")
        return REMOTE[ref]

    newer, unchecked = updates.newer_images(FakeHost(), remote)
    assert unchecked == ["oreo-cloud (timed out)"]


def test_summary_lines(host):
    lines = updates.summary(FakeHost(), lambda ref: REMOTE[ref])
    assert lines == [
        "⬆️ OS: 3 updates available, 1 security, Docker: docker-ce",
        "👉 Update the OS: os-update, or: "
        "cd ~/oreo-cloud && docker compose run --rm agent update os",
        "⬆️ Images: newer immich-server",
        "👉 Update the images: image-update, or: "
        "cd ~/oreo-cloud && docker compose run --rm agent update all"
        " (read Immich's release notes first)",
    ]


def test_a_newer_oreo_image_is_pulled_before_update_all():
    assert updates._images_command(["oreo-cloud", "oreo-cloud-proxy"]) == (
        "image-update, or: cd ~/oreo-cloud && docker compose pull heartbeat agent"
        " && docker run --rm ghcr.io/animesh-net/oreo-cloud show compose > compose.yaml"
        " && docker compose up -d"
        " && docker compose run --rm agent update all"
    )


def test_a_newer_oreo_ai_image_is_updated_in_its_own_folder():
    assert updates._images_command(["oreo-ai-web"]) == (
        "image-update, or: cd ~/oreo-cloud && docker compose run --rm agent update all"
        " && docker run --rm --pull always ghcr.io/animesh-net/oreo-ai | tar -xf - -C ~/oreo-ai"
        " && bash ~/oreo-ai/scripts/oreo-ai.sh on"
    )


def test_summary_when_everything_is_current(host):
    fake = FakeHost(apt="NOTE: This is only a simulation!\n", ps=RUNNING)
    lines = updates.summary(fake, lambda ref: LOCAL[ref][0].split("@")[1])
    assert lines == ["✅ OS: up to date", "✅ Images: up to date"]


def test_summary_never_raises(host):
    def broken(command):
        raise subprocess.TimeoutExpired(command, 60)

    lines = updates.summary(broken)
    assert lines[0].startswith("❔ OS: could not check for updates")
    assert lines[1].startswith("❔ Images: could not check for updates")


def test_no_line_warns_so_the_report_title_stays_ok(host):
    for line in updates.summary(FakeHost(apt_code=100), lambda ref: REMOTE[ref]):
        assert "❌" not in line and "⚠" not in line


def test_report_adds_a_pending_reboot(host, monkeypatch):
    monkeypatch.setattr(updates, "summary", lambda: ["✅ OS: up to date"])
    assert updates.report() == ["✅ OS: up to date"]
    (host / "var/run").mkdir(parents=True)
    (host / "var/run/reboot-required").touch()
    assert updates.report()[-1] == "🔁 Reboot needed after earlier updates (sudo reboot)"


class Response:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self.body = io.BytesIO(body)

    def read(self, *args):
        return self.body.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_remote_digest_logs_in_with_the_pis_registry_login(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"auths": {"ghcr.io": {"auth": "dXNlcjp0b2tlbg=="}}}))
    monkeypatch.setattr(updates, "DOCKER_CONFIGS", (config,))
    seen = []

    def opener(request, timeout):
        seen.append(request)
        if request.full_url.startswith("https://ghcr.io/token"):
            return Response(body=b'{"token": "t0k"}')
        if request.get_header("Authorization") != "Bearer t0k":
            headers = {
                "WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:animesh-net/oreo-cloud:pull"'
            }
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", headers, None)
        return Response({"Docker-Content-Digest": "sha256:remote"})

    assert updates.remote_digest("ghcr.io/animesh-net/oreo-cloud:latest", opener) == "sha256:remote"
    first, token, second = seen
    assert first.get_method() == "HEAD"
    assert first.full_url == "https://ghcr.io/v2/animesh-net/oreo-cloud/manifests/latest"
    assert "scope=repository%3Aanimesh-net%2Foreo-cloud%3Apull" in token.full_url
    assert token.get_header("Authorization") == "Basic dXNlcjp0b2tlbg=="
    assert "image.index" in second.get_header("Accept")


def test_docker_hub_images_use_its_registry_host():
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return Response({"Docker-Content-Digest": "sha256:x"})

    updates.remote_digest("caddy:2", opener)
    assert seen == ["https://registry-1.docker.io/v2/library/caddy/manifests/2"]


def test_reboot_check_survives_an_unreadable_host(monkeypatch):
    class Unreadable:
        def __truediv__(self, other):
            return self

        def exists(self):
            raise PermissionError("/proc/1/root")

    monkeypatch.setattr(updates, "HOST", Unreadable())
    assert updates.reboot_needed() is False
