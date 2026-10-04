"""Draft missing table and column descriptions.

Two describers share one interface:

* :class:`HeuristicDescriber` works offline from naming conventions
  (``created_at``, ``is_active``, foreign keys, ``*_cents``...).
* :class:`ClaudeDescriber` asks Claude, one request per table, with the
  table's columns, keys and neighbours as context, plus a few sample rows
  only when explicitly enabled (values are redacted and truncated first).

Drafts are written to the descriptions file with a ``[draft]`` prefix and
never replace descriptions a person wrote.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from diagram_regenerator.descriptions import Descriptions
from diagram_regenerator.model import DRAFT_PREFIX, Column, Schema, Table

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"


class DescribeError(RuntimeError):
    """Drafting cannot continue (missing package, credentials...)."""


@dataclass
class TableDraft:
    table: str | None = None
    columns: dict[str, str] = field(default_factory=dict)


class Describer(Protocol):
    name: str

    def describe(
        self,
        table: Table,
        schema: Schema,
        wanted: list[str],
        describe_table: bool,
        samples: list[dict] | None = None,
    ) -> TableDraft: ...


# =========================================================================== heuristics


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return " ".join(part for part in spaced.lower().split("_") if part)


def _thing(table: Table) -> str:
    words = _words(table.name).split()
    return " ".join(words[:-1] + [_singular(words[-1])]) if words else "row"


_EXACT = {
    "email": "Email address.",
    "email_address": "Email address.",
    "phone": "Phone number.",
    "phone_number": "Phone number.",
    "slug": "URL-friendly unique identifier.",
    "uuid": "Universally unique identifier.",
    "guid": "Globally unique identifier.",
    "description": "Free-text description.",
    "notes": "Free-text notes.",
    "note": "Free-text note.",
    "currency": "ISO 4217 currency code.",
    "currency_code": "ISO 4217 currency code.",
    "locale": "Locale code, e.g. en-US.",
    "timezone": "IANA time zone name.",
    "time_zone": "IANA time zone name.",
    "ip": "IP address.",
    "ip_address": "IP address.",
    "user_agent": "Client user-agent string.",
    "latitude": "Latitude in decimal degrees.",
    "lat": "Latitude in decimal degrees.",
    "longitude": "Longitude in decimal degrees.",
    "lng": "Longitude in decimal degrees.",
    "lon": "Longitude in decimal degrees.",
    "password": "Password hash; never store plain text.",
    "password_hash": "Hashed password.",
    "encrypted_password": "Hashed password.",
    "position": "Sort position.",
    "sort_order": "Sort position.",
    "version": "Row version, for optimistic locking.",
    "lock_version": "Row version, for optimistic locking.",
    "metadata": "Free-form metadata.",
    "meta": "Free-form metadata.",
    "settings": "Configuration values.",
    "url": "Web address.",
    "price": "Price.",
    "amount": "Monetary amount.",
    "total": "Total amount.",
}


class HeuristicDescriber:
    """Descriptions from naming conventions. Fast, free and offline; conservative."""

    name = "heuristic"

    def describe(self, table, schema, wanted, describe_table, samples=None) -> TableDraft:
        draft = TableDraft()
        if describe_table:
            draft.table = self._table(table)
        for name in wanted:
            column = table.column(name)
            text = self._column(table, column, schema) if column else None
            if text:
                draft.columns[name] = text
        return draft

    def _table(self, table: Table) -> str | None:
        """Only join tables get a description: "Users." would add nothing."""
        if len(table.foreign_keys) != 2:
            return None
        fk_columns = {c for fk in table.foreign_keys for c in fk.columns}
        keyed_by_links = bool(table.primary_key) and set(table.primary_key) == fk_columns
        unkeyed_link = not table.primary_key and len(table.columns) <= len(fk_columns) + 1
        if not (keyed_by_links or unkeyed_link):
            return None
        a, b = (fk.ref_table for fk in table.foreign_keys)
        return f"Links {a} and {b} (many-to-many)."

    def _column(self, table: Table, column: Column, schema: Schema) -> str | None:
        name = column.name
        key = _words(name).replace(" ", "_")
        thing = _thing(table)
        fk = table.foreign_key_for(name)
        if name in table.primary_key and len(table.primary_key) == 1:
            kind = " (UUID)" if "uuid" in column.type else ""
            return f"Primary key{kind}."
        if fk:
            position = fk.columns.index(name)
            target = fk.ref_columns[position] if position < len(fk.ref_columns) else "id"
            text = f"References {fk.ref_table}.{target}"
            if fk.on_delete == "CASCADE":
                text += "; rows are deleted with it"
            elif fk.on_delete == "SET NULL":
                text += "; cleared when it is deleted"
            return text + "."
        if key in {"created_at", "inserted_at", "created_on", "date_created"}:
            return f"When the {thing} was created."
        if key in {"updated_at", "modified_at", "updated_on", "last_modified"}:
            return f"When the {thing} was last updated."
        if key in {"deleted_at", "archived_at"}:
            verb = "deleted" if key == "deleted_at" else "archived"
            return f"When the {thing} was {verb}; NULL while it is active (soft delete)."
        if key in _EXACT:
            return _EXACT[key]
        words = _words(name).split()
        if words and words[0] in {"is", "has", "can", "should", "was"} and len(words) > 1:
            return f"Whether the {thing} {words[0]} {' '.join(words[1:])}."
        if words and words[-1] in {"at", "on"} and len(words) > 1:
            return _event_phrase(thing, words[:-1], date=words[-1] == "on")
        if words and words[-1] == "cents" and len(words) > 1:
            return f"{' '.join(words[:-1]).capitalize()} in cents."
        if words and words[-1] == "count" and len(words) > 1:
            counted = " ".join(words[:-1])
            return f"Number of {counted if counted.endswith('s') else counted + 's'}."
        if words and words[-1] == "url":
            return f"URL of the {' '.join(words[:-1]) or thing}."
        if words and words[-1] == "id" and len(words) > 1:
            return f"Identifier of the related {' '.join(words[:-1])} (no foreign key)."
        if key in {"name", "title", "label"}:
            return f"{key.capitalize()} of the {thing}."
        if key in {"status", "state"}:
            values = _enum_values(column, schema)
            suffix = f" One of: {', '.join(values)}." if values else ""
            return f"Current {key} of the {thing}.{suffix}"
        if key in {"type", "kind", "category"}:
            return f"{key.capitalize()} of {thing}."
        return None


def _enum_values(column: Column, schema: Schema) -> list[str]:
    match = re.match(r"enum\((.*)\)$", column.type)
    if match:
        return re.findall(r"'((?:[^']|'')*)'", match.group(1))
    for name, values in schema.enums.items():
        if column.type == name.split(".")[-1].lower():
            return list(values)
    return []


_IRREGULAR_PAST = {
    "paid",
    "sent",
    "built",
    "held",
    "made",
    "read",
    "run",
    "seen",
    "won",
    "lost",
    "sold",
    "bought",
    "taken",
    "given",
    "done",
    "begun",
    "shown",
    "known",
    "written",
    "spent",
    "set",
    "put",
    "cut",
    "hit",
    "quit",
    "shut",
    "left",
    "met",
    "kept",
    "found",
}


def _event_phrase(thing: str, words: list[str], date: bool) -> str:
    """``paid_at`` -> "When the invoice was paid."; ``renews_on`` -> "When the ... renews."."""
    event = " ".join(words)
    if len(words) == 1:
        word = words[0]
        if word.endswith("ed") or word in _IRREGULAR_PAST:
            return f"When the {thing} was {word}."
        if word == "due":
            return f"When the {thing} is due."
        if word.endswith("s") and not word.endswith("ss"):
            return f"When the {thing} {word}."
    return f"{event.capitalize()} {'date' if date else 'time'}."


# =========================================================================== Claude

SYSTEM_PROMPT = """\
You write the data dictionary for a relational database: one short, factual \
description per table and per column, for engineers and analysts reading the docs.

Rules:
- One sentence each, at most about 20 words. No markdown.
- Say what the value means in the business domain, plus units, formats or \
allowed values when the name, type or samples make them clear.
- Do not restate the column name or the SQL type on their own, and do not \
describe constraints already visible in the schema (primary/foreign key, NOT NULL) \
unless they carry meaning (e.g. what a foreign key points at).
- When the meaning is genuinely unclear, give your best reading and end the \
description with "(unclear)". Never invent business rules you can't infer.
- Describe only the columns you are asked about, using their exact names."""


def _output_schema(describe_table: bool) -> dict:
    properties: dict = {
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        }
    }
    required = ["columns"]
    if describe_table:
        properties["table_description"] = {"type": "string"}
        required.insert(0, "table_description")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def table_context(table: Table, schema: Schema) -> str:
    """A compact, model-friendly description of a table and its neighbours."""
    lines = [f"Table: {table.key}"]
    if table.comment:
        lines.append(f"Existing table description: {table.comment}")
    lines.append("Columns:")
    for column in table.columns:
        bits = [column.type, "nullable" if column.nullable else "not null"]
        if column.name in table.primary_key:
            bits.append("primary key")
        fk = table.foreign_key_for(column.name)
        if fk:
            bits.append(f"references {fk.ref_table}({', '.join(fk.ref_columns)})")
        if column.default is not None:
            bits.append(f"default {column.default}")
        if column.autoincrement:
            bits.append("auto-increment")
        line = f"  - {column.name}: {', '.join(bits)}"
        if column.comment:
            line += f" -- {column.comment}"
        lines.append(line)
    unique = [ix.columns for ix in table.indexes if ix.unique]
    if unique:
        lines.append("Unique: " + "; ".join(f"({', '.join(cols)})" for cols in unique))
    referencing = schema.referencing(table.key)
    if referencing:
        lines.append(
            "Referenced by: "
            + ", ".join(f"{other.key}.{', '.join(fk.columns)}" for other, fk in referencing)
        )
    return "\n".join(lines)


class ClaudeDescriber:
    """Drafts descriptions with Claude through the Anthropic SDK (``[llm]`` extra)."""

    name = "claude"

    def __init__(self, model: str | None = None, client=None, effort: str = "low") -> None:
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise DescribeError(
                    "Claude drafting needs the Anthropic SDK: "
                    "pip install 'diagram-regenerator[llm]' (or use --provider heuristic)"
                ) from exc
            client = anthropic.Anthropic(timeout=120.0, max_retries=3)
        self.client = client

    def describe(self, table, schema, wanted, describe_table, samples=None) -> TableDraft:
        import anthropic

        prompt = [table_context(table, schema)]
        if samples:
            prompt.append(
                "Sample rows (redacted, truncated):\n"
                + "\n".join(json.dumps(row, default=str) for row in samples)
            )
        ask = f"Describe these columns: {', '.join(wanted)}." if wanted else "Columns: none."
        if describe_table:
            ask += " Also describe the table itself in table_description."
        prompt.append(ask)

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": "\n\n".join(prompt)}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": _output_schema(describe_table)},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise DescribeError(
                "Anthropic rejected the credentials: set ANTHROPIC_API_KEY (or run "
                "`ant auth login`), or use --provider heuristic"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise DescribeError(f"the API key can't use {self.model}: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise DescribeError(f"unknown model {self.model!r}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise DescribeError(f"could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            log.warning("%s: Claude request failed (%s); skipped", table.key, exc.status_code)
            return TableDraft()

        if response.stop_reason == "refusal":
            log.warning("%s: Claude declined to describe this table; skipped", table.key)
            return TableDraft()
        if response.stop_reason == "max_tokens":
            log.warning("%s: response was cut off; skipped", table.key)
            return TableDraft()
        text = next((block.text for block in response.content if block.type == "text"), None)
        try:
            data = json.loads(text or "")
        except json.JSONDecodeError:
            log.warning("%s: Claude returned unreadable JSON; skipped", table.key)
            return TableDraft()

        allowed = set(wanted)
        draft = TableDraft(
            table=(data.get("table_description") or None) if describe_table else None
        )
        for item in data.get("columns", []):
            name, description = item.get("name"), (item.get("description") or "").strip()
            if name in allowed and description:
                draft.columns[name] = description
        return draft


# =========================================================================== samples

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_SECRETISH = re.compile(r"(pass|secret|token|hash|salt|api_?key|ssn|card|iban|cvv|otp)", re.I)
_LONG_TOKEN = re.compile(r"^[A-Za-z0-9_\-+/=]{24,}$")


def redact_value(column: str, value: object, limit: int = 40) -> object:
    """Mask obviously sensitive values before they leave the machine."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if _SECRETISH.search(column):
        return "<hidden>"
    text = str(value)
    if _LONG_TOKEN.match(text):
        return "<token>"
    text = _EMAIL.sub("<email>", text)
    text = _PHONE.sub("<phone>", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fetch_samples(url: str, table: Table, limit: int) -> list[dict]:
    """Up to ``limit`` redacted rows from a live table (read-only SELECT)."""
    from sqlalchemy import MetaData, create_engine, select
    from sqlalchemy import Table as SATable

    from diagram_regenerator.sources.sqla import prepare_url

    engine = create_engine(prepare_url(url))
    try:
        with engine.connect() as connection:
            sa_table = SATable(
                table.name, MetaData(), schema=table.schema, autoload_with=connection
            )
            rows = connection.execute(select(sa_table).limit(limit)).mappings().all()
            return [{k: redact_value(k, v) for k, v in row.items()} for row in rows]
    finally:
        engine.dispose()


# =========================================================================== driver


@dataclass
class DescribeResult:
    tables: int = 0
    added: int = 0
    skipped_tables: list[str] = field(default_factory=list)


def draft_descriptions(
    schema: Schema,
    docs: Descriptions,
    describer: Describer,
    tables: Iterable[str] | None = None,
    redraft: bool = False,
    sample_url: str | None = None,
    sample_rows: int = 0,
) -> DescribeResult:
    """Fill gaps in ``docs`` (in place) with ``[draft]`` descriptions.

    Descriptions written by people, and database comments, are never replaced.
    With ``redraft``, existing drafts are regenerated too.
    """
    result = DescribeResult()
    selected = set(tables) if tables is not None else None

    def needs(existing: str | None, comment: str | None) -> bool:
        if existing:
            return redraft and existing.lower().startswith(DRAFT_PREFIX)
        return not comment

    for table in schema.sorted_tables():
        if selected is not None and table.key not in selected:
            continue
        describe_table = needs(docs.table(table.key), table.comment)
        wanted = [
            column.name
            for column in table.columns
            if needs(docs.column(table.key, column.name), column.comment)
        ]
        if not wanted and not describe_table:
            continue
        samples = None
        if sample_url and sample_rows > 0:
            try:
                samples = fetch_samples(sample_url, table, sample_rows)
            except Exception as exc:  # samples are optional context
                log.warning("%s: could not read sample rows (%s)", table.key, exc)
        draft = describer.describe(table, schema, wanted, describe_table, samples)
        result.tables += 1
        if not draft.table and not draft.columns:
            result.skipped_tables.append(table.key)
            continue
        if draft.table and describe_table:
            docs.set_table(table.key, f"{DRAFT_PREFIX} {draft.table}", overwrite=True)
            result.added += 1
        for name, text in draft.columns.items():
            docs.set_column(table.key, name, f"{DRAFT_PREFIX} {text}", overwrite=True)
            result.added += 1
    return result
