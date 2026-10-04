"""One vocabulary for SQL dialect names.

SQLAlchemy says ``postgresql``/``mssql``, sqlglot says ``postgres``/``tsql``,
people type ``pg`` or ``mariadb``. Everything is stored in SQLAlchemy's
spelling and translated at the edges.
"""

from __future__ import annotations

from sqlalchemy.dialects import registry
from sqlalchemy.engine import Dialect
from sqlalchemy.engine.default import DefaultDialect

_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pg": "postgresql",
    "pgsql": "postgresql",
    "cockroachdb": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "mssql": "mssql",
    "tsql": "mssql",
    "sqlserver": "mssql",
    "oracle": "oracle",
}

_SQLGLOT_NAMES = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "oracle": "oracle",
}

_DEFAULT_SCHEMAS = {"postgresql": "public", "sqlite": "main", "mssql": "dbo"}

SUPPORTED = tuple(sorted(_SQLGLOT_NAMES))


def canonical_dialect(name: str | None) -> str | None:
    """``"pg"`` -> ``"postgresql"``; ``None`` stays ``None``; unknown names raise."""
    if not name:
        return None
    key = name.lower().split("+", 1)[0]
    if key not in _ALIASES:
        raise ValueError(f"unknown dialect {name!r}; expected one of {', '.join(SUPPORTED)}")
    return _ALIASES[key]


def sqlglot_dialect(name: str | None) -> str:
    return _SQLGLOT_NAMES[canonical_dialect(name) or "postgresql"]


def sqlalchemy_dialect(name: str | None) -> Dialect:
    """A dialect instance for compiling types; no database driver is needed."""
    canonical = canonical_dialect(name)
    if canonical is None:
        return DefaultDialect()
    return registry.load(canonical)()


def default_schema_for(name: str | None) -> str | None:
    """The schema unqualified names live in (``public`` on PostgreSQL)."""
    canonical = canonical_dialect(name)
    return _DEFAULT_SCHEMAS.get(canonical) if canonical else None
