"""Mermaid ``erDiagram`` output.

GitHub, GitLab, Notion and most docs sites render Mermaid natively, so a
diagram in a Markdown file or PR comment needs no image hosting. Mermaid's
grammar is strict about identifiers, so names and types are sanitised here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from diagram_regenerator.model import DRAFT_PREFIX, Column, ForeignKey, Schema, Table

COLUMN_MODES = ("all", "keys", "none")


@dataclass
class MermaidOptions:
    columns: str = "all"  # all | keys | none
    comments: bool = True
    max_comment: int = 60
    layout: str | None = None  # "elk" for Mermaid's ELK layout engine

    def __post_init__(self) -> None:
        if self.columns not in COLUMN_MODES:
            raise ValueError(f"columns must be one of {', '.join(COLUMN_MODES)}")


_NOT_WORD = re.compile(r"[^A-Za-z0-9_\-]")


def entity_id(key: str) -> str:
    """Mermaid entity name for a table key (``billing.invoices`` -> ``billing__invoices``)."""
    text = _NOT_WORD.sub("_", key.replace(".", "__"))
    return text if text and (text[0].isalpha() or text[0] == "_") else f"_{text}"


def attribute_type(column_type: str) -> str:
    """Mermaid attribute types allow letters, digits, ``_-()[]`` only."""
    text = re.sub(r"\(([^)]*)\)", lambda m: _type_args(m.group(1)), column_type)
    text = text.replace(" ", "_")
    text = re.sub(r"[^A-Za-z0-9_\-()\[\]]", "", text)
    return text if text and (text[0].isalpha() or text[0] == "_") else f"t_{text}"


def _type_args(args: str) -> str:
    if re.fullmatch(r"[\d\s,]*", args):
        return "(" + "_".join(part.strip() for part in args.split(",")) + ")"
    return ""  # enum('a','b') and friends: the values live in the data dictionary


def attribute_name(name: str) -> str:
    text = _NOT_WORD.sub("_", name)
    return text if text and (text[0].isalpha() or text[0] == "_") else f"_{text}"


def comment_text(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    if text.lower().startswith(DRAFT_PREFIX):
        text = text[len(DRAFT_PREFIX) :]
    text = " ".join(text.replace('"', "'").split())
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def key_markers(table: Table, column: Column) -> list[str]:
    markers = []
    if column.name in table.primary_key:
        markers.append("PK")
    if table.foreign_key_for(column.name):
        markers.append("FK")
    if column.name not in table.primary_key and table.is_unique([column.name]):
        markers.append("UK")
    return markers


def attribute_line(table: Table, column: Column, comment: str | None = None) -> str:
    parts = [attribute_type(column.type), attribute_name(column.name)]
    markers = key_markers(table, column)
    if markers:
        parts.append(", ".join(markers))
    if comment:
        parts.append(f'"{comment}"')
    return " ".join(parts)


def relationship_line(
    schema: Schema, table: Table, fk: ForeignKey, label: str | None = None
) -> str:
    """``parent ||--o{ child`` for a foreign key from ``table`` to its parent."""
    nullable = any((column := table.column(name)) is None or column.nullable for name in fk.columns)
    parent_side = "|o" if nullable else "||"
    child_side = "o|" if table.is_unique(fk.columns) else "o{"
    identifying = bool(table.primary_key) and set(fk.columns) <= set(table.primary_key)
    line = "--" if identifying else ".."
    text = label if label is not None else ", ".join(fk.columns)
    text = text.replace('"', "'")
    return f'{entity_id(fk.ref_table)} {parent_side}{line}{child_side} {entity_id(table.key)} : "{text}"'


def visible_columns(table: Table, mode: str) -> list[Column]:
    if mode == "none":
        return []
    if mode == "keys":
        return [column for column in table.columns if key_markers(table, column)]
    return list(table.columns)


def render_mermaid(
    schema: Schema,
    options: MermaidOptions | None = None,
    *,
    tables: Iterable[str] | None = None,
    stubs: Iterable[str] = (),
) -> str:
    """Render ``schema`` (or just ``tables`` from it) as a Mermaid ER diagram.

    ``stubs`` are drawn as bare boxes: useful to show the neighbours of a
    focused group of tables without repeating their columns.
    """
    options = options or MermaidOptions()
    keys = sorted(tables) if tables is not None else sorted(schema.tables)
    stub_keys = sorted(set(stubs) - set(keys))
    shown = set(keys) | set(stub_keys)

    lines: list[str] = []
    if options.layout:
        lines += ["---", "config:", f"  layout: {options.layout}", "---"]
    lines.append("erDiagram")

    for key in keys:
        table = schema.tables[key]
        columns = visible_columns(table, options.columns)
        if not columns:
            lines.append(f"    {entity_id(key)}")
            continue
        lines.append(f"    {entity_id(key)} {{")
        for column in columns:
            comment = (
                comment_text(column.comment, options.max_comment) if options.comments else None
            )
            lines.append(f"        {attribute_line(table, column, comment)}")
        lines.append("    }")
    for key in stub_keys:
        lines.append(f"    {entity_id(key)}")

    for key in keys:
        table = schema.tables[key]
        for fk in table.foreign_keys:
            if fk.ref_table in shown:
                lines.append(f"    {relationship_line(schema, table, fk)}")
    for key in stub_keys:
        table = schema.tables.get(key)
        for fk in table.foreign_keys if table else []:
            if fk.ref_table in keys:
                lines.append(f"    {relationship_line(schema, table, fk)}")
    return "\n".join(lines) + "\n"


def neighbours(schema: Schema, keys: Iterable[str]) -> set[str]:
    """Tables directly linked (either direction) to ``keys`` but not in it."""
    keys = set(keys)
    found: set[str] = set()
    for key in keys:
        table = schema.tables.get(key)
        if table is None:
            continue
        found.update(fk.ref_table for fk in table.foreign_keys)
        found.update(other.key for other, _ in schema.referencing(key))
    return {key for key in found if key in schema.tables} - keys
