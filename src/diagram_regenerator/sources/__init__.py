"""Load a :class:`~diagram_regenerator.model.Schema` from any supported source.

A *source spec* is a single string, so every command accepts the same things:

==============================  =============================================
``postgresql://user@host/db``   live database (any SQLAlchemy URL)
``sqlite:///app.db``            live SQLite file
``db/migrations``               directory of SQL migrations, applied in order
``schema.sql``                  a single DDL file
``schema/schema.json``          a saved snapshot
``python:myapp.models:Base``    SQLAlchemy models (also ``models:``)
==============================  =============================================
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from diagram_regenerator.model import Schema
from diagram_regenerator.sources.sqla import (
    SourceError,
    load_database,
    load_models,
    redact_url,
)

__all__ = ["SourceError", "describe_source", "detect_kind", "load_source", "redact_url"]

_MODEL_PREFIXES = ("python:", "models:")


def detect_kind(spec: str) -> str:
    """Classify a source spec: database, models, snapshot or sql."""
    if spec.startswith(_MODEL_PREFIXES):
        return "models"
    if "://" in spec or spec.startswith("sqlite:"):
        return "database"
    path = Path(spec)
    if spec.endswith(".json"):
        return "snapshot"
    if spec.endswith(".sql") or path.is_dir():
        return "sql"
    if not path.exists():
        raise SourceError(f"source {spec!r} not found (no such file or directory)")
    raise SourceError(
        f"don't know how to read {spec!r}: expected a database URL, a .sql file, a "
        "migrations directory, a .json snapshot or python:module:Base"
    )


def describe_source(spec: str) -> str:
    """Human-readable name for a source, with credentials masked."""
    return redact_url(spec) if detect_kind(spec) == "database" else spec


def load_source(
    spec: str,
    *,
    dialect: str | None = None,
    schemas: Iterable[str] | None = None,
) -> Schema:
    """Read ``spec`` into a :class:`Schema`.

    ``dialect`` matters for SQL files and models (how to parse / compile types);
    ``schemas`` selects database schemas when reflecting a live database.
    """
    kind = detect_kind(spec)
    if kind == "database":
        return load_database(spec, schemas=schemas)
    if kind == "models":
        return load_models(spec.split(":", 1)[1], dialect=dialect)
    if kind == "snapshot":
        path = Path(spec)
        if not path.is_file():
            raise SourceError(f"snapshot {spec!r} not found")
        try:
            return Schema.load(path)
        except ValueError as exc:
            raise SourceError(f"{spec}: {exc}") from exc
    from diagram_regenerator.sources.sql import load_sql

    return load_sql(spec, dialect=dialect)
