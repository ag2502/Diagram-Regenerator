"""Schemas from a live database (reflection) or from SQLAlchemy models.

Both paths go through :func:`schema_from_metadata`, so a schema reflected from
production and one declared in ``models.py`` are read the same way.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from collections.abc import Iterable

from sqlalchemy import Computed, MetaData, create_engine, inspect
from sqlalchemy import Table as SATable
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.sql.elements import ClauseElement, TextClause

from diagram_regenerator.dialects import default_schema_for, sqlalchemy_dialect
from diagram_regenerator.model import Column, ForeignKey, Index, Schema, Table, qualified_name
from diagram_regenerator.normalize import is_sequence_default, normalize_default, normalize_type

_SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "mysql",
    "performance_schema",
    "sys",
}


class SourceError(RuntimeError):
    """A source could not be read; the message is meant for the user."""


# --------------------------------------------------------------------------- URLs


def redact_url(url: str) -> str:
    """The URL with any password masked, safe to print or post."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return url


def prepare_url(url: str) -> str:
    """Accept the URL spellings people paste from hosting dashboards.

    ``postgres://`` (Heroku, Render, Supabase) becomes ``postgresql://``, and a
    URL without a driver picks one that is actually installed.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    scheme = url.split("://", 1)[0]
    if scheme == "postgresql" and not _has_module("psycopg2") and _has_module("psycopg"):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    elif scheme in {"mysql", "mariadb"} and not _has_module("MySQLdb") and _has_module("pymysql"):
        url = f"{scheme}+pymysql://" + url.split("://", 1)[1]
    return url


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


# --------------------------------------------------------------------------- live database


def load_database(url: str, schemas: Iterable[str] | None = None) -> Schema:
    """Reflect a live database. Only catalogue queries run; nothing is written.

    ``schemas`` lists the database schemas to read (default: the connection's
    default schema). ``"*"`` reads every non-system schema.
    """
    url = prepare_url(url)
    try:
        engine = create_engine(url)
    except (NoSuchModuleError, ModuleNotFoundError, ImportError) as exc:
        raise SourceError(
            f"no database driver for {redact_url(url)} ({exc}). Install one, e.g. "
            "pip install 'diagram-regenerator[postgres]' or '[mysql]'."
        ) from exc
    except ArgumentError as exc:
        raise SourceError(f"not a database URL: {redact_url(url)} ({exc})") from exc

    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            default_schema = inspector.default_schema_name
            wanted = list(schemas or [])
            if wanted == ["*"]:
                wanted = [s for s in inspector.get_schema_names() if s not in _SYSTEM_SCHEMAS]
            targets = [None if s in (None, "", default_schema) else s for s in wanted] or [None]

            metadata = MetaData()
            for target in dict.fromkeys(targets):
                metadata.reflect(bind=connection, schema=target, resolve_fks=False, views=False)

            schema = schema_from_metadata(
                metadata,
                dialect=engine.dialect,
                default_schema=default_schema,
                reflected=True,
            )
            _reflect_enums(inspector, targets, schema)
            if engine.dialect.name == "sqlite":
                _sqlite_fixups(connection, inspector, schema)
            return schema
    except SourceError:
        raise
    except Exception as exc:  # driver errors vary widely; surface them readably
        raise SourceError(f"could not read {redact_url(url)}: {exc}") from exc
    finally:
        engine.dispose()


def _reflect_enums(inspector, targets: list[str | None], schema: Schema) -> None:
    get_enums = getattr(inspector, "get_enums", None)
    if get_enums is None:
        return
    for target in targets:
        for enum in get_enums(schema=target or None):
            enum_schema = enum.get("schema")
            if enum_schema == inspector.default_schema_name:
                enum_schema = None
            schema.enums[qualified_name(enum_schema, enum["name"])] = list(enum["labels"])


def _sqlite_fixups(connection, inspector, schema: Schema) -> None:
    """Recover what SQLAlchemy's SQLite reflection drops for inline constraints.

    Inline ``UNIQUE`` columns live only in ``sqlite_autoindex_*`` indexes, and
    inline ``REFERENCES ... ON DELETE`` actions only in ``PRAGMA foreign_key_list``.
    """
    for table in schema.tables.values():
        for index in inspector.get_indexes(table.name, include_auto_indexes=True):
            if (index.get("name") or "").startswith("sqlite_autoindex_"):
                table.add_index(Index(list(index["column_names"]), bool(index["unique"])))

        actions: dict[tuple, tuple[str, str]] = {}
        quoted = table.name.replace('"', '""')
        rows = connection.exec_driver_sql(f'PRAGMA foreign_key_list("{quoted}")').fetchall()
        grouped: dict[int, list] = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(row)
        for group in grouped.values():
            group.sort(key=lambda row: row[1])
            columns = tuple(row[3] for row in group)
            actions[(columns, group[0][2])] = (group[0][6], group[0][5])
        for fk in table.foreign_keys:
            on_delete, on_update = actions.get((tuple(fk.columns), fk.ref_table), (None, None))
            fk.on_delete = fk.on_delete or _action(on_delete)
            fk.on_update = fk.on_update or _action(on_update)
        table.canonicalize()


def _action(value: str | None) -> str | None:
    return None if not value or value.upper() == "NO ACTION" else value.upper()


# --------------------------------------------------------------------------- models


def load_models(target: str, dialect: str | None = None) -> Schema:
    """Read SQLAlchemy models: ``package.module:Base`` (or ``:metadata``, ``:db.Model``).

    The current directory is put on ``sys.path`` so project packages import the
    way they do in the app. Anything with a ``.metadata`` works, which covers
    declarative ``Base``, Flask-SQLAlchemy's ``db`` and SQLModel.
    """
    module_name, _, attr_path = target.partition(":")
    if not module_name:
        raise SourceError(f"models target needs a module, e.g. myapp.models:Base (got {target!r})")
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        obj = importlib.import_module(module_name)
    except ImportError as exc:
        raise SourceError(f"could not import {module_name!r}: {exc}") from exc

    if attr_path:
        for part in attr_path.split("."):
            try:
                obj = getattr(obj, part)
            except AttributeError as exc:
                raise SourceError(f"{module_name} has no attribute {attr_path!r}") from exc
        metadata = obj if isinstance(obj, MetaData) else getattr(obj, "metadata", None)
    else:
        metadata = _find_metadata(obj)

    if not isinstance(metadata, MetaData):
        raise SourceError(f"{target!r} is not a SQLAlchemy MetaData or a class with .metadata")
    sa_dialect = sqlalchemy_dialect(dialect)
    return schema_from_metadata(
        metadata,
        dialect=sa_dialect,
        default_schema=default_schema_for(dialect),
        reflected=False,
    )


def _find_metadata(module) -> MetaData | None:
    for name in ("metadata", "Base", "db", "Model", "SQLModel"):
        candidate = getattr(module, name, None)
        if isinstance(candidate, MetaData):
            return candidate
        if isinstance(getattr(candidate, "metadata", None), MetaData):
            return candidate.metadata
    return None


# --------------------------------------------------------------------------- conversion


def schema_from_metadata(
    metadata: MetaData,
    dialect: Dialect,
    default_schema: str | None = None,
    reflected: bool = False,
) -> Schema:
    """Convert SQLAlchemy ``Table`` objects into the tool's :class:`Schema`."""

    def local(schema_name: str | None) -> str | None:
        return None if schema_name in (None, "", default_schema) else schema_name

    schema = Schema(dialect=dialect.name if dialect.name != "default" else None)
    for sa_table in metadata.sorted_tables:
        table = Table(name=sa_table.name, schema=local(sa_table.schema), comment=sa_table.comment)
        autoinc = _autoincrement_column(sa_table, reflected)
        for sa_column in sa_table.columns:
            raw_default = _server_default(sa_column, dialect)
            table.columns.append(
                Column(
                    name=sa_column.name,
                    type=_type_name(sa_column.type, dialect),
                    # SQLite lets PK columns be NULL; no design means that.
                    nullable=bool(sa_column.nullable) and not sa_column.primary_key,
                    default=None
                    if is_sequence_default(raw_default)
                    else normalize_default(raw_default),
                    autoincrement=(
                        sa_column is autoinc
                        or sa_column.identity is not None
                        or is_sequence_default(raw_default)
                    ),
                    comment=sa_column.comment or (None if reflected else sa_column.doc),
                )
            )
        table.primary_key = [column.name for column in sa_table.primary_key.columns]

        for constraint in sorted(sa_table.foreign_key_constraints, key=lambda c: c.name or ""):
            elements = list(constraint.elements)
            if not elements:
                continue
            ref_schema, ref_name = _referred(elements[0])
            table.foreign_keys.append(
                ForeignKey(
                    columns=[element.parent.name for element in elements],
                    ref_table=qualified_name(local(ref_schema), ref_name),
                    ref_columns=[_referred_column(element) for element in elements],
                    name=constraint.name,
                    on_delete=_action(constraint.ondelete),
                    on_update=_action(constraint.onupdate),
                )
            )

        for constraint in sa_table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint":
                table.add_index(
                    Index([column.name for column in constraint.columns], True, constraint.name)
                )
        for column in sa_table.columns:
            if column.unique:
                table.add_index(Index([column.name], unique=True))
        for sa_index in sorted(sa_table.indexes, key=lambda ix: ix.name or ""):
            columns = [column.name for column in sa_index.columns] or [
                str(expression) for expression in sa_index.expressions
            ]
            table.add_index(Index(columns, bool(sa_index.unique), sa_index.name))

        _drop_mysql_fk_indexes(table, dialect)
        table.canonicalize()
        schema.add_table(table)
    return schema


def _autoincrement_column(sa_table: SATable, reflected: bool):
    if reflected:
        # Reflection marks real AUTO_INCREMENT columns explicitly; don't guess.
        return next((c for c in sa_table.columns if c.autoincrement is True), None)
    try:
        return sa_table.autoincrement_column
    except Exception:  # pragma: no cover - defensive for exotic tables
        return None


def _referred(element) -> tuple[str | None, str]:
    target = element.target_fullname  # "schema.table.column" or "table.column"
    parts = target.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-2]), parts[-2]
    return None, parts[0]


def _referred_column(element) -> str:
    return element.target_fullname.split(".")[-1]


def _type_name(sa_type, dialect: Dialect) -> str:
    for compile_with in (dialect, None):
        try:
            return normalize_type(sa_type.compile(dialect=compile_with))
        except Exception:
            continue
    return normalize_type(type(sa_type).__name__)


def _server_default(sa_column, dialect: Dialect) -> str | None:
    default = sa_column.server_default
    if default is None:
        return None
    if isinstance(default, Computed):
        return f"generated as ({default.sqltext})"
    arg = getattr(default, "arg", None)
    if arg is None:
        return None
    if isinstance(arg, str):
        return "'" + arg.replace("'", "''") + "'"
    if isinstance(arg, TextClause):
        return arg.text
    if isinstance(arg, ClauseElement):
        try:
            return str(arg.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        except Exception:
            return str(arg)
    return str(arg)


def _drop_mysql_fk_indexes(table: Table, dialect: Dialect) -> None:
    """MySQL silently adds an index per foreign key; it is not part of the design."""
    if dialect.name not in {"mysql", "mariadb"}:
        return
    fk_shapes = {(fk.name, tuple(fk.columns)) for fk in table.foreign_keys}
    table.indexes = [
        index
        for index in table.indexes
        if index.unique or (index.name, tuple(index.columns)) not in fk_shapes
    ]
