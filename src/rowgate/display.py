"""How RowGate shows paths, column names and values in its output.

Python's repr doubles every backslash, so a Windows path such as
C:\\data\\rules.yaml would be printed with two backslashes between each
part. RowGate shows text as written instead, and escapes only characters
that are not printable: control characters (line breaks, tabs, terminal
escape codes), invisible formatting characters and separators other than
the space. A line break in a value is shown as \\n, so it can never break
or colour the output.
"""

from __future__ import annotations


def escaped(text: str) -> str:
    """``text`` with each non-printable character written as an escape such as \\n or \\x1b."""
    if text.isprintable():
        return text
    # repr of one character gives Python's escape for it: \n, \t, \x1b, \u200b.
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def quoted(text: str) -> str:
    """``text`` escaped and in quotes: single quotes, or double quotes if it has a single one.

    Quotes inside the text are not escaped, and backslashes are kept as
    written: C:\\data is shown as 'C:\\data'.
    """
    quote = '"' if "'" in text and '"' not in text else "'"
    return f"{quote}{escaped(text)}{quote}"


def shown(value: object) -> str:
    """A value from a rules file or the data: text quoted as above, anything else as repr."""
    if isinstance(value, str):
        return quoted(value)
    return repr(value)
