"""Read a rules file, parse its YAML and validate it against the rules format."""

from __future__ import annotations

import difflib
import re
import sys
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError

from rowgate import display
from rowgate.errors import RulesError
from rowgate.rules import RULE_MODELS, RulesFile, Source, describe_value

_MERGE_TAG = "tag:yaml.org,2002:merge"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_STR_TAG = "tag:yaml.org,2002:str"
_VALUE_TAG = "tag:yaml.org,2002:value"
_AMBIGUOUS_TAG = "tag:rowgate,2026:ambiguous"

_PLAIN_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)")
_LEADING_ZERO_INT = re.compile(r"[-+]?[0-9]+")
_OCTAL_1_2 = re.compile(r"[-+]?0o[0-7]+")
_NUMBER_LIKE = re.compile(
    r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[-+]?0o[0-7]+"
)

# Rules files need about five levels; anything far deeper is a mistake or an
# attack, and would otherwise exhaust Python's recursion limit.
MAX_NESTING = 50


@dataclass(frozen=True)
class AmbiguousScalar:
    """An unquoted value that YAML 1.1 reads in a way people rarely expect.

    PyYAML implements YAML 1.1, where ``0123`` is the octal number 83, ``1:30``
    is 90, ``1_000`` is 1000 and ``yes`` is a boolean, while ``1e3`` and ``08``
    are text. RowGate refuses such values unless they are quoted, and accepts
    only ``true`` and ``false`` as booleans.
    """

    text: str
    meaning: object  # what PyYAML would have read
    line: int
    column: int

    def describe(self) -> str:
        where = f"(line {self.line}, column {self.column})"
        quoted = f"put it in quotes ('{self.text}') for text"
        if isinstance(self.meaning, bool):
            word = "true" if self.meaning else "false"
            return (
                f"unquoted {self.text} is read by YAML as the boolean {word}; write true or "
                f"false for a boolean, or {quoted} {where}"
            )
        if isinstance(self.meaning, int | Decimal):
            return (
                f"unquoted {self.text} is read by YAML as the number {self.meaning}; write "
                f"{self.meaning} if you mean that number, or {quoted} {where}"
            )
        number = _number_it_looks_like(self.text)
        return (
            f"unquoted {self.text} is read by YAML as text, not as the number {number}; write "
            f"{number} if you mean that number, or {quoted} {where}"
        )


def _number_it_looks_like(text: str) -> int | float:
    if _OCTAL_1_2.fullmatch(text):
        sign = -1 if text.startswith("-") else 1
        return sign * int(text.lstrip("+-")[2:], 8)
    if _LEADING_ZERO_INT.fullmatch(text):
        return int(text)
    return float(text)


def _is_ambiguous(tag: str, value: str) -> bool:
    if tag == _BOOL_TAG:
        return value not in ("true", "false")
    if tag == _INT_TAG:
        return _PLAIN_INT.fullmatch(value) is None
    if tag == _FLOAT_TAG:
        return "_" in value or ":" in value
    if tag == _STR_TAG:
        return _NUMBER_LIKE.fullmatch(value) is not None
    return False


class _RulesLoader(yaml.SafeLoader):
    """The PyYAML safe loader with fixes for rules files.

    Duplicate mapping keys are an error. Plain PyYAML keeps the last value
    without a word, which hides typos such as writing ``column:`` twice.

    A scalar that cannot be built, such as the date ``2024-02-30`` or
    ``!!int abc``, becomes a YAML error with its position. Plain PyYAML lets
    a bare ValueError, KeyError or AttributeError escape.

    Nesting deeper than MAX_NESTING is an error. Plain PyYAML recurses once
    per level and crashes with RecursionError.

    Unquoted values that YAML 1.1 reads surprisingly become AmbiguousScalar
    markers, reported later with their check number and field. A bare ``=``
    is text instead of PyYAML's unconstructable "value" tag.
    """

    def resolve(
        self, kind: type[yaml.Node], value: str | None, implicit: tuple[bool, bool] | bool
    ) -> str:
        # types-PyYAML leaves Resolver.resolve unannotated; it returns a tag string.
        tag: str = super().resolve(kind, value, implicit)  # type: ignore[no-untyped-call]
        # For scalars, implicit is (plain, quoted); for lists and mappings it is a bool.
        plain = isinstance(implicit, tuple) and implicit[0]
        if kind is yaml.ScalarNode and plain and value is not None:
            if tag == _VALUE_TAG:
                return _STR_TAG
            if _is_ambiguous(tag, value):
                return _AMBIGUOUS_TAG
        return tag

    def construct_ambiguous(self, node: yaml.ScalarNode) -> AmbiguousScalar:
        tag = super().resolve(yaml.ScalarNode, node.value, (True, False))  # type: ignore[no-untyped-call]
        meaning = node.value if tag == _STR_TAG else self.yaml_constructors[tag](self, node)
        mark = node.start_mark
        return AmbiguousScalar(node.value, meaning, mark.line + 1, mark.column + 1)

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._nesting = 0

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        self._nesting += 1
        try:
            if self._nesting > MAX_NESTING:
                raise ComposerError(
                    None,
                    None,
                    f"the YAML is nested more than {MAX_NESTING} levels deep",
                    parent.start_mark if parent is not None else None,
                )
            return super().compose_node(parent, index)
        finally:
            self._nesting -= 1

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except yaml.YAMLError:
            raise
        except (ValueError, KeyError, AttributeError, TypeError, OverflowError) as exc:
            tag = node.tag.rsplit(":", 1)[-1]
            shown = f"{display.quoted(node.value)} " if isinstance(node, yaml.ScalarNode) else ""
            raise ConstructorError(
                None, None, f"cannot read the value {shown}as {tag}: {exc}", node.start_mark
            ) from None


def _construct_mapping(loader: _RulesLoader, node: yaml.MappingNode) -> Iterator[dict[Any, Any]]:
    # A generator, like PyYAML's own: the empty dict is handed out first and
    # filled later, so a mapping that refers to itself through an alias can be
    # built (and then rejected with a clear message) instead of failing.
    data: dict[Any, Any] = {}
    yield data
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
                f"found duplicate key {display.shown(key)}",
                key_node.start_mark,
            )
        seen.add(key)
    data.update(loader.construct_mapping(node))


def _construct_decimal(loader: _RulesLoader, node: yaml.ScalarNode) -> Decimal:
    """Read a YAML float as an exact Decimal: 0.1 is one tenth, not the float nearest to it.

    Bounds such as 12345678.12345678901 keep every digit, so data can be
    compared with them exactly.
    """
    text = loader.construct_scalar(node).replace("_", "")
    special = _SPECIAL_FLOATS.get(text.lower())
    if special is not None:
        return Decimal(special)
    if ":" in text:  # base 60, only with an explicit !!float tag; PyYAML reads it
        return Decimal(repr(loader.construct_yaml_float(node)))
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError("not a number") from None


def _construct_int(loader: _RulesLoader, node: yaml.ScalarNode) -> int | Decimal:
    """Read a YAML int; one too long for Python's int() becomes an exact Decimal.

    Python refuses to turn text of more than sys.get_int_max_str_digits()
    digits (4,300 by default) into an int. Such a number is read as the
    whole Decimal it is, exactly as if it were written with ".0", so a whole
    number of any size is accepted.
    """
    text = loader.construct_scalar(node)
    limit = sys.get_int_max_str_digits()
    if limit and _LONG_INT.fullmatch(text) and len(text.lstrip("+-")) > limit:
        return Decimal(text)
    value: int = loader.construct_yaml_int(node)
    return value


_LONG_INT = re.compile(r"[-+]?[0-9]+")

_SPECIAL_FLOATS = {
    ".inf": "Infinity",
    "+.inf": "Infinity",
    "-.inf": "-Infinity",
    ".nan": "NaN",
}

_RulesLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_RulesLoader.add_constructor(_AMBIGUOUS_TAG, _RulesLoader.construct_ambiguous)
_RulesLoader.add_constructor(_FLOAT_TAG, _construct_decimal)
_RulesLoader.add_constructor(_INT_TAG, _construct_int)


def read_rules_text(path: str | Path) -> str:
    """Return the text of a rules file, decoded as UTF-8.

    Messages show ``path`` as given: a str is kept as typed (Path would turn
    "./rules.yaml" into "rules.yaml", and "a/b.yaml" into "a\\b.yaml" on
    Windows).
    """
    shown = str(path)
    path = Path(path)
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


def load_rules(path: str | Path) -> RulesFile:
    """Read, parse and validate a rules file. Raises RulesError listing every problem."""
    shown = str(path)
    data = parse_rules_yaml(read_rules_text(path), shown)
    problems = find_structure_problems(data)
    if problems:
        raise RulesError(shown, problems)
    try:
        return RulesFile.model_validate(data)
    except ValidationError as exc:
        raise RulesError(shown, describe_validation_errors(exc)) from None


def find_structure_problems(data: object) -> list[str]:
    """Find ambiguous unquoted values and lists or mappings that contain themselves.

    Walks the data once, iteratively (no recursion), skipping containers it
    has already seen so shared aliases cannot make the walk explode.
    """
    problems: list[str] = []
    on_path: set[int] = set()
    finished: set[int] = set()
    stack: list[tuple[bool, object, tuple[int | str, ...]]] = [(False, data, ())]
    while stack:
        leaving, value, path = stack.pop()
        if isinstance(value, AmbiguousScalar):
            problems.append(_located(data, path, value.describe()))
            continue
        if not isinstance(value, list | dict):
            continue
        ident = id(value)
        if leaving:
            on_path.discard(ident)
            finished.add(ident)
            continue
        if ident in on_path:
            message = "an alias refers to the list or mapping that contains it"
            problems.append(_located(data, path, message))
            continue
        if ident in finished:
            continue
        on_path.add(ident)
        stack.append((True, value, path))
        children = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in reversed(list(children)):
            if isinstance(key, AmbiguousScalar):
                stack.append((False, child, (*path, key.text)))
                stack.append((False, key, (*path, key.text)))
            else:
                stack.append(
                    (False, child, (*path, key if isinstance(key, int | str) else str(key)))
                )
    return problems


def _located(data: object, path: tuple[int | str, ...], message: str) -> str:
    where = _raw_location(data, path)
    return f"{where}: {message}" if where else message


def _raw_location(data: object, path: tuple[int | str, ...]) -> str:
    """Describe a path in the raw YAML data the way validation errors do."""
    loc = path
    if len(path) >= 2 and path[0] == "checks" and isinstance(path[1], int):
        checks = data.get("checks") if isinstance(data, dict) else None
        check = checks[path[1]] if isinstance(checks, list) else None
        rule = check.get("rule") if isinstance(check, dict) else None
        if isinstance(rule, str) and rule in RULE_MODELS:
            loc = (*path[:2], rule, *path[2:])
    return _locate(loc)[0]


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
    quoted = [display.shown(value) for value in values] or ["an allowed value"]
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
    return f"unknown rule {display.shown(rule)}{hint}; known rules: {known}"
