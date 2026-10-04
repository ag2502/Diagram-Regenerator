"""Canonical spellings for column types and defaults.

The same column looks different depending on where it was read from: a
migration says ``SERIAL``, PostgreSQL reflects ``INTEGER`` with a
``nextval(...)`` default; MySQL reflects ``BOOLEAN`` as ``TINYINT(1)``.
Normalising both sides keeps diffs about real changes, not spelling.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Whole-type aliases, matched after lower-casing and collapsing whitespace.
_TYPE_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "signed": "integer",
    "mediumint": "integer",
    "int2": "smallint",
    "int8": "bigint",
    "bool": "boolean",
    "tinyint(1)": "boolean",
    "character varying": "varchar",
    "nvarchar": "varchar",
    "varchar2": "varchar",
    "string": "varchar",
    "character": "char",
    "bpchar": "char",
    "nchar": "char",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "double precision": "double",
    "float8": "double",
    "float4": "real",
    "decimal": "numeric",
    "dec": "numeric",
    "datetime2": "datetime",
    "bit varying": "varbit",
}

# Parameterised base names that should be renamed but keep their arguments.
_BASE_ALIASES = {
    "character varying": "varchar",
    "nvarchar": "varchar",
    "varchar2": "varchar",
    "character": "char",
    "bpchar": "char",
    "nchar": "char",
    "decimal": "numeric",
    "dec": "numeric",
    "datetime2": "datetime",
    "bit varying": "varbit",
}

# Integer display widths (MySQL ``INT(11)``) carry no meaning; drop them.
_DISPLAY_WIDTH_TYPES = {"integer", "int", "smallint", "bigint", "mediumint", "tinyint"}

_SERIAL_TYPES = {
    "serial": "integer",
    "serial4": "integer",
    "smallserial": "smallint",
    "serial2": "smallint",
    "bigserial": "bigint",
    "serial8": "bigint",
}

_TIMEZONE_SUFFIX = re.compile(r"^(timestamp|time)(\(\d+\))? with(out)? time zone$")
_TRAILING_CLAUSE = re.compile(r"\s+(collate|character set|charset)\s+.*$")


def split_serial(raw: str) -> tuple[str, bool]:
    """Return ``(type, is_serial)`` so ``SERIAL`` becomes an auto-incrementing integer."""
    key = " ".join(raw.lower().split())
    if key in _SERIAL_TYPES:
        return _SERIAL_TYPES[key], True
    return raw, False


def normalize_type(raw: str | None) -> str:
    """Canonical, lower-case spelling of a column type."""
    if not raw:
        return "unknown"
    text = " ".join(str(raw).lower().replace('"', "").replace("`", "").split())
    text = _TRAILING_CLAUSE.sub("", text)
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*\)", ")", text)
    text = re.sub(r"\s*,\s*", ",", text)

    array_suffix = ""
    while text.endswith("[]"):
        array_suffix += "[]"
        text = text[:-2].rstrip()
    if text.startswith("array<") and text.endswith(">"):
        inner = normalize_type(text[6:-1])
        return inner + "[]" + array_suffix

    match = _TIMEZONE_SUFFIX.match(text)
    if match:
        base = "timestamp" if match.group(1) == "timestamp" else "time"
        precision = match.group(2) or ""
        tz = "" if match.group(3) else "tz"
        return f"{base}{tz}{precision}" + array_suffix

    if text in _TYPE_ALIASES:
        return _TYPE_ALIASES[text] + array_suffix

    base, paren, rest = text.partition("(")
    base = base.strip()
    base = _BASE_ALIASES.get(base, _TYPE_ALIASES.get(base, base))
    if paren:
        args = rest.rsplit(")", 1)[0]
        tail = rest.rsplit(")", 1)[1].strip() if ")" in rest else ""
        if base in _DISPLAY_WIDTH_TYPES and args.isdigit():
            base = _TYPE_ALIASES.get(base, base)
            return (f"{base} {tail}".strip()) + array_suffix
        return (f"{base}({args})" + (f" {tail}" if tail else "")) + array_suffix
    return base + array_suffix


# --------------------------------------------------------------------------- defaults

_CAST = re.compile(r"::\s*[a-z_][a-z0-9_ ]*(\(\s*\d+(\s*,\s*\d+)?\s*\))?(\[\])*", re.IGNORECASE)
# A number as written by a database: no leading zeros ("007" stays a string).
_NUMBER = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")
_NOW_SPELLINGS = {
    "now()",
    "current_timestamp()",
    "current_timestamp",
    "localtimestamp",
    "localtimestamp()",
    "transaction_timestamp()",
    "getdate()",
    "sysdatetime()",
    "datetime('now')",
    "current_timestamp(6)",
    "current_timestamp(3)",
    "now(6)",
    "now(3)",
}
_NEXTVAL = re.compile(r"^nextval\(", re.IGNORECASE)


def _outside_quotes(text: str, transform) -> str:
    """Apply ``transform`` to the parts of ``text`` that are not single-quoted literals."""
    parts = re.split(r"('(?:[^']|'')*')", text)
    return "".join(part if part.startswith("'") else transform(part) for part in parts)


def _strip_wrapping_parens(text: str) -> str:
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for index, char in enumerate(text):
            depth += char == "("
            depth -= char == ")"
            if depth == 0 and index < len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def is_sequence_default(raw: str | None) -> bool:
    """True when a default just draws from a sequence (``SERIAL`` / identity)."""
    return bool(raw) and bool(_NEXTVAL.match(str(raw).strip()))


def normalize_default(raw: object) -> str | None:
    """Canonical spelling of a column default, or ``None`` when there is none."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = _strip_wrapping_parens(text)
    if text.upper() == "NULL" or text.upper().startswith("NULL::"):
        return None
    text = _outside_quotes(text, lambda part: _CAST.sub("", part))
    text = _outside_quotes(text, lambda part: " ".join(part.lower().split()))
    text = _strip_wrapping_parens(text.strip())
    if text in _NOW_SPELLINGS:
        return "current_timestamp"
    if text.startswith("'") and text.endswith("'") and _NUMBER.match(text[1:-1]):
        text = text[1:-1]
    if _NUMBER.match(text):
        return _canonical_number(text)
    return text


def _canonical_number(text: str) -> str:
    """``0.00`` -> ``0``, ``1.50`` -> ``1.5`` (MySQL pads DECIMAL defaults)."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        return text
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")
