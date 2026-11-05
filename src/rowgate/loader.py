"""Read a rules file from disk and parse its YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from rowgate.errors import RulesError

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _RulesLoader(yaml.SafeLoader):
    """The PyYAML safe loader, but duplicate mapping keys are an error.

    Plain PyYAML keeps the last value of a duplicated key without a word,
    which hides typos such as writing ``column:`` twice in one check.
    """


def _construct_mapping(loader: _RulesLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[object] = set()
    for key_node, _value_node in node.value:
        if key_node.tag == _MERGE_TAG:
            continue
        key = loader.construct_object(key_node)
        try:
            duplicate = key in seen
        except TypeError:
            # Unhashable key: the base constructor below reports it.
            continue
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node)


_RulesLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def read_rules_text(path: Path) -> str:
    """Return the text of a rules file, decoded as UTF-8."""
    shown = str(path)
    if path.is_dir():
        raise RulesError(shown, ["is a directory, not a rules file"])
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise RulesError(shown, ["file not found"]) from None
    except OSError as exc:
        raise RulesError(shown, [f"cannot read the file: {exc.strerror or exc}"]) from None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RulesError(
            shown, [f"the file is not valid UTF-8 text (invalid byte at offset {exc.start})"]
        ) from None


def parse_rules_yaml(text: str, shown: str) -> object:
    """Parse rules YAML with the safe loader. ``shown`` names the file in errors."""
    try:
        loader = _RulesLoader(text)  # the reader rejects control characters here
        try:
            data: object = loader.get_single_data()
        finally:
            loader.dispose()
    except yaml.MarkedYAMLError as exc:
        raise RulesError(shown, [_describe_marked_error(exc)]) from None
    except yaml.YAMLError as exc:
        raise RulesError(shown, [f"invalid YAML: {_one_line(str(exc))}"]) from None
    if data is None:
        raise RulesError(shown, ["the file is empty; expected 'version', 'source' and 'checks'"])
    return data


def _describe_marked_error(exc: yaml.MarkedYAMLError) -> str:
    problem = exc.problem or exc.context or "the YAML cannot be parsed"
    mark = exc.problem_mark or exc.context_mark
    if mark is None:
        return f"invalid YAML: {problem}"
    return f"invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}"


def _one_line(text: str) -> str:
    return " ".join(text.split())
