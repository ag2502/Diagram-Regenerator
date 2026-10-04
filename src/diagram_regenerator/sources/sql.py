"""Schemas from SQL files, with no database needed.

A single DDL file (``schema.sql``, ``structure.sql``, a ``pg_dump --schema-only``)
or a directory of migrations is *replayed* statement by statement into an
in-memory :class:`Schema`: ``CREATE TABLE``, ``ALTER TABLE`` (add / drop /
alter / rename column, constraints), ``CREATE INDEX``, ``DROP``, ``COMMENT ON``
and enum types. Statements that don't change table structure (functions,
triggers, grants, data) are skipped.

Migration layouts recognised out of the box:

* Prisma        ``migrations/20240101000000_init/migration.sql``
* Flyway        ``V1__init.sql``, ``V1_1__add.sql`` (``U``ndo files skipped, ``R``epeatables last)
* golang-migrate / sqlx   ``0001_init.up.sql`` (``.down.sql`` skipped)
* diesel        ``2024-01-01-000000_init/up.sql`` (``down.sql`` skipped)
* dbmate        ``-- migrate:up`` / ``-- migrate:down`` sections
* goose / sql-migrate     ``-- +goose Up`` / ``-- +migrate Up`` sections
* per-dialect files (``*.postgres.up.sql``, ``*.mysql.up.sql``) keep only the active dialect
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from diagram_regenerator.dialects import canonical_dialect, default_schema_for, sqlglot_dialect
from diagram_regenerator.model import Column, ForeignKey, Index, Schema, Table, qualified_name
from diagram_regenerator.normalize import (
    is_sequence_default,
    normalize_default,
    normalize_type,
    split_serial,
)

log = logging.getLogger(__name__)

# sqlglot logs every statement it falls back on; we report what matters ourselves.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass
class SqlLoadResult:
    schema: Schema
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statements: int = 0


# =========================================================================== files


def load_sql(path: str | Path, dialect: str | None = None) -> Schema:
    """Replay a ``.sql`` file or a migrations directory; warnings go to the log."""
    result = parse_sql_path(path, dialect)
    for warning in result.warnings:
        log.warning(warning)
    return result.schema


def parse_sql_path(path: str | Path, dialect: str | None = None) -> SqlLoadResult:
    root = Path(path)
    if root.is_file():
        return replay([(root.name, root.read_text(encoding="utf-8", errors="replace"))], dialect)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} does not exist")
    relative = [p.relative_to(root).as_posix() for p in root.rglob("*.sql") if p.is_file()]
    ordered = migration_order(relative, dialect)
    return replay(
        ((name, (root / name).read_text(encoding="utf-8", errors="replace")) for name in ordered),
        dialect,
    )


_DOWN_FILE = re.compile(r"(^|[._-])(down|rollback|undo)\.sql$", re.IGNORECASE)
_FLYWAY_UNDO = re.compile(r"^U\d", re.IGNORECASE)
_FLYWAY_REPEATABLE = re.compile(r"^R__", re.IGNORECASE)
_DIALECT_TAGS = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pg": "postgresql",
    "cockroach": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "oracle": "oracle",
}


def migration_order(paths: Iterable[str], dialect: str | None = None) -> list[str]:
    """Keep the *up* migrations for ``dialect`` and sort them in apply order."""
    wanted = canonical_dialect(dialect) or "postgresql"
    kept = []
    for path in paths:
        parts = PurePosixPath(path).parts
        name = parts[-1]
        if any(part.startswith(".") for part in parts) or _DOWN_FILE.search(name):
            continue
        if _FLYWAY_UNDO.match(name) and "__" in name:
            continue
        tags = {_DIALECT_TAGS[t] for t in name.lower().split(".")[:-1] if t in _DIALECT_TAGS}
        if tags and wanted not in tags:
            continue
        kept.append(path)
    return sorted(kept, key=_natural_key)


def _natural_key(path: str) -> tuple:
    name = PurePosixPath(path).name
    repeatable = bool(_FLYWAY_REPEATABLE.match(name))
    chunks = re.split(r"(\d+)", path.lower())
    return (repeatable, [int(c) if c.isdigit() else c for c in chunks])


# =========================================================================== text


_SECTION_UP = re.compile(r"^\s*--\s*(\+goose\s+up|\+migrate\s+up|migrate:up)\b", re.IGNORECASE)
_SECTION_DOWN = re.compile(
    r"^\s*--\s*(\+goose\s+down|\+migrate\s+down|migrate:down)\b", re.IGNORECASE
)
_BLOCK_BEGIN = re.compile(r"^\s*--\s*\+(goose|migrate)\s+statementbegin\b", re.IGNORECASE)
_BLOCK_END = re.compile(r"^\s*--\s*\+(goose|migrate)\s+statementend\b", re.IGNORECASE)


def up_statements(text: str, dialect: str | None = None) -> list[tuple[int, str]]:
    """``(line, statement)`` pairs from the *up* part of a migration file."""
    lines = text.splitlines()
    has_sections = any(_SECTION_UP.match(line) or _SECTION_DOWN.match(line) for line in lines)
    active = not has_sections
    statements: list[tuple[int, str]] = []
    pending: list[str] = []
    pending_start = 1
    block: list[str] | None = None
    block_start = 0

    def flush(next_line: int) -> None:
        nonlocal pending, pending_start
        if pending:
            for offset, statement in split_statements("\n".join(pending), dialect):
                statements.append((pending_start + offset - 1, statement))
        pending = []
        pending_start = next_line

    for number, line in enumerate(lines, start=1):
        if _SECTION_UP.match(line):
            flush(number + 1)
            active = True
            continue
        if _SECTION_DOWN.match(line):
            flush(number + 1)
            active = False
            continue
        if not active:
            pending_start = number + 1
            continue
        if _BLOCK_BEGIN.match(line):
            flush(number + 1)
            block, block_start = [], number + 1
            continue
        if _BLOCK_END.match(line) and block is not None:
            body = "\n".join(block).strip().rstrip(";").strip()
            if body:
                statements.append((block_start, body))
            block = None
            pending_start = number + 1
            continue
        if block is not None:
            block.append(line)
        else:
            if not pending:
                pending_start = number
            pending.append(line)
    flush(len(lines) + 1)
    return statements


def split_statements(sql: str, dialect: str | None = None) -> list[tuple[int, str]]:
    """Split on ``;`` outside strings, quoted names, comments and ``$$`` bodies.

    Returns ``(line, statement)`` with comments removed. MySQL ``DELIMITER``
    directives and ``#`` comments are honoured when ``dialect`` is MySQL.
    """
    mysql = canonical_dialect(dialect) == "mysql"
    statements: list[tuple[int, str]] = []
    buffer: list[str] = []
    delimiter = ";"
    line = 1
    start_line = None
    index = 0
    length = len(sql)

    def emit() -> None:
        nonlocal buffer, start_line
        statement = "".join(buffer).strip()
        if statement:
            statements.append((start_line or line, statement))
        buffer = []
        start_line = None

    while index < length:
        char = sql[index]
        at_line_start = index == 0 or sql[index - 1] == "\n"

        if mysql and at_line_start and sql[index : index + 10].upper() == "DELIMITER ":
            end = sql.find("\n", index)
            end = length if end == -1 else end
            emit()
            delimiter = sql[index + 10 : end].strip() or ";"
            index = end
            continue

        if sql.startswith(delimiter, index):
            emit()
            index += len(delimiter)
            continue

        if char == "\n":
            line += 1
            buffer.append(char)
            index += 1
            continue

        if sql.startswith("--", index) or (mysql and char == "#"):
            end = sql.find("\n", index)
            index = length if end == -1 else end
            continue

        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            line += sql.count("\n", index, end)
            buffer.append(" ")
            index = end
            continue

        if start_line is None and not char.isspace():
            start_line = line

        if char in "'\"`":
            escaped = (mysql and char != "`") or (
                char == "'" and index > 0 and sql[index - 1] in "eE"
            )
            end = index + 1
            while end < length:
                if escaped and sql[end] == "\\":
                    end += 2
                    continue
                if sql[end] == char:
                    if end + 1 < length and sql[end + 1] == char:
                        end += 2
                        continue
                    break
                end += 1
            end = min(end + 1, length)
            chunk = sql[index:end]
            line += chunk.count("\n")
            buffer.append(chunk)
            index = end
            continue

        if char == "$":
            match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
            if match:
                tag = match.group(0)
                end = sql.find(tag, index + len(tag))
                end = length if end == -1 else end + len(tag)
                chunk = sql[index:end]
                line += chunk.count("\n")
                buffer.append(chunk)
                index = end
                continue

        buffer.append(char)
        index += 1

    emit()
    return statements


# =========================================================================== replay


def replay(files: Iterable[tuple[str, str]], dialect: str | None = None) -> SqlLoadResult:
    """Apply the statements of ``(name, text)`` files in order."""
    replayer = _Replayer(dialect)
    result = SqlLoadResult(schema=replayer.schema)
    for name, text in files:
        result.files.append(name)
        for line, statement in up_statements(text, dialect):
            result.statements += 1
            replayer.apply(statement, f"{name}:{line}")
    replayer.finish()
    result.warnings = replayer.warnings
    return result


# Rewrites for syntax sqlglot doesn't parse, so the surrounding table isn't lost.
_REWRITES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bbit\s+varying\b", re.IGNORECASE), "varbit"),
    (re.compile(r"\)\s*(without\s+rowid|strict)(\s*,\s*(without\s+rowid|strict))*\s*$", re.I), ")"),
    (re.compile(r"^\s*create\s+unlogged\s+table\b", re.IGNORECASE), "CREATE TABLE"),
]
_COMMENT_NULL = re.compile(
    r"^\s*comment\s+on\s+(table|column)\s+(.+?)\s+is\s+null\s*$", re.IGNORECASE | re.DOTALL
)
_RENAME_TABLE = re.compile(r"^\s*rename\s+table\s+(.+)$", re.IGNORECASE | re.DOTALL)
_ALTER_TYPE = re.compile(r"^\s*alter\s+type\s+(\S+)\s+(.*)$", re.IGNORECASE | re.DOTALL)
_RENAME_CONSTRAINT = re.compile(
    r"^\s*alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(\S+)\s+rename\s+constraint\s+(\S+)\s+to\s+(\S+)\s*$",
    re.IGNORECASE,
)
_ADD_IDENTITY = re.compile(
    r"^\s*alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(\S+)\s+alter\s+(?:column\s+)?(\S+)\s+add\s+generated\b",
    re.IGNORECASE,
)
_SCHEMA_CHANGE_HINT = re.compile(r"\b(create|alter|drop)\s+table\b", re.IGNORECASE)
_DDL_KEYWORDS = re.compile(r"^\s*(create|alter|drop)\s+(table|index|unique\s+index)\b", re.I)


class _Replayer:
    def __init__(self, dialect: str | None) -> None:
        self.dialect = canonical_dialect(dialect) or "postgresql"
        self.read = sqlglot_dialect(self.dialect)
        self.default_schema = default_schema_for(self.dialect)
        self.fold_lower = self.dialect in {"postgresql", "oracle"}
        self.case_insensitive = self.dialect in {"mysql", "sqlite", "mssql"}
        self.schema = Schema(dialect=self.dialect)
        self.warnings: list[str] = []
        self.pk_names: dict[str, str] = {}
        self.declared_types: dict[str, str] = {}

    # ------------------------------------------------------------------ entry

    def apply(self, statement: str, where: str) -> None:
        for pattern, replacement in _REWRITES:
            statement = pattern.sub(replacement, statement)

        if self._apply_textual(statement, where):
            return
        try:
            tree = sqlglot.parse_one(statement, read=self.read)
        except SqlglotError as exc:
            if _DDL_KEYWORDS.match(statement):
                self.warn(where, f"could not parse statement, skipped ({_first_line(exc)})")
            return
        if tree is None:
            return
        if self.dialect == "sqlite":
            self.declared_types = _declared_types(statement, self.read)
        try:
            self._dispatch(tree, statement, where)
        except _Skip as skip:
            self.warn(where, str(skip))
        except Exception as exc:  # an AST shape we haven't met; keep going
            self.warn(where, f"statement skipped ({type(exc).__name__}: {exc})")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")

    def _dispatch(self, tree: exp.Expression, statement: str, where: str) -> None:
        if isinstance(tree, exp.Create):
            kind = (tree.args.get("kind") or "").upper()
            if kind == "TABLE":
                self._create_table(tree, where)
            elif kind == "INDEX":
                self._create_index(tree, where)
            elif kind == "TYPE":
                self._create_type(tree)
        elif isinstance(tree, exp.Alter):
            kind = (tree.args.get("kind") or "").upper()
            if kind == "TABLE":
                self._alter_table(tree, where)
            elif kind == "INDEX":
                self._alter_index(tree)
        elif isinstance(tree, exp.Drop):
            self._drop(tree)
        elif isinstance(tree, exp.Comment):
            self._comment(tree, where)
        elif isinstance(tree, exp.Command):
            if _DDL_KEYWORDS.match(statement):
                self.warn(where, "statement uses syntax the parser doesn't model; skipped")
            elif str(tree.this).upper() == "DO" and _SCHEMA_CHANGE_HINT.search(statement):
                self.warn(where, "DO block changes tables; its changes are not reflected")

    # ------------------------------------------------------------------ names

    def ident(self, node) -> str | None:
        if node is None:
            return None
        if isinstance(node, str):
            return node.lower() if self.fold_lower else node
        if isinstance(node, (exp.Column, exp.Ordered)) and not isinstance(node.this, exp.Star):
            return self.ident(node.this)
        if isinstance(node, exp.Identifier):
            text = node.this
            return text if node.quoted or not self.fold_lower else text.lower()
        if isinstance(node, (exp.Dot, exp.Table)):
            return self.ident(node.this)
        return node.sql(dialect=self.read)

    def table_key(self, node: exp.Expression) -> str:
        if isinstance(node, exp.Schema):
            node = node.this
        db = self.ident(node.args.get("db"))
        if db == self.default_schema:
            db = None
        return qualified_name(db, self.ident(node.this))

    def find_table(self, key: str) -> Table | None:
        table = self.schema.tables.get(key)
        if table is None and self.case_insensitive:
            lowered = key.lower()
            table = next((t for k, t in self.schema.tables.items() if k.lower() == lowered), None)
        return table

    def require_table(self, key: str, where: str) -> Table | None:
        table = self.find_table(key)
        if table is None:
            self.warn(where, f"table {key!r} is altered before it is created; skipped")
        return table

    @staticmethod
    def find_column(table: Table, name: str, case_insensitive: bool) -> Column | None:
        column = table.column(name)
        if column is None and case_insensitive:
            column = next((c for c in table.columns if c.name.lower() == name.lower()), None)
        return column

    def column_names(self, nodes: Iterable) -> list[str]:
        return [self.ident(node) for node in nodes or []]

    # ------------------------------------------------------------------ CREATE TABLE

    def _create_table(self, tree: exp.Create, where: str) -> None:
        target = tree.this
        if not isinstance(target, exp.Schema):
            raise _Skip("CREATE TABLE ... AS SELECT isn't modelled; table skipped")
        key = self.table_key(target)
        if self.find_table(key):
            if tree.args.get("exists"):
                return
            self.warn(where, f"table {key!r} created twice; keeping the later definition")
        name = self.ident(target.this.this)
        db = None if "." not in key else key.rsplit(".", 1)[0]
        table = Table(name=name, schema=db)

        for item in target.expressions:
            if isinstance(item, exp.ColumnDef):
                self._add_column(table, item, where)
            elif isinstance(item, exp.LikeProperty):
                source = self.find_table(self.table_key(item.this))
                if source:
                    table.columns.extend(Column(**vars(c)) for c in source.columns)
            else:
                self._add_constraint(table, item, where)

        properties = tree.args.get("properties")
        for prop in properties.expressions if properties else []:
            if isinstance(prop, exp.SchemaCommentProperty):
                table.comment = _literal(prop.this)
        self.schema.add_table(table)
        self._finish_table(table)

    def _add_column(self, table: Table, node: exp.ColumnDef, where: str, at: int | None = None):
        name = self.ident(node.this)
        if self.find_column(table, name, self.case_insensitive):
            if node.args.get("exists"):
                return
            self.warn(where, f"column {table.key}.{name} added twice")
            self._drop_column(table, name)
        column = Column(name=name, type="unknown")
        self._set_type(column, node.args.get("kind"))
        if at is None:
            table.columns.append(column)
        else:
            table.columns.insert(at, column)
        for constraint in node.args.get("constraints") or []:
            self._column_constraint(table, column, constraint)
        if column.name in table.primary_key:
            column.nullable = False
        position = node.args.get("position")
        if isinstance(position, exp.ColumnPosition):
            self._move_column(table, column, position)

    def _set_type(self, column: Column, kind: exp.Expression | None) -> None:
        if kind is None:
            return
        raw = self.declared_types.get(column.name) or kind.sql(dialect=self.read)
        raw, serial = split_serial(raw)
        column.type = normalize_type(raw)
        if serial:
            column.autoincrement = True

    def _column_constraint(self, table: Table, column: Column, node: exp.ColumnConstraint):
        kind = node.args.get("kind")
        name = self.ident(node.this) if node.this else None
        if isinstance(kind, exp.NotNullColumnConstraint):
            column.nullable = bool(kind.args.get("allow_null"))
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            table.primary_key = [column.name]
            column.nullable = False
            if name:
                self.pk_names[table.key] = name
        elif isinstance(kind, exp.UniqueColumnConstraint):
            table.add_index(Index([column.name], unique=True, name=name))
        elif isinstance(kind, exp.DefaultColumnConstraint):
            self._set_default(column, kind.this)
        elif isinstance(kind, (exp.AutoIncrementColumnConstraint)):
            column.autoincrement = True
        elif isinstance(kind, exp.GeneratedAsIdentityColumnConstraint):
            if kind.args.get("expression") is not None:
                column.default = normalize_default(
                    f"generated as ({kind.args['expression'].sql(dialect=self.read)})"
                )
            else:
                column.autoincrement = True
        elif isinstance(kind, exp.ComputedColumnConstraint):
            column.default = normalize_default(f"generated as ({kind.this.sql(dialect=self.read)})")
        elif isinstance(kind, exp.Reference):
            self._add_reference(table, [column.name], kind, name)
        elif isinstance(kind, exp.CommentColumnConstraint):
            column.comment = _literal(kind.this)

    def _set_default(self, column: Column, value: exp.Expression | None) -> None:
        if value is None:
            column.default = None
            return
        stripped = value.copy().transform(
            lambda node: node.this if isinstance(node, exp.Cast) else node
        )
        raw = stripped.sql(dialect=self.read)
        if self.dialect == "mysql" and raw.upper() in {"TRUE", "FALSE"}:
            raw = "1" if raw.upper() == "TRUE" else "0"  # MySQL stores booleans as 1/0
        if is_sequence_default(raw):
            column.autoincrement = True
            column.default = None
        else:
            column.default = normalize_default(raw)

    def _add_constraint(self, table: Table, node: exp.Expression, where: str) -> None:
        name = None
        parts = [node]
        if isinstance(node, exp.Constraint):
            name = self.ident(node.this)
            parts = node.expressions
        for part in parts:
            if isinstance(part, exp.PrimaryKey):
                table.primary_key = self.column_names(part.expressions)
                for column_name in table.primary_key:
                    column = self.find_column(table, column_name, self.case_insensitive)
                    if column:
                        column.nullable = False
                if name:
                    self.pk_names[table.key] = name
            elif isinstance(part, exp.ForeignKey):
                self._add_reference(
                    table, self.column_names(part.expressions), part.args["reference"], name
                )
            elif isinstance(part, exp.UniqueColumnConstraint):
                schema_node = part.this
                columns = self.column_names(schema_node.expressions) if schema_node else []
                index_name = name or (self.ident(schema_node.this) if schema_node else None)
                if columns:
                    table.add_index(Index(columns, unique=True, name=index_name))
            elif isinstance(part, exp.IndexColumnConstraint):
                columns = self.column_names(part.expressions)
                if columns:
                    table.add_index(Index(columns, unique=False, name=self.ident(part.this)))
            elif isinstance(part, exp.PrimaryKeyColumnConstraint):
                pass

    def _add_reference(
        self, table: Table, columns: list[str], ref: exp.Reference, name: str | None
    ) -> None:
        target = ref.this
        if isinstance(target, exp.Schema):
            ref_table = self.table_key(target.this)
            ref_columns = self.column_names(target.expressions)
        else:
            ref_table = self.table_key(target)
            ref_columns = []  # resolved to the referenced primary key in finish()
        on_delete = on_update = None
        for option in ref.args.get("options") or []:
            text = " ".join(str(option).upper().split())
            if text.startswith("ON DELETE "):
                on_delete = _action(text[len("ON DELETE ") :])
            elif text.startswith("ON UPDATE "):
                on_update = _action(text[len("ON UPDATE ") :])
        existing = self.find_table(ref_table)
        table.foreign_keys = [fk for fk in table.foreign_keys if fk.columns != columns]
        table.foreign_keys.append(
            ForeignKey(
                columns=columns,
                ref_table=existing.key if existing else ref_table,
                ref_columns=ref_columns,
                name=name,
                on_delete=on_delete,
                on_update=on_update,
            )
        )

    def _finish_table(self, table: Table) -> None:
        if self.dialect != "postgresql":
            return
        # Name unnamed constraints the way PostgreSQL does, so later
        # "DROP CONSTRAINT users_email_key" statements find them.
        self.pk_names.setdefault(table.key, f"{table.name}_pkey")
        for index in table.indexes:
            if index.unique and not index.name:
                index.name = f"{table.name}_{'_'.join(index.columns)}_key"
        for fk in table.foreign_keys:
            if not fk.name:
                fk.name = f"{table.name}_{'_'.join(fk.columns)}_fkey"

    # ------------------------------------------------------------------ ALTER TABLE

    def _alter_table(self, tree: exp.Alter, where: str) -> None:
        key = self.table_key(tree.this)
        table = self.find_table(key)
        if table is None:
            if tree.args.get("exists"):
                return
            self.require_table(key, where)
            return
        for action in tree.args.get("actions") or []:
            self._alter_action(table, action, where)
            table = self.find_table(table.key) or table
        self._finish_table(table)

    def _alter_action(self, table: Table, action: exp.Expression, where: str) -> None:
        if isinstance(action, exp.ColumnDef):
            self._add_column(table, action, where)
        elif isinstance(action, exp.Drop):
            kind = (action.args.get("kind") or "").upper()
            for target in action.args.get("tables") or []:
                name = self.ident(target)
                if kind == "COLUMN":
                    if not self.find_column(table, name, self.case_insensitive):
                        if not action.args.get("exists"):
                            self.warn(where, f"drop of unknown column {table.key}.{name}")
                        continue
                    self._drop_column(table, name)
                elif kind in {"CONSTRAINT", "FOREIGN KEY", "INDEX", "KEY", "UNIQUE"}:
                    self._drop_constraint(table, name)
                elif kind == "PRIMARY KEY":
                    table.primary_key = []
            if kind == "PRIMARY KEY" and not action.args.get("tables"):
                table.primary_key = []
        elif isinstance(action, exp.AlterColumn):
            self._alter_column(table, action, where)
        elif isinstance(action, exp.ModifyColumn):
            definition = action.this
            old_name = self.ident(action.args.get("rename_from") or definition.this)
            old = self.find_column(table, old_name, self.case_insensitive)
            at = table.columns.index(old) if old else None
            if old:
                new_name = self.ident(definition.this)
                if new_name != old.name:
                    self._rename_column(table, old.name, new_name)
                table.columns.remove(table.column(new_name))
            self._add_column(table, definition, where, at=at)
        elif isinstance(action, exp.RenameColumn):
            old = self.find_column(table, self.ident(action.this), self.case_insensitive)
            if old is None:
                self.warn(where, f"rename of unknown column {table.key}.{self.ident(action.this)}")
                return
            self._rename_column(table, old.name, self.ident(action.args["to"]))
        elif isinstance(action, exp.AlterRename):
            new_key = self.table_key(action.this)
            if "." not in new_key and table.schema:
                new_key = qualified_name(table.schema, new_key)
            self._rename_table(table, new_key)
        elif isinstance(action, exp.AddConstraint):
            for item in action.expressions:
                self._add_constraint(table, item, where)

    def _alter_column(self, table: Table, action: exp.AlterColumn, where: str) -> None:
        name = self.ident(action.this)
        column = self.find_column(table, name, self.case_insensitive)
        if column is None:
            self.warn(where, f"alter of unknown column {table.key}.{name}")
            return
        args = action.args
        if args.get("dtype") is not None:
            self._set_type(column, args["dtype"])
        if args.get("default") is not None:
            self._set_default(column, args["default"])
        if "allow_null" in args and args.get("allow_null") is not None:
            column.nullable = bool(args["allow_null"])
        elif args.get("drop"):
            column.default = None
        if args.get("comment") is not None:
            column.comment = _literal(args["comment"])

    def _drop_column(self, table: Table, name: str) -> None:
        column = self.find_column(table, name, self.case_insensitive)
        if column is None:
            return
        table.columns.remove(column)
        table.primary_key = [c for c in table.primary_key if c != column.name]
        table.foreign_keys = [fk for fk in table.foreign_keys if column.name not in fk.columns]
        table.indexes = [ix for ix in table.indexes if column.name not in ix.columns]
        for other in self.schema.tables.values():
            other.foreign_keys = [
                fk
                for fk in other.foreign_keys
                if not (fk.ref_table == table.key and column.name in fk.ref_columns)
            ]

    def _rename_column(self, table: Table, old: str, new: str) -> None:
        column = table.column(old)
        if column is None:
            return
        column.name = new

        def swap(names: list[str]) -> list[str]:
            return [new if n == old else n for n in names]

        table.primary_key = swap(table.primary_key)
        for fk in table.foreign_keys:
            fk.columns = swap(fk.columns)
        for index in table.indexes:
            index.columns = swap(index.columns)
        for other in self.schema.tables.values():
            for fk in other.foreign_keys:
                if fk.ref_table == table.key:
                    fk.ref_columns = swap(fk.ref_columns)

    def _rename_table(self, table: Table, new_key: str) -> None:
        old_key = table.key
        del self.schema.tables[old_key]
        if "." in new_key:
            table.schema, table.name = new_key.rsplit(".", 1)
        else:
            table.schema, table.name = None, new_key
        self.schema.add_table(table)
        if old_key in self.pk_names:
            self.pk_names[table.key] = self.pk_names.pop(old_key)
        for other in self.schema.tables.values():
            for fk in other.foreign_keys:
                if fk.ref_table == old_key:
                    fk.ref_table = table.key

    def _drop_constraint(self, table: Table, name: str) -> None:
        if self.pk_names.get(table.key) == name:
            table.primary_key = []
        table.foreign_keys = [fk for fk in table.foreign_keys if fk.name != name]
        table.indexes = [ix for ix in table.indexes if ix.name != name]

    # ------------------------------------------------------------------ indexes

    def _create_index(self, tree: exp.Create, where: str) -> None:
        index_node = tree.this
        if not isinstance(index_node, exp.Index):
            return
        table_node = index_node.args.get("table")
        if table_node is None:
            return
        table = self.require_table(self.table_key(table_node), where)
        if table is None:
            return
        params = index_node.args.get("params")
        columns = []
        for item in params.args.get("columns") or [] if params else []:
            inner = item.this if isinstance(item, exp.Ordered) else item
            if isinstance(inner, (exp.Column, exp.Identifier)):
                columns.append(self.ident(inner))
            else:
                columns.append(inner.sql(dialect=self.read).lower())
        if not columns:
            return
        name = self.ident(index_node.this) if index_node.this else None
        if name is None and self.dialect == "postgresql":
            name = f"{table.name}_{'_'.join(columns)}_idx"
        index = Index(columns, unique=bool(tree.args.get("unique")), name=name)
        if name:
            existing = next((ix for ix in table.indexes if ix.name == name), None)
            if existing and tree.args.get("exists"):
                return
            table.indexes = [ix for ix in table.indexes if ix.name != name]
        table.add_index(index)

    def _alter_index(self, tree: exp.Alter) -> None:
        old = self.ident(tree.this)
        for action in tree.args.get("actions") or []:
            if isinstance(action, exp.AlterRename):
                new = self.ident(action.this)
                for table in self.schema.tables.values():
                    for index in table.indexes:
                        if index.name == old:
                            index.name = new

    # ------------------------------------------------------------------ DROP / types / comments

    def _drop(self, tree: exp.Drop) -> None:
        kind = (tree.args.get("kind") or "").upper()
        for target in tree.args.get("tables") or []:
            if kind == "TABLE":
                table = self.find_table(self.table_key(target))
                if table is None:
                    continue
                del self.schema.tables[table.key]
                self.pk_names.pop(table.key, None)
                for other in self.schema.tables.values():
                    other.foreign_keys = [
                        fk for fk in other.foreign_keys if fk.ref_table != table.key
                    ]
            elif kind == "INDEX":
                name = self.ident(target)
                for table in self.schema.tables.values():
                    table.indexes = [ix for ix in table.indexes if ix.name != name]
            elif kind == "TYPE":
                self.schema.enums.pop(self.table_key(target), None)

    def _create_type(self, tree: exp.Create) -> None:
        expression = tree.args.get("expression")
        if isinstance(expression, exp.DataType) and expression.this == exp.DataType.Type.ENUM:
            values = [_literal(value) for value in expression.expressions]
            self.schema.enums[self.table_key(tree.this)] = values

    def _comment(self, tree: exp.Comment, where: str) -> None:
        kind = (tree.args.get("kind") or "").upper()
        text = _literal(tree.args.get("expression"))
        target = tree.this
        if kind == "TABLE":
            table = self.require_table(self.table_key(target), where)
            if table:
                table.comment = text or None
        elif kind == "COLUMN" and isinstance(target, exp.Column):
            table_node = exp.Table(this=target.args.get("table"), db=target.args.get("db"))
            table = self.require_table(self.table_key(table_node), where)
            if table:
                column = self.find_column(table, self.ident(target.this), self.case_insensitive)
                if column:
                    column.comment = text or None

    # ------------------------------------------------------------------ textual fallbacks

    def _apply_textual(self, statement: str, where: str) -> bool:
        """Statements sqlglot can't parse but which matter for the schema."""
        match = _COMMENT_NULL.match(statement)
        if match:
            kind, target = match.group(1).upper(), self._split_name(match.group(2))
            if kind == "TABLE":
                table = self.find_table(self._key_from_parts(target))
                if table:
                    table.comment = None
            elif len(target) >= 2:
                table = self.find_table(self._key_from_parts(target[:-1]))
                column = table and self.find_column(table, target[-1], self.case_insensitive)
                if column:
                    column.comment = None
            return True

        match = _RENAME_TABLE.match(statement)
        if match:
            for pair in match.group(1).split(","):
                names = re.split(r"\s+to\s+", pair.strip(), flags=re.IGNORECASE)
                if len(names) != 2:
                    continue
                table = self.find_table(self._key_from_parts(self._split_name(names[0])))
                if table:
                    self._rename_table(table, self._key_from_parts(self._split_name(names[1])))
            return True

        match = _RENAME_CONSTRAINT.match(statement)
        if match:
            table = self.find_table(self._key_from_parts(self._split_name(match.group(1))))
            if table:
                old, new = (self._split_name(match.group(i))[-1] for i in (2, 3))
                if self.pk_names.get(table.key) == old:
                    self.pk_names[table.key] = new
                for item in [*table.foreign_keys, *table.indexes]:
                    if item.name == old:
                        item.name = new
            return True

        match = _ADD_IDENTITY.match(statement)
        if match:
            table = self.find_table(self._key_from_parts(self._split_name(match.group(1))))
            column_name = self._split_name(match.group(2))[-1]
            column = table and self.find_column(table, column_name, self.case_insensitive)
            if column:
                column.autoincrement = True
            return True

        match = _ALTER_TYPE.match(statement)
        if match:
            self._alter_type(self._key_from_parts(self._split_name(match.group(1))), match.group(2))
            return True
        return False

    def _alter_type(self, key: str, rest: str) -> None:
        values = self.schema.enums.get(key)
        add = re.match(
            r"add\s+value\s+(?:if\s+not\s+exists\s+)?'((?:[^']|'')*)'"
            r"(?:\s+(before|after)\s+'((?:[^']|'')*)')?",
            rest,
            re.IGNORECASE,
        )
        rename_value = re.match(
            r"rename\s+value\s+'((?:[^']|'')*)'\s+to\s+'((?:[^']|'')*)'", rest, re.IGNORECASE
        )
        rename = re.match(r"rename\s+to\s+(\S+)", rest, re.IGNORECASE)
        if add and values is not None:
            value = add.group(1).replace("''", "'")
            if value not in values:
                anchor = add.group(3)
                if anchor and anchor in values:
                    position = values.index(anchor) + (add.group(2).lower() == "after")
                    values.insert(position, value)
                else:
                    values.append(value)
        elif rename_value and values is not None:
            old, new = (rename_value.group(i).replace("''", "'") for i in (1, 2))
            self.schema.enums[key] = [new if v == old else v for v in values]
        elif rename:
            new_key = self._key_from_parts(self._split_name(rename.group(1)))
            if values is not None:
                self.schema.enums[new_key] = self.schema.enums.pop(key)
            old_type, new_type = (
                normalize_type(key.split(".")[-1]),
                normalize_type(new_key.split(".")[-1]),
            )
            for table in self.schema.tables.values():
                for column in table.columns:
                    if column.type == old_type:
                        column.type = new_type
                    elif column.type == old_type + "[]":
                        column.type = new_type + "[]"

    def _split_name(self, text: str) -> list[str]:
        parts = re.findall(r'"((?:[^"]|"")*)"|`([^`]*)`|\[([^\]]*)\]|([^."`\[\]\s;]+)', text)
        names = []
        for quoted, backtick, bracket, bare in parts:
            if quoted or backtick or bracket:
                names.append((quoted or backtick or bracket).replace('""', '"'))
            elif bare:
                names.append(bare.lower() if self.fold_lower else bare)
        return names

    def _key_from_parts(self, parts: list[str]) -> str:
        if not parts:
            return ""
        db = parts[-2] if len(parts) >= 2 else None
        if db == self.default_schema:
            db = None
        return qualified_name(db, parts[-1])

    # ------------------------------------------------------------------ helpers

    def _move_column(self, table: Table, column: Column, position: exp.ColumnPosition) -> None:
        where = (position.args.get("position") or "").upper()
        table.columns.remove(column)
        if where == "FIRST":
            table.columns.insert(0, column)
            return
        anchor = self.ident(position.this)
        names = [c.name for c in table.columns]
        index = names.index(anchor) + 1 if anchor in names else len(table.columns)
        table.columns.insert(index, column)

    def finish(self) -> None:
        for table in self.schema.tables.values():
            for fk in table.foreign_keys:
                if not fk.ref_columns:
                    target = self.find_table(fk.ref_table)
                    fk.ref_columns = list(target.primary_key) if target else []
            table.canonicalize()


class _Skip(Exception):
    """A statement that was understood but intentionally not applied."""


_TYPE_STOP_WORDS = {
    "CONSTRAINT",
    "PRIMARY",
    "NOT",
    "NULL",
    "UNIQUE",
    "CHECK",
    "DEFAULT",
    "COLLATE",
    "REFERENCES",
    "GENERATED",
    "AS",
    "AUTOINCREMENT",
    "ON",
}
_TABLE_ITEM_KEYWORDS = {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}


def _declared_types(statement: str, read: str) -> dict[str, str]:
    """Column -> type text exactly as written in a CREATE TABLE / ADD COLUMN.

    SQLite stores declared types verbatim (and reflection returns them), but
    sqlglot maps them to storage affinities (VARCHAR(255) -> TEXT(255)).
    """
    try:
        tokens = sqlglot.Dialect.get_or_raise(read).tokenize(statement)
    except SqlglotError:
        return {}
    words = [token.text.upper() for token in tokens]
    items: list[list] = []
    if words[:2] == ["CREATE", "TABLE"] and "(" in words:
        depth, current = 0, []
        for token, word in zip(tokens[words.index("(") :], words[words.index("(") :], strict=True):
            if word == "(":
                depth += 1
                if depth == 1:
                    continue
            elif word == ")":
                depth -= 1
                if depth == 0:
                    break
            if depth == 1 and word == ",":
                items.append(current)
                current = []
                continue
            current.append(token)
        items.append(current)
    elif words[:2] == ["ALTER", "TABLE"] and "ADD" in words:
        start = words.index("ADD") + 1
        if start < len(words) and words[start] == "COLUMN":
            start += 1
        items.append(tokens[start:])

    declared: dict[str, str] = {}
    for item in items:
        if len(item) < 2 or item[0].text.upper().split(" ", 1)[0] in _TABLE_ITEM_KEYWORDS:
            continue
        depth, last = 0, None
        for token in item[1:]:
            word = token.text.upper()
            # Multi-word keywords ("PRIMARY KEY", "NOT NULL") arrive as one token.
            if depth == 0 and word.split(" ", 1)[0] in _TYPE_STOP_WORDS:
                break
            depth += (word == "(") - (word == ")")
            last = token
        if last is not None:
            declared[item[0].text] = statement[item[1].start : last.end + 1]
    return declared


def _literal(node) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        return node.this
    return node.name if hasattr(node, "name") and node.name else node.sql()


def _action(text: str) -> str | None:
    text = " ".join(text.upper().split())
    return None if text in {"", "NO ACTION"} else text


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0][:160]
