"""DBML output (https://dbml.dbdiagram.io), for teams that also use dbdiagram.io."""

from __future__ import annotations

import re

from diagram_regenerator.model import Column, Schema, Table

_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _name(text: str) -> str:
    return text if _BARE.match(text) else '"' + text.replace('"', '\\"') + '"'


def _table_ref(key: str) -> str:
    return ".".join(_name(part) for part in key.split(".", 1))


def _type(column_type: str) -> str:
    return column_type if re.fullmatch(r"[A-Za-z0-9_()\[\],]+", column_type) else f'"{column_type}"'


def _string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _default(value: str) -> str:
    if _NUMBER.match(value) or value in {"true", "false", "null"}:
        return value
    if value.startswith("'") and value.endswith("'"):
        return _string(value[1:-1].replace("''", "'"))
    return f"`{value}`"


def _settings(table: Table, column: Column) -> str:
    settings = []
    if column.name in table.primary_key and len(table.primary_key) == 1:
        settings.append("pk")
    if column.autoincrement:
        settings.append("increment")
    if not column.nullable and column.name not in table.primary_key:
        settings.append("not null")
    if column.name not in table.primary_key and any(
        index.unique and index.columns == [column.name] for index in table.indexes
    ):
        settings.append("unique")
    if column.default is not None:
        settings.append(f"default: {_default(column.default)}")
    if column.comment:
        settings.append(f"note: {_string(column.comment)}")
    return f" [{', '.join(settings)}]" if settings else ""


def render_dbml(schema: Schema) -> str:
    out: list[str] = []
    for name in sorted(schema.enums):
        out.append(f"Enum {_table_ref(name)} {{")
        out += [f"  {_name(value)}" for value in schema.enums[name]]
        out += ["}", ""]

    refs: list[str] = []
    for table in schema.sorted_tables():
        header = f"Table {_table_ref(table.key)}"
        if table.comment:
            header += f" [note: {_string(table.comment)}]"
        out.append(header + " {")
        for column in table.columns:
            out.append(f"  {_name(column.name)} {_type(column.type)}{_settings(table, column)}")

        composite = len(table.primary_key) > 1
        extra = [ix for ix in table.indexes if not (ix.unique and len(ix.columns) == 1)]
        if composite or extra:
            out.append("")
            out.append("  indexes {")
            if composite:
                out.append(f"    ({', '.join(_name(c) for c in table.primary_key)}) [pk]")
            for index in extra:
                cols = ", ".join(_name(c) if _BARE.match(c) else f"`{c}`" for c in index.columns)
                options = (["unique"] if index.unique else []) + (
                    [f"name: {_string(index.name)}"] if index.name else []
                )
                suffix = f" [{', '.join(options)}]" if options else ""
                out.append(f"    ({cols}){suffix}")
            out.append("  }")
        out += ["}", ""]

        for fk in table.foreign_keys:
            if len(fk.columns) == 1:
                left = f"{_table_ref(table.key)}.{_name(fk.columns[0])}"
                right = f"{_table_ref(fk.ref_table)}.{_name(fk.ref_columns[0] if fk.ref_columns else 'id')}"
            else:
                left = f"{_table_ref(table.key)}.({', '.join(_name(c) for c in fk.columns)})"
                right = (
                    f"{_table_ref(fk.ref_table)}.({', '.join(_name(c) for c in fk.ref_columns)})"
                )
            relation = "-" if table.is_unique(fk.columns) else ">"
            actions = []
            if fk.on_delete:
                actions.append(f"delete: {fk.on_delete.lower()}")
            if fk.on_update:
                actions.append(f"update: {fk.on_update.lower()}")
            suffix = f" [{', '.join(actions)}]" if actions else ""
            refs.append(f"Ref: {left} {relation} {right}{suffix}")

    out += refs
    return "\n".join(out).rstrip() + "\n"
