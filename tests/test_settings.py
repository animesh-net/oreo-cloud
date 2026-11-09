from pathlib import Path

import pytest

from oreo import cli, settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # load() writes straight into os.environ; keep that from leaking into other tests.
    monkeypatch.setattr(settings.os, "environ", dict(settings.os.environ))


@pytest.fixture
def share(tmp_path, monkeypatch):
    monkeypatch.setenv("OREO_SHARE_DIR", str(tmp_path))
    monkeypatch.delenv("OREO_SETTINGS_FILE", raising=False)
    return tmp_path


def test_settings_fill_in_unset_variables(share, monkeypatch):
    (share / "settings.env").write_text("# comment\n\nOREO_X_ONE=1\nOREO_X_TWO = two\n")
    monkeypatch.delenv("OREO_X_ONE", raising=False)
    monkeypatch.delenv("OREO_X_TWO", raising=False)
    settings.load()
    assert settings.os.environ["OREO_X_ONE"] == "1"
    assert settings.os.environ["OREO_X_TWO"] == "two"


def test_env_overrides_the_built_in_setting(share, monkeypatch):
    (share / "settings.env").write_text("OREO_X_ONE=1\n")
    monkeypatch.setenv("OREO_X_ONE", "from-env")
    settings.load()
    assert settings.os.environ["OREO_X_ONE"] == "from-env"


def test_empty_settings_file_variable_skips_them(share, monkeypatch):
    (share / "settings.env").write_text("OREO_X_ONE=1\n")
    monkeypatch.delenv("OREO_X_ONE", raising=False)
    monkeypatch.setenv("OREO_SETTINGS_FILE", "")
    settings.load()
    assert "OREO_X_ONE" not in settings.os.environ


def test_missing_settings_file_is_ignored(share):
    settings.load()


def test_repository_settings_parse(monkeypatch):
    path = settings.Path(__file__).parent.parent / "config" / "settings.env"
    monkeypatch.setenv("OREO_SETTINGS_FILE", str(path))
    monkeypatch.delenv("OREO_MEMORY_MAX_PERCENT", raising=False)
    settings.load()
    assert float(settings.os.environ["OREO_MEMORY_MAX_PERCENT"]) == 70


def test_the_cutover_script_ships_in_the_image():
    assert settings.FILES["migrate"] == "migrate.sh"
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    assert "COPY pi/migrate.sh /usr/share/oreo/migrate.sh" in dockerfile


def test_show_prints_the_shipped_file(share, capsys):
    (share / "compose.yaml").write_text("services: {}\n")
    assert cli.main(["show", "compose"]) == 0
    assert capsys.readouterr().out == "services: {}\n"


def test_compose_matching_the_image_passes(share, monkeypatch, tmp_path):
    (share / "compose.yaml").write_text("same\n")
    host = tmp_path / "host-compose.yaml"
    host.write_text("same\n")
    monkeypatch.setenv("OREO_HOST_COMPOSE_FILE", str(host))
    assert settings.compose_check().ok


def test_outdated_compose_fails_with_the_refresh_command(share, monkeypatch, tmp_path):
    (share / "compose.yaml").write_text("new\n")
    host = tmp_path / "host-compose.yaml"
    host.write_text("old\n")
    monkeypatch.setenv("OREO_HOST_COMPOSE_FILE", str(host))
    check = settings.compose_check()
    assert not check.ok
    assert "show compose > compose.yaml" in check.detail
    assert check.detail.split("refresh it: ")[1].startswith("image-update, or: ")


def test_compose_not_found_is_not_a_problem(share, monkeypatch, tmp_path):
    monkeypatch.setenv("OREO_HOST_COMPOSE_FILE", str(tmp_path / "missing.yaml"))
    assert settings.compose_check().ok
