"""Compare two schemas and rate each change by how risky it is to deploy.

Severities, most to least serious:

``breaking``  existing queries, writes or data can fail or be lost
``warning``   can fail on existing data or change behaviour (new FK, new unique index)
``safe``      additive or widening; existing code keeps working
``info``      documentation only (comments, defaults)

Constraint and index *names* are ignored: generated names differ between
environments and tools, and a renamed constraint is not a schema change
anyone needs to review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from diagram_regenerator.model import Column, ForeignKey, Index, Schema, Table

SEVERITIES = ("breaking", "warning", "safe", "info")
IGNORABLE = ("comments", "defaults", "indexes", "foreign_keys", "enums", "column_order")
_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


@dataclass
class Change:
    kind: str
    table: str | None
    severity: str
    summary: str
    column: str | None = None
    before: Any = None
    after: Any = None
    note: str | None = None

    @property
    def target(self) -> str:
        if self.table and self.column:
            return f"{self.table}.{self.column}"
        return self.table or ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "severity": self.severity,
            "table": self.table,
            "column": self.column,
            "summary": self.summary,
        }
        if self.before is not None:
            data["before"] = self.before
        if self.after is not None:
            data["after"] = self.after
        if self.note:
            data["note"] = self.note
        return data


@dataclass
class SchemaDiff:
    old: Schema
    new: Schema
    changes: list[Change] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def by_severity(self, severity: str) -> list[Change]:
        return [c for c in self.changes if c.severity == severity]

    @property
    def breaking(self) -> list[Change]:
        return self.by_severity("breaking")

    def counts(self) -> dict[str, int]:
        return {s: len(self.by_severity(s)) for s in SEVERITIES}

    def worst(self) -> str | None:
        return min((c.severity for c in self.changes), key=_RANK.__getitem__, default=None)

    def at_least(self, severity: str) -> bool:
        """Any change as serious as ``severity`` or worse?"""
        return any(_RANK[c.severity] <= _RANK[severity] for c in self.changes)

    def changed_tables(self) -> list[str]:
        return sorted({c.table for c in self.changes if c.table})

    def summary(self) -> str:
        if not self.changes:
            return "No schema changes."
        tables = len(self.changed_tables())
        parts = [f"{len(self.changes)} change{'s' * (len(self.changes) != 1)}"]
        parts.append(f"across {tables} table{'s' * (tables != 1)}")
        counts = self.counts()
        rated = [f"{counts[s]} {s}" for s in SEVERITIES if counts[s]]
        return " ".join(parts) + f" ({', '.join(rated)})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "counts": self.counts(),
            "old_fingerprint": self.old.fingerprint(),
            "new_fingerprint": self.new.fingerprint(),
            "changes": [c.to_dict() for c in self.changes],
        }


# --------------------------------------------------------------------------- entry point


def diff_schemas(old: Schema, new: Schema, ignore: Iterable[str] = ()) -> SchemaDiff:
    ignore = set(ignore)
    unknown = ignore - set(IGNORABLE)
    if unknown:
        raise ValueError(f"can't ignore {sorted(unknown)}; choose from {', '.join(IGNORABLE)}")
    result = SchemaDiff(old=old, new=new)
    changes = result.changes

    removed = sorted(set(old.tables) - set(new.tables))
    added = sorted(set(new.tables) - set(old.tables))
    renamed = _match_renamed_tables(old, new, removed, added)
    for before, after in renamed:
        removed.remove(before)
        added.remove(after)
        changes.append(
            Change(
                "table_renamed",
                after,
                "breaking",
                f"Table `{before}` renamed to `{after}`",
                before=before,
                after=after,
                note="looks like a rename (same columns); queries using the old name fail",
            )
        )
    for key in removed:
        changes.append(
            Change(
                "table_removed",
                key,
                "breaking",
                f"Table `{key}` removed",
                note=f"{len(old.tables[key].columns)} columns and their data are dropped",
            )
        )
    for key in added:
        table = new.tables[key]
        changes.append(
            Change(
                "table_added",
                key,
                "safe",
                f"Table `{key}` added",
                note=f"{len(table.columns)} columns",
            )
        )
    for before, after in renamed:
        changes += _diff_table(old.tables[before], new.tables[after], ignore)
    for key in sorted(set(old.tables) & set(new.tables)):
        changes += _diff_table(old.tables[key], new.tables[key], ignore)
    if "enums" not in ignore:
        changes += _diff_enums(old.enums, new.enums)
    changes.sort(
        key=lambda c: (c.table is None, c.table or "", _RANK[c.severity], c.column or "", c.kind)
    )
    return result


def _match_renamed_tables(
    old: Schema, new: Schema, removed: list[str], added: list[str]
) -> list[tuple[str, str]]:
    def shape(table: Table) -> tuple:
        return tuple((c.name, c.type) for c in table.columns)

    pairs = []
    for before in removed:
        candidates = [a for a in added if shape(new.tables[a]) == shape(old.tables[before])]
        if len(candidates) == 1 and shape(old.tables[before]):
            twins = [r for r in removed if shape(old.tables[r]) == shape(new.tables[candidates[0]])]
            if len(twins) == 1:
                pairs.append((before, candidates[0]))
    return pairs


# --------------------------------------------------------------------------- tables


def _diff_table(old: Table, new: Table, ignore: set[str]) -> list[Change]:
    key = new.key
    changes: list[Change] = []
    old_cols = {c.name: c for c in old.columns}
    new_cols = {c.name: c for c in new.columns}
    removed = [n for n in old_cols if n not in new_cols]
    added = [n for n in new_cols if n not in old_cols]

    for before, after in _match_renamed_columns(old_cols, new_cols, removed, added):
        removed.remove(before)
        added.remove(after)
        changes.append(
            Change(
                "column_renamed",
                key,
                "breaking",
                f"Column `{key}.{before}` renamed to `{after}`",
                column=after,
                before=before,
                after=after,
                note="looks like a rename (identical definition); queries using the old name fail",
            )
        )
        changes += _diff_column(key, old_cols[before], new_cols[after], ignore)

    for name in removed:
        column = old_cols[name]
        changes.append(
            Change(
                "column_removed",
                key,
                "breaking",
                f"Column `{key}.{name}` removed",
                column=name,
                before=_column_brief(column),
                note="its data is dropped and queries using it fail",
            )
        )
    for name in added:
        column = new_cols[name]
        required = not column.nullable and column.default is None and not column.autoincrement
        changes.append(
            Change(
                "column_added",
                key,
                "breaking" if required else "safe",
                f"Column `{key}.{name}` added",
                column=name,
                after=_column_brief(column),
                note=(
                    "NOT NULL without a default: inserts that omit it fail, and adding it "
                    "fails on a table that already has rows"
                    if required
                    else None
                ),
            )
        )
    for name in [n for n in new_cols if n in old_cols]:
        changes += _diff_column(key, old_cols[name], new_cols[name], ignore)

    if "column_order" not in ignore:
        common_old = [n for n in old_cols if n in new_cols]
        common_new = [n for n in new_cols if n in old_cols]
        if common_old != common_new:
            changes.append(
                Change("column_order_changed", key, "info", f"Column order of `{key}` changed")
            )

    if old.primary_key != new.primary_key:
        changes.append(
            Change(
                "primary_key_changed",
                key,
                "breaking",
                f"Primary key of `{key}` changed",
                before=old.primary_key or None,
                after=new.primary_key or None,
                note="row identity changes; references and upserts may break",
            )
        )
    if "foreign_keys" not in ignore:
        changes += _diff_foreign_keys(key, old.foreign_keys, new.foreign_keys)
    if "indexes" not in ignore:
        changes += _diff_indexes(key, old.indexes, new.indexes)
    if "comments" not in ignore and (old.comment or None) != (new.comment or None):
        changes.append(
            Change(
                "table_comment_changed",
                key,
                "info",
                f"Description of `{key}` changed",
                before=old.comment,
                after=new.comment,
            )
        )
    return changes


def _match_renamed_columns(old_cols, new_cols, removed, added) -> list[tuple[str, str]]:
    """Pair a dropped and an added column when they are identical apart from the name."""

    def shape(column: Column) -> tuple:
        return (column.type, column.nullable, column.default)

    pairs = []
    for before in removed:
        candidates = [a for a in added if shape(new_cols[a]) == shape(old_cols[before])]
        if len(candidates) != 1:
            continue
        twins = [r for r in removed if shape(old_cols[r]) == shape(new_cols[candidates[0]])]
        if len(twins) == 1:
            pairs.append((before, candidates[0]))
    return pairs


def _column_brief(column: Column) -> str:
    text = column.type + ("" if column.nullable else " not null")
    if column.default is not None:
        text += f" default {column.default}"
    return text


def _diff_column(key: str, old: Column, new: Column, ignore: set[str]) -> list[Change]:
    changes = []
    name = new.name
    if old.type != new.type:
        widening = is_widening(old.type, new.type)
        changes.append(
            Change(
                "column_type_changed",
                key,
                "safe" if widening else "breaking",
                f"Type of `{key}.{name}` changed",
                column=name,
                before=old.type,
                after=new.type,
                note=None if widening else "existing values may not convert, or may be truncated",
            )
        )
    if old.nullable != new.nullable:
        if new.nullable:
            changes.append(
                Change(
                    "column_nullable",
                    key,
                    "warning",
                    f"`{key}.{name}` now allows NULL",
                    column=name,
                    before="not null",
                    after="null",
                    note="readers may now receive NULL",
                )
            )
        else:
            changes.append(
                Change(
                    "column_not_null",
                    key,
                    "breaking",
                    f"`{key}.{name}` is now NOT NULL",
                    column=name,
                    before="null",
                    after="not null",
                    note="fails if existing rows hold NULL; inserts without a value fail"
                    if new.default is None
                    else "fails if existing rows hold NULL",
                )
            )
    if "defaults" not in ignore and old.default != new.default:
        changes.append(
            Change(
                "column_default_changed",
                key,
                "info",
                f"Default of `{key}.{name}` changed",
                column=name,
                before=old.default,
                after=new.default,
            )
        )
    if "comments" not in ignore and (old.comment or None) != (new.comment or None):
        changes.append(
            Change(
                "column_comment_changed",
                key,
                "info",
                f"Description of `{key}.{name}` changed",
                column=name,
                before=old.comment,
                after=new.comment,
            )
        )
    return changes


def fk_text(fk: ForeignKey, actions: bool = True) -> str:
    text = f"({', '.join(fk.columns)}) → {fk.ref_table}({', '.join(fk.ref_columns)})"
    if actions and fk.on_delete:
        text += f" on delete {fk.on_delete.lower()}"
    return text


def _diff_foreign_keys(key: str, old: list[ForeignKey], new: list[ForeignKey]) -> list[Change]:
    changes = []
    old_by = {fk.signature: fk for fk in old}
    new_by = {fk.signature: fk for fk in new}
    for signature in sorted(set(old_by) - set(new_by)):
        fk = old_by[signature]
        changes.append(
            Change(
                "foreign_key_removed",
                key,
                "warning",
                f"Foreign key {fk_text(fk)} removed from `{key}`",
                column=fk.columns[0] if len(fk.columns) == 1 else None,
                before=fk_text(fk),
                note="referential integrity is no longer enforced",
            )
        )
    for signature in sorted(set(new_by) - set(old_by)):
        fk = new_by[signature]
        changes.append(
            Change(
                "foreign_key_added",
                key,
                "warning",
                f"Foreign key {fk_text(fk)} added to `{key}`",
                column=fk.columns[0] if len(fk.columns) == 1 else None,
                after=fk_text(fk),
                note="existing rows must match; writes with unknown references now fail",
            )
        )
    for signature in sorted(set(old_by) & set(new_by)):
        before, after = old_by[signature], new_by[signature]
        moved = [
            (event, old_action, new_action)
            for event, old_action, new_action in (
                ("delete", before.on_delete, after.on_delete),
                ("update", before.on_update, after.on_update),
            )
            if old_action != new_action
        ]
        if moved:
            changes.append(
                Change(
                    "foreign_key_action_changed",
                    key,
                    "warning",
                    f"Foreign key {fk_text(after, actions=False)} now acts differently",
                    column=after.columns[0] if len(after.columns) == 1 else None,
                    before=", ".join(f"on {e} {(a or 'no action').lower()}" for e, a, _ in moved),
                    after=", ".join(f"on {e} {(b or 'no action').lower()}" for e, _, b in moved),
                    note=f"{' and '.join(e + 's' for e, _, _ in moved)} of parent rows now "
                    "behave differently",
                )
            )
    return changes


def _index_text(index: Index) -> str:
    return ("unique " if index.unique else "") + f"index ({', '.join(index.columns)})"


def _diff_indexes(key: str, old: list[Index], new: list[Index]) -> list[Change]:
    changes = []
    old_by = {ix.signature: ix for ix in old}
    new_by = {ix.signature: ix for ix in new}
    for signature in sorted(set(old_by) - set(new_by)):
        index = old_by[signature]
        changes.append(
            Change(
                "index_removed",
                key,
                "warning",
                f"{_index_text(index).capitalize()} removed from `{key}`",
                before=_index_text(index),
                note="duplicates are no longer rejected"
                if index.unique
                else "queries that used it may slow down",
            )
        )
    for signature in sorted(set(new_by) - set(old_by)):
        index = new_by[signature]
        changes.append(
            Change(
                "index_added",
                key,
                "warning" if index.unique else "safe",
                f"{_index_text(index).capitalize()} added to `{key}`",
                after=_index_text(index),
                note="fails to build if duplicates exist; duplicate writes now fail"
                if index.unique
                else None,
            )
        )
    return changes


def _diff_enums(old: dict[str, list[str]], new: dict[str, list[str]]) -> list[Change]:
    changes = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        if before is None:
            changes.append(Change("enum_added", None, "safe", f"Enum `{name}` added", after=after))
        elif after is None:
            changes.append(
                Change("enum_removed", None, "breaking", f"Enum `{name}` removed", before=before)
            )
        else:
            dropped = [v for v in before if v not in after]
            changes.append(
                Change(
                    "enum_changed",
                    None,
                    "breaking" if dropped else "safe",
                    f"Values of enum `{name}` changed",
                    before=before,
                    after=after,
                    note=f"removed values: {', '.join(dropped)}" if dropped else None,
                )
            )
    return changes


# --------------------------------------------------------------------------- types

_INT_RANK = {"smallint": 1, "integer": 2, "bigint": 3}
_FLOAT_RANK = {"real": 1, "double": 2}
_TEXTUAL = re.compile(r"^(varchar|char|text|citext)(\((\d+)\))?$")
_NUMERIC = re.compile(r"^numeric(\((\d+)(,(\d+))?\))?$")
_PRECISION = re.compile(r"^(timestamptz|timestamp|time|timetz)(\((\d+)\))?$")


def is_widening(old: str, new: str) -> bool:
    """True when every value of ``old`` fits in ``new`` without conversion loss."""
    if old in _INT_RANK and new in _INT_RANK:
        return _INT_RANK[new] >= _INT_RANK[old]
    if old in _INT_RANK and new == "numeric":
        return True
    if old in _FLOAT_RANK and new in _FLOAT_RANK:
        return _FLOAT_RANK[new] >= _FLOAT_RANK[old]

    old_text, new_text = _TEXTUAL.match(old), _TEXTUAL.match(new)
    if old_text and new_text:
        if new_text.group(1) in {"text", "citext"} and not new_text.group(3):
            return True
        if new_text.group(1) == "varchar" and not new_text.group(3):
            return True
        if old_text.group(3) and new_text.group(3) and new_text.group(1) == "varchar":
            return int(new_text.group(3)) >= int(old_text.group(3))
        return False

    old_num, new_num = _NUMERIC.match(old), _NUMERIC.match(new)
    if old_num and new_num:
        if not new_num.group(1):
            return True
        if not old_num.group(1):
            return False
        old_p, old_s = int(old_num.group(2)), int(old_num.group(4) or 0)
        new_p, new_s = int(new_num.group(2)), int(new_num.group(4) or 0)
        return new_s >= old_s and (new_p - new_s) >= (old_p - old_s)

    old_ts, new_ts = _PRECISION.match(old), _PRECISION.match(new)
    if old_ts and new_ts and old_ts.group(1) == new_ts.group(1):
        return int(new_ts.group(3) or 6) >= int(old_ts.group(3) or 6)
    return False
