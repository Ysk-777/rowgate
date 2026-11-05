"""Reading and parsing the rules file, before any rules-format validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rowgate.errors import RulesError
from rowgate.loader import _describe_marked_error, parse_rules_yaml, read_rules_text


def yaml_problems(text: str) -> list[str]:
    with pytest.raises(RulesError) as excinfo:
        parse_rules_yaml(text, "rules.yaml")
    assert excinfo.value.path == "rules.yaml"
    return excinfo.value.problems


def test_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    with pytest.raises(RulesError) as excinfo:
        read_rules_text(path)
    assert excinfo.value.problems == ["file not found"]
    assert excinfo.value.lines() == [f"error: {path}: file not found"]


def test_directory_instead_of_file(tmp_path: Path) -> None:
    with pytest.raises(RulesError) as excinfo:
        read_rules_text(tmp_path)
    assert excinfo.value.problems == ["is a directory, not a rules file"]


def test_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("version: 1\n", encoding="utf-8")

    def deny(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied")

    # Tests may run as root, where chmod cannot make a file unreadable.
    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(RulesError) as excinfo:
        read_rules_text(path)
    assert excinfo.value.problems == ["cannot read the file: Permission denied"]


def test_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_bytes(b"version: 1\n# caf\xe9\n")
    with pytest.raises(RulesError) as excinfo:
        read_rules_text(path)
    assert excinfo.value.problems == [
        "the file is not valid UTF-8 text (invalid byte at offset 16)"
    ]


def test_utf8_byte_order_mark_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_bytes("﻿version: 1\n".encode())
    assert read_rules_text(path) == "version: 1\n"


def test_valid_yaml_is_returned_as_data() -> None:
    data = parse_rules_yaml("version: 1\nchecks:\n  - rule: unique\n", "rules.yaml")
    assert data == {"version": 1, "checks": [{"rule": "unique"}]}


@pytest.mark.parametrize("text", ["", "\n\n", "# only a comment\n", "null\n", "---\n"])
def test_empty_document(text: str) -> None:
    assert yaml_problems(text) == ["the file is empty; expected 'version', 'source' and 'checks'"]


def test_syntax_error_reports_line_and_column() -> None:
    assert yaml_problems("version: 1\nsource: [a, b\nchecks: []\n") == [
        "invalid YAML at line 3, column 7: expected ',' or ']', but got ':'"
    ]


def test_tab_indentation_is_a_syntax_error() -> None:
    problems = yaml_problems("source:\n\tpath: a.csv\n")
    assert problems == [
        "invalid YAML at line 2, column 1: found character '\\t' that cannot start any token"
    ]


def test_duplicate_top_level_key() -> None:
    assert yaml_problems("version: 1\nversion: 1\n") == [
        "invalid YAML at line 2, column 1: found duplicate key 'version'"
    ]


def test_duplicate_key_inside_a_check() -> None:
    text = "checks:\n  - rule: not_null\n    column: a\n    column: b\n"
    assert yaml_problems(text) == ["invalid YAML at line 4, column 5: found duplicate key 'column'"]


def test_merge_keys_are_not_duplicates() -> None:
    text = "base: &base\n  rule: not_null\n  column: a\nmerged:\n  <<: *base\n  column: b\n"
    data = parse_rules_yaml(text, "rules.yaml")
    assert data == {
        "base": {"rule": "not_null", "column": "a"},
        "merged": {"rule": "not_null", "column": "b"},
    }


def test_unhashable_key() -> None:
    assert yaml_problems("? [a, b]\n: 1\n") == [
        "invalid YAML at line 1, column 3: found unhashable key"
    ]


def test_multiple_documents() -> None:
    assert yaml_problems("version: 1\n---\nversion: 1\n") == [
        "invalid YAML at line 2, column 1: but found another document"
    ]


def test_python_object_tags_are_refused() -> None:
    # The safe loader never builds Python objects or calls functions.
    problems = yaml_problems("checks: !!python/object/apply:os.system ['echo unsafe']\n")
    assert problems == [
        "invalid YAML at line 1, column 9: could not determine a constructor for the tag "
        "'tag:yaml.org,2002:python/object/apply:os.system'"
    ]


def test_control_characters_are_refused() -> None:
    assert yaml_problems("version: 1\x00\n") == [
        "invalid YAML: unacceptable character #x0000: special characters are not allowed "
        'in "<unicode string>", position 10'
    ]


def test_yaml_error_without_position_is_still_reported() -> None:
    error = yaml.MarkedYAMLError(problem="something odd")
    assert _describe_marked_error(error) == "invalid YAML: something odd"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            "a: 2024-02-30\n",
            "invalid YAML at line 1, column 4: cannot read the value '2024-02-30' as timestamp: "
            "day is out of range for month",
        ),
        (
            "a: 2024-01-01 25:00:00\n",
            "invalid YAML at line 1, column 4: cannot read the value '2024-01-01 25:00:00' "
            "as timestamp: hour must be in 0..23",
        ),
        (
            "a: !!int abc\n",
            "invalid YAML at line 1, column 4: cannot read the value 'abc' as int: "
            "invalid literal for int() with base 10: 'abc'",
        ),
        (
            "a: !!bool maybe\n",
            "invalid YAML at line 1, column 4: cannot read the value 'maybe' as bool: 'maybe'",
        ),
        (
            "a: !!float x\n",
            "invalid YAML at line 1, column 4: cannot read the value 'x' as float: "
            "could not convert string to float: 'x'",
        ),
        (
            "a:\n  - !!timestamp nope\n",
            "invalid YAML at line 2, column 5: cannot read the value 'nope' as timestamp: "
            "'NoneType' object has no attribute 'groupdict'",
        ),
    ],
)
def test_scalars_that_cannot_be_built_are_yaml_errors(text: str, problem: str) -> None:
    assert yaml_problems(text) == [problem]
