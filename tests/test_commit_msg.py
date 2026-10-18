import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_commit_msg", Path(__file__).parent.parent / "scripts" / "check_commit_msg.py"
)
check_commit_msg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_commit_msg)
problems = check_commit_msg.problems


def test_good_message_passes():
    assert problems("Add backup check\n") == []


def test_lowercase_subject_fails():
    assert problems("add backup check") == ["subject must start with a capital letter"]


def test_long_subject_fails():
    [problem] = problems("A" * 73)
    assert "73 characters" in problem


def test_72_characters_is_allowed():
    assert problems("A" * 72) == []


def test_wip_fails():
    assert "never commit WIP" in problems("WIP backup check")


def test_body_fails():
    assert problems("Add backup check\n\nLonger explanation") == [
        "message must be a single line with no body"
    ]


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-Authored-By: Claude Opus <noreply@anthropic.com>",
        "co-authored-by: GitHub Copilot <copilot@github.com>",
        "Generated with Claude Code",
    ],
)
def test_ai_attribution_fails(trailer):
    assert "message contains AI attribution" in problems(f"Add backup check\n\n{trailer}")


def test_comments_and_verbose_diff_are_ignored():
    message = (
        "Add backup check\n\n"
        "# Please enter the commit message for your changes.\n"
        f"{check_commit_msg.SCISSORS}\n"
        "diff --git a/x b/x\n"
    )
    assert problems(message) == []


def test_empty_message_fails():
    assert problems("\n# only a comment\n") == ["message is empty"]


def test_main_reads_file(tmp_path, capsys):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("fix stuff\n")
    assert check_commit_msg.main([str(message)]) == 1
    assert "capital letter" in capsys.readouterr().err


def test_main_reads_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Add backup check\n"))
    assert check_commit_msg.main(["-"]) == 0
