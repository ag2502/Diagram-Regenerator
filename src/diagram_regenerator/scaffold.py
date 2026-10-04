"""``diagram-regen init``: detect the project's schema source and write a config."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from diagram_regenerator.config import CONFIG_NAME, DEFAULT_DIR

# Common homes for SQL migrations and schema dumps, most specific first.
SOURCE_CANDIDATES = (
    "prisma/migrations",
    "supabase/migrations",
    "src/main/resources/db/migration",
    "db/migrations",
    "database/migrations",
    "sql/migrations",
    "migrations",
    "db/structure.sql",
    "db/schema.sql",
    "sql/schema.sql",
    "schema.sql",
)

_DIALECT_HINTS = {
    "postgresql": [
        r"\bserial\b",
        r"\bjsonb\b",
        r"::\w",
        r"\btimestamptz\b",
        r"\$\$",
        r"\bbigserial\b",
    ],
    "mysql": [r"\bauto_increment\b", r"\bengine\s*=", r"`\w+`", r"\bunsigned\b"],
    "sqlite": [r"\bautoincrement\b", r"\bwithout rowid\b"],
    "mssql": [r"\bidentity\s*\(", r"\bnvarchar\b", r"\[dbo\]", r"^go$"],
}


@dataclass
class Detected:
    source: str | None
    dialect: str
    reason: str


def detect(root: Path) -> Detected:
    for candidate in SOURCE_CANDIDATES:
        path = root / candidate
        if path.is_file() or (path.is_dir() and any(path.rglob("*.sql"))):
            dialect, why = _detect_dialect(path)
            return Detected(candidate, dialect, f"found SQL in {candidate} ({why})")
    if (root / "alembic.ini").is_file() or (root / "manage.py").is_file():
        return Detected(
            None,
            "postgresql",
            "Python migrations (Alembic/Django) found: point `source` at a database the "
            "migrations have been applied to, or at your SQLAlchemy models",
        )
    return Detected(None, "postgresql", "no SQL migrations found")


def _detect_dialect(path: Path) -> tuple[str, str]:
    lock = path / "migration_lock.toml" if path.is_dir() else None
    if lock and lock.is_file():
        match = re.search(r'provider\s*=\s*"(\w+)"', lock.read_text())
        if match:
            provider = {"postgres": "postgresql", "sqlserver": "mssql"}.get(
                match.group(1), match.group(1)
            )
            return provider, "Prisma migration_lock.toml"
    files = [path] if path.is_file() else sorted(path.rglob("*.sql"))[:30]
    text = "\n".join(f.read_text(errors="replace")[:20000] for f in files).lower()
    scores = {
        dialect: sum(len(re.findall(p, text, re.MULTILINE)) for p in patterns)
        for dialect, patterns in _DIALECT_HINTS.items()
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "postgresql", "no dialect-specific syntax seen; assuming PostgreSQL"
    return best, f"looks like {best}"


CONFIG_TEMPLATE = """\
# Diagram Regenerator configuration.
# Reference: https://github.com/ag2502/Diagram-Regenerator#configuration

# Where the schema comes from: a migrations directory, a .sql file, a database
# URL (keep credentials in env vars, e.g. "${{DATABASE_URL}}") or SQLAlchemy
# models as "python:myapp.models:Base".
{source_line}
dialect = "{dialect}"

# Tables to leave out of the docs (glob patterns, e.g. "tmp_*").
exclude = []

# Human-written table and column descriptions; `diagram-regen describe` drafts missing ones.
descriptions = "{out_dir}/descriptions.yml"

[output]
snapshot = "{out_dir}/schema.json"   # commit it: PRs are compared against this file
markdown = "{out_dir}/README.md"     # Mermaid ER diagram + data dictionary
# html = "{out_dir}/index.html"      # searchable, zoomable schema browser
# dbml = "{out_dir}/schema.dbml"     # for dbdiagram.io
# mermaid = "{out_dir}/schema.mmd"
title = "{title}"
diagram = "auto"                     # auto | all | keys | none
# source_label = "Production"        # how the docs name the source

# Focused diagrams for areas of a big schema.
# [groups]
# Billing = ["invoice*", "payment*"]

[diff]
fail_on = "breaking"                 # breaking | warning | any | never
ignore = []                          # comments, defaults, indexes, foreign_keys, enums, column_order

# Databases compared by `diagram-regen drift` (read-only access is enough).
# [environments]
# staging = "${{STAGING_DATABASE_URL}}"
# prod = "${{PROD_DATABASE_URL}}"

# [notify]
# slack_webhook = "${{SLACK_WEBHOOK_URL}}"
"""


def render_config(
    detected: Detected, out_dir: str = DEFAULT_DIR, title: str = "Database schema"
) -> str:
    if detected.source:
        source_line = f'source = "{detected.source}"'
    else:
        source_line = 'source = "${DATABASE_URL}"  # TODO: set this'
    return CONFIG_TEMPLATE.format(
        source_line=source_line, dialect=detected.dialect, out_dir=out_dir, title=title
    )


def write_config(root: Path, content: str, force: bool = False) -> Path:
    path = root / CONFIG_NAME
    if path.exists() and not force:
        raise FileExistsError(f"{CONFIG_NAME} already exists (use --force to overwrite)")
    path.write_text(content, encoding="utf-8")
    return path
