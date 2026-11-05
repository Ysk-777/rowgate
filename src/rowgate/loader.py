"""Read a rules file, parse its YAML and validate it against the rules format."""

from __future__ import annotations

import difflib
import types
import typing
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails
from yaml.constructor import ConstructorError

from rowgate.errors import RulesError
from rowgate.rules import RULE_MODELS, RulesFile, Source, describe_value

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _RulesLoader(yaml.SafeLoader):
    """The PyYAML safe loader with two fixes for rules files.

    Duplicate mapping keys are an error. Plain PyYAML keeps the last value
    without a word, which hides typos such as writing ``column:`` twice.

    A scalar that cannot be built, such as the date ``2024-02-30`` or
    ``!!int abc``, becomes a YAML error with its position. Plain PyYAML lets
    a bare ValueError, KeyError or AttributeError escape.
    """

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except yaml.YAMLError:
            raise
        except (ValueError, KeyError, AttributeError, TypeError, OverflowError) as exc:
            tag = node.tag.rsplit(":", 1)[-1]
            shown = f"{node.value!r} " if isinstance(node, yaml.ScalarNode) else ""
            raise ConstructorError(
                None, None, f"cannot read the value {shown}as {tag}: {exc}", node.start_mark
            ) from None


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


def load_rules(path: Path) -> RulesFile:
    """Read, parse and validate a rules file. Raises RulesError listing every problem."""
    shown = str(path)
    data = parse_rules_yaml(read_rules_text(path), shown)
    try:
        return RulesFile.model_validate(data)
    except ValidationError as exc:
        raise RulesError(shown, describe_validation_errors(exc)) from None


def describe_validation_errors(exc: ValidationError) -> list[str]:
    """Turn pydantic errors into messages that name the check number and field."""
    return [_describe_error(error) for error in exc.errors(include_url=False)]


def _describe_error(error: ErrorDetails) -> str:
    where, model, field = _locate(error["loc"])
    message = _message(error, model, field)
    return f"{where}: {message}" if where else message


def _locate(loc: tuple[int | str, ...]) -> tuple[str, type[BaseModel] | None, str | None]:
    """Return a readable location, the model that owns the field, and the field name."""
    parts = list(loc)
    labels: list[str] = []
    model: type[BaseModel] | None = RulesFile
    prefix = ""
    if len(parts) >= 2 and parts[0] == "checks" and isinstance(parts[1], int):
        label = f"check {parts[1] + 1}"
        parts = parts[2:]
        model = None
        if parts and isinstance(parts[0], str) and parts[0] in RULE_MODELS:
            label += f" ({parts[0]})"
            model = RULE_MODELS[parts[0]]
            parts = parts[1:]
        labels.append(label)
    elif len(parts) >= 2 and parts[0] == "source":
        model = Source
        prefix = "source."
        parts = parts[1:]

    field: str | None = None
    if parts:
        field = str(parts[0])
        labels.append(f"field '{prefix}{field}'")
        labels.extend(
            f"item {part + 1}" if isinstance(part, int) else str(part) for part in parts[1:]
        )
    return ", ".join(labels), model, field


def _message(error: ErrorDetails, model: type[BaseModel] | None, field: str | None) -> str:
    kind = error["type"]
    value = error.get("input")
    ctx = error.get("ctx", {})
    got = describe_value(value)
    if kind == "missing":
        return "required field is missing"
    if kind == "extra_forbidden":
        return _unknown_field(model, field)
    if kind == "union_tag_not_found":
        return f"missing required field 'rule' (one of: {', '.join(RULE_MODELS)})"
    if kind == "union_tag_invalid":
        return _unknown_rule(value)
    if kind in {"model_type", "model_attributes_type", "dict_type"}:
        if field is None and model is RulesFile:
            return (
                f"the rules file must be a mapping with 'version', 'source' and 'checks', got {got}"
            )
        return f"expected a mapping of fields, got {got}"
    if kind == "literal_error":
        if model is RulesFile and field == "version":
            return f"unsupported version; expected 1, got {got}"
        return f"expected {_choices(model, field)}, got {got}"
    if kind == "value_error":
        return str(ctx["error"])
    simple = {
        "string_type": f"expected a string, got {got}",
        "int_type": f"expected a whole number, got {got}",
        "list_type": f"expected a list, got {got}",
        "too_short": "must contain at least one item",
        "string_too_short": "must not be empty",
        "greater_than_equal": f"must be {ctx.get('ge')} or more, got {got}",
    }
    return simple.get(kind, error["msg"])


def _choices(model: type[BaseModel] | None, field: str | None) -> str:
    """The allowed values of a Literal field, written as 'a' or 'b'."""
    values: list[object] = []
    if model is not None and field in model.model_fields:
        values = _literal_values(model.model_fields[field].annotation)
    quoted = [repr(value) for value in values] or ["an allowed value"]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _literal_values(annotation: object) -> list[object]:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return list(typing.get_args(annotation))
    if origin in (typing.Union, types.UnionType):
        return [value for arg in typing.get_args(annotation) for value in _literal_values(arg)]
    return []


def _unknown_field(model: type[BaseModel] | None, field: str | None) -> str:
    if model is None or field is None:
        return "unknown field"
    allowed = sorted(model.model_fields, key=lambda name: (name != "rule", name))
    close = difflib.get_close_matches(field, allowed, n=1)
    if close:
        return f"unknown field (did you mean '{close[0]}'?)"
    return f"unknown field (allowed fields: {', '.join(allowed)})"


def _unknown_rule(value: object) -> str:
    rule = value.get("rule") if isinstance(value, dict) else None
    known = ", ".join(RULE_MODELS)
    if not isinstance(rule, str):
        return f"field 'rule' must be the name of a rule, got {describe_value(rule)}"
    close = difflib.get_close_matches(rule, list(RULE_MODELS), n=1)
    hint = f" (did you mean '{close[0]}'?)" if close else ""
    return f"unknown rule {rule!r}{hint}; known rules: {known}"
