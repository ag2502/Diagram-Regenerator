"""Human-maintained table and column descriptions (``descriptions.yml``).

The file is the place to document a schema without touching migrations::

    tables:
      customers:
        description: People who buy things.
        columns:
          email: Login address; unique per customer.
          deleted_at: "[draft] Soft-delete timestamp; NULL while active."

Precedence when rendering docs: this file, then database comments. Entries
starting with ``[draft]`` were suggested by ``diagram-regen describe`` and
are shown as drafts until someone removes the prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from diagram_regenerator.model import DRAFT_PREFIX, Schema

HEADER = """\
# Table and column descriptions used in the generated schema docs.
# Edit freely: `diagram-regen describe` only adds missing entries and never
# overwrites yours. Entries starting with "[draft]" were suggested
# automatically; delete the prefix once a person has checked them.
# (Comments other than this header are not preserved when the file is updated.)
"""


@dataclass
class TableDocs:
    description: str | None = None
    columns: dict[str, str] = field(default_factory=dict)


@dataclass
class Descriptions:
    tables: dict[str, TableDocs] = field(default_factory=dict)

    # ------------------------------------------------------------------ IO

    @classmethod
    def load(cls, path: str | Path | None) -> Descriptions:
        if path is None or not Path(path).is_file():
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping with a `tables` key")
        docs = cls()
        for key, entry in (data.get("tables") or {}).items():
            entry = entry or {}
            if isinstance(entry, str):
                docs.tables[str(key)] = TableDocs(description=entry)
                continue
            columns = {str(k): str(v) for k, v in (entry.get("columns") or {}).items() if v}
            docs.tables[str(key)] = TableDocs(entry.get("description") or None, columns)
        return docs

    def to_yaml(self) -> str:
        tables = {}
        for key, entry in self.tables.items():
            item: dict = {}
            if entry.description:
                item["description"] = entry.description
            if entry.columns:
                item["columns"] = dict(entry.columns)
            if item:
                tables[key] = item
        body = yaml.safe_dump({"tables": tables}, sort_keys=False, allow_unicode=True, width=100)
        return HEADER + body

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_yaml(), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ lookups

    def table(self, key: str) -> str | None:
        entry = self.tables.get(key)
        return entry.description if entry else None

    def column(self, key: str, name: str) -> str | None:
        entry = self.tables.get(key)
        return entry.columns.get(name) if entry else None

    def set_table(self, key: str, text: str, overwrite: bool = False) -> bool:
        entry = self.tables.setdefault(key, TableDocs())
        if entry.description and not overwrite:
            return False
        entry.description = text
        return True

    def set_column(self, key: str, name: str, text: str, overwrite: bool = False) -> bool:
        entry = self.tables.setdefault(key, TableDocs())
        if entry.columns.get(name) and not overwrite:
            return False
        entry.columns[name] = text
        return True

    # ------------------------------------------------------------------ schema

    def apply(self, schema: Schema) -> Schema:
        """A copy of ``schema`` whose comments carry these descriptions."""
        documented = Schema.from_dict(schema.to_dict())
        for key, table in documented.tables.items():
            table.comment = self.table(key) or table.comment
            for column in table.columns:
                column.comment = self.column(key, column.name) or column.comment
        return documented

    def missing(self, schema: Schema) -> list[tuple[str, str | None]]:
        """``(table, column)`` pairs (column ``None`` = the table) with no description anywhere."""
        gaps: list[tuple[str, str | None]] = []
        for table in schema.sorted_tables():
            if not (self.table(table.key) or table.comment):
                gaps.append((table.key, None))
            for column in table.columns:
                if not (self.column(table.key, column.name) or column.comment):
                    gaps.append((table.key, column.name))
        return gaps

    def stale(self, schema: Schema) -> list[str]:
        """Entries that describe tables or columns the schema no longer has."""
        found = []
        for key, entry in self.tables.items():
            table = schema.tables.get(key)
            if table is None:
                found.append(key)
                continue
            found += [f"{key}.{name}" for name in entry.columns if table.column(name) is None]
        return found

    def coverage(self, schema: Schema) -> tuple[int, int, int]:
        """``(reviewed, drafts, total)`` over every table and column."""
        reviewed = drafts = total = 0
        documented = self.apply(schema)
        for table in documented.tables.values():
            for text in [table.comment, *(c.comment for c in table.columns)]:
                total += 1
                if text and text.lower().startswith(DRAFT_PREFIX):
                    drafts += 1
                elif text:
                    reviewed += 1
        return reviewed, drafts, total
