"""``rowgate init``: a starter rules file that validates, and never an overwrite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rowgate.cli import app
from rowgate.starter import STARTER_RULES


def native(*parts: str) -> str:
    """A relative path written with this system's separator."""
    return str(Path(*parts))


def run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_writes_rowgate_yaml_by_default(in_tmp: Path) -> None:
    code, out, err = run("init")
    assert (code, err) == (0, "")
    assert out == (
        "created rowgate.yaml; set source.path to your data file, then run "
        "rowgate validate rowgate.yaml\n"
    )
    written = (in_tmp / "rowgate.yaml").read_bytes()
    assert written == STARTER_RULES.encode("utf-8")
    assert b"\r" not in written  # LF line endings on every system


def test_the_starter_file_passes_validate(in_tmp: Path) -> None:
    run("init")
    assert run("validate", "rowgate.yaml") == (
        0,
        "rowgate.yaml is valid: 1 check on data.csv (csv)\n",
        "",
    )


def test_every_example_check_in_the_starter_file_is_valid(in_tmp: Path) -> None:
    # Removing "# " from each example check, as the comment says, gives a
    # rules file with all seven rules that also passes validate.
    lines = []
    for line in STARTER_RULES.splitlines(keepends=True):
        if line.startswith(("  # - ", "  #   ")):
            line = "  " + line[4:]
        lines.append(line)
    (in_tmp / "all.yaml").write_text("".join(lines), encoding="utf-8")
    code, out, _ = run("validate", "all.yaml")
    assert (code, out) == (0, "all.yaml is valid: 7 checks on data.csv (csv)\n")


def test_the_starter_file_runs_against_a_data_file(in_tmp: Path) -> None:
    run("init")
    (in_tmp / "data.csv").write_bytes(b"order_id,status\n1,new\n")
    code, out, _ = run("check", "rowgate.yaml")
    assert code == 0
    assert "PASS  1. row_count: 1 row, expected at least 1" in out


def test_init_writes_to_a_given_path(in_tmp: Path) -> None:
    (in_tmp / "rules").mkdir()
    code, out, _ = run("init", native("rules", "orders.yaml"))
    assert code == 0
    assert (in_tmp / "rules" / "orders.yaml").read_text(encoding="utf-8") == STARTER_RULES
    assert out.startswith(f"created {native('rules', 'orders.yaml')};")


def test_init_never_overwrites_a_file(in_tmp: Path) -> None:
    (in_tmp / "rowgate.yaml").write_text("my own rules\n", encoding="utf-8")
    code, out, err = run("init")
    assert (code, out) == (2, "")
    assert err == (
        "error: 'rowgate.yaml' already exists; RowGate never overwrites a file, so choose "
        "another name or move that file away\n"
    )
    assert (in_tmp / "rowgate.yaml").read_text(encoding="utf-8") == "my own rules\n"


def test_init_does_not_follow_a_link_to_a_missing_file(in_tmp: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("creating symbolic links needs extra rights on Windows")
    (in_tmp / "rowgate.yaml").symlink_to(in_tmp / "elsewhere.yaml")
    code, _, err = run("init")
    assert code == 2
    assert "already exists" in err
    assert not (in_tmp / "elsewhere.yaml").exists()


def test_init_needs_an_existing_folder(in_tmp: Path) -> None:
    code, out, err = run("init", native("missing", "rules.yaml"))
    assert (code, out) == (2, "")
    assert err == (
        f"error: cannot create '{native('missing', 'rules.yaml')}': 'missing' does not exist\n"
    )
    assert not (in_tmp / "missing").exists()


def test_init_refuses_a_folder_and_a_file_used_as_a_folder(in_tmp: Path) -> None:
    (in_tmp / "rules").mkdir()
    code, _, err = run("init", "rules")
    assert code == 2
    assert err == (
        f"error: 'rules' is a folder; give a file name, such as "
        f"'{native('rules', 'rowgate.yaml')}'\n"
    )
    (in_tmp / "notes.txt").write_text("x", encoding="utf-8")
    code, _, err = run("init", native("notes.txt", "rules.yaml"))
    assert code == 2
    assert err.endswith("'notes.txt' is a file, not a folder\n")


def test_init_refuses_urls(in_tmp: Path) -> None:
    code, _, err = run("init", "https://example.invalid/rules.yaml")
    assert code == 2
    assert err == (
        "error: 'https://example.invalid/rules.yaml' looks like a URL; "
        "RowGate writes local files only\n"
    )


def test_init_reports_a_write_failure_in_one_line(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", refused)
    code, out, err = run("init")
    assert (code, out) == (2, "")
    assert err == "error: cannot create 'rowgate.yaml': Permission denied\n"


def test_init_shows_windows_style_paths_as_written(in_tmp: Path) -> None:
    # a\rules.yaml: a file named with a backslash on Linux and macOS, and
    # rules.yaml in the folder a on Windows.
    (in_tmp / "a").mkdir()
    code, out, _ = run("init", "a\\rules.yaml")
    assert code == 0
    assert out.startswith("created a\\rules.yaml;")


def test_init_refuses_an_empty_path(in_tmp: Path) -> None:
    code, out, err = run("init", "")
    assert (code, out) == (2, "")
    assert err == "error: the file path is empty; give a file name, such as 'rowgate.yaml'\n"
    assert list(in_tmp.iterdir()) == []
