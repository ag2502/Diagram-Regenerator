"""The schema model every source produces and every renderer consumes.

A :class:`Schema` serialises to a deterministic JSON *snapshot*: tables are
sorted, there are no timestamps, and constraint lists have a stable order, so
a committed snapshot only changes when the schema does.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT_FORMAT = "diagram-regenerator/schema@1"

# Descriptions suggested by a tool (not yet reviewed by a person) start with this.
DRAFT_PREFIX = "[draft]"

# Tables that migration tools create for their own bookkeeping. They exist in a
# live database but never in the migration files, so they would show up as
# drift on every comparison unless they are skipped.
BOOKKEEPING_TABLES = (
    "alembic_version",
    "schema_migrations",
    "ar_internal_metadata",
    "_prisma_migrations",
    "flyway_schema_history",
    "schema_version",
    "__diesel_schema_migrations",
    "_sqlx_migrations",
    "goose_db_version",
    "gorp_migrations",
    "knex_migrations",
    "knex_migrations_lock",
    "django_migrations",
    "SequelizeMeta",
    "databasechangelog",
    "databasechangeloglock",
    "sqlite_sequence",
    "spatial_ref_sys",
    "dbmate_migrations",
)


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    autoincrement: bool = False
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "type": self.type, "nullable": self.nullable}
        if self.default is not None:
            data["default"] = self.default
        if self.autoincrement:
            data["autoincrement"] = True
        if self.comment:
            data["comment"] = self.comment
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Column:
        return cls(
            name=data["name"],
            type=data.get("type", "unknown"),
            nullable=data.get("nullable", True),
            default=data.get("default"),
            autoincrement=data.get("autoincrement", False),
            comment=data.get("comment"),
        )


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    name: str | None = None
    on_delete: str | None = None
    on_update: str | None = None

    @property
    def signature(self) -> tuple:
        """Identity used for comparisons: generated constraint names differ between sources."""
        return (tuple(self.columns), self.ref_table, tuple(self.ref_columns))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "columns": list(self.columns),
            "ref_table": self.ref_table,
            "ref_columns": list(self.ref_columns),
        }
        if self.name:
            data["name"] = self.name
        if self.on_delete:
            data["on_delete"] = self.on_delete
        if self.on_update:
            data["on_update"] = self.on_update
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ForeignKey:
        return cls(
            columns=list(data["columns"]),
            ref_table=data["ref_table"],
            ref_columns=list(data.get("ref_columns", [])),
            name=data.get("name"),
            on_delete=data.get("on_delete"),
            on_update=data.get("on_update"),
        )


@dataclass
class Index:
    columns: list[str]
    unique: bool = False
    name: str | None = None

    @property
    def signature(self) -> tuple:
        return (tuple(self.columns), self.unique)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"columns": list(self.columns), "unique": self.unique}
        if self.name:
            data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Index:
        return cls(
            columns=list(data["columns"]),
            unique=data.get("unique", False),
            name=data.get("name"),
        )


@dataclass
class Table:
    name: str
    schema: str | None = None
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    comment: str | None = None

    @property
    def key(self) -> str:
        return qualified_name(self.schema, self.name)

    def column(self, name: str) -> Column | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    def foreign_key_for(self, column: str) -> ForeignKey | None:
        for fk in self.foreign_keys:
            if column in fk.columns:
                return fk
        return None

    def is_unique(self, columns: Iterable[str]) -> bool:
        """Whether ``columns`` (in any order) are covered by the PK or a unique index."""
        wanted = set(columns)
        if wanted and wanted == set(self.primary_key):
            return True
        return any(index.unique and set(index.columns) == wanted for index in self.indexes)

    def add_index(self, index: Index) -> None:
        """Add ``index`` unless an equivalent one (same columns and uniqueness) exists."""
        if index.unique and set(index.columns) == set(self.primary_key):
            return
        if all(existing.signature != index.signature for existing in self.indexes):
            self.indexes.append(index)

    def canonicalize(self) -> None:
        self.foreign_keys.sort(key=lambda fk: (fk.columns, fk.ref_table, fk.ref_columns))
        self.indexes.sort(key=lambda ix: (not ix.unique, ix.columns, ix.name or ""))

    def to_dict(self) -> dict[str, Any]:
        self.canonicalize()
        data: dict[str, Any] = {"name": self.name}
        if self.schema:
            data["schema"] = self.schema
        if self.comment:
            data["comment"] = self.comment
        data["columns"] = [column.to_dict() for column in self.columns]
        data["primary_key"] = list(self.primary_key)
        data["foreign_keys"] = [fk.to_dict() for fk in self.foreign_keys]
        data["indexes"] = [index.to_dict() for index in self.indexes]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Table:
        return cls(
            name=data["name"],
            schema=data.get("schema"),
            columns=[Column.from_dict(item) for item in data.get("columns", [])],
            primary_key=list(data.get("primary_key", [])),
            foreign_keys=[ForeignKey.from_dict(item) for item in data.get("foreign_keys", [])],
            indexes=[Index.from_dict(item) for item in data.get("indexes", [])],
            comment=data.get("comment"),
        )


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)
    dialect: str | None = None

    # ------------------------------------------------------------------ building

    def add_table(self, table: Table) -> Table:
        self.tables[table.key] = table
        return table

    def get(self, key: str) -> Table | None:
        return self.tables.get(key)

    def resolve(self, name: str) -> Table | None:
        """Find a table by qualified key, or by bare name when that is unambiguous."""
        if name in self.tables:
            return self.tables[name]
        matches = [table for table in self.tables.values() if table.name == name]
        return matches[0] if len(matches) == 1 else None

    def sorted_tables(self) -> list[Table]:
        return [self.tables[key] for key in sorted(self.tables)]

    def referencing(self, key: str) -> list[tuple[Table, ForeignKey]]:
        """Foreign keys in other tables that point at table ``key``."""
        return [
            (table, fk)
            for table in self.sorted_tables()
            for fk in table.foreign_keys
            if fk.ref_table == key
        ]

    @property
    def column_count(self) -> int:
        return sum(len(table.columns) for table in self.tables.values())

    # ------------------------------------------------------------------ filtering

    def filtered(
        self,
        include: Iterable[str] = (),
        exclude: Iterable[str] = (),
        skip_bookkeeping: bool = True,
    ) -> Schema:
        """A copy keeping tables matching ``include`` globs and not matching ``exclude`` ones.

        Patterns match either the qualified key (``billing.invoices``) or the bare
        table name, so ``audit_*`` works across schemas.
        """
        include = list(include)
        exclude = list(exclude) + (list(BOOKKEEPING_TABLES) if skip_bookkeeping else [])

        def matches(table: Table, patterns: list[str]) -> bool:
            return any(
                fnmatch.fnmatchcase(table.key, pattern) or fnmatch.fnmatchcase(table.name, pattern)
                for pattern in patterns
            )

        kept = {
            key: table
            for key, table in self.tables.items()
            if (not include or matches(table, include)) and not matches(table, exclude)
        }
        return Schema(tables=kept, enums=dict(self.enums), dialect=self.dialect)

    # ------------------------------------------------------------------ serialisation

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"format": SNAPSHOT_FORMAT}
        if self.dialect:
            data["dialect"] = self.dialect
        data["tables"] = {key: self.tables[key].to_dict() for key in sorted(self.tables)}
        if self.enums:
            data["enums"] = {name: list(self.enums[name]) for name in sorted(self.enums)}
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schema:
        fmt = data.get("format", SNAPSHOT_FORMAT)
        if not str(fmt).startswith("diagram-regenerator/schema@"):
            raise ValueError(f"not a diagram-regenerator snapshot (format={fmt!r})")
        schema = cls(dialect=data.get("dialect"), enums=dict(data.get("enums", {})))
        for item in data.get("tables", {}).values():
            schema.add_table(Table.from_dict(item))
        return schema

    @classmethod
    def from_json(cls, text: str) -> Schema:
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> Schema:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def fingerprint(self) -> str:
        """Short content hash: equal schemas have equal fingerprints."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:12]


def qualified_name(schema: str | None, name: str) -> str:
    return f"{schema}.{name}" if schema else name
