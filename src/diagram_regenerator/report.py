"""Present a :class:`~diagram_regenerator.diff.SchemaDiff` to people and machines.

* :func:`format_text`     terminal output, optionally coloured
* :func:`format_markdown` PR comment / job summary, with a visual Mermaid diff
* :func:`format_json`     for scripts
"""

from __future__ import annotations

import json

from diagram_regenerator.diff import SEVERITIES, Change, SchemaDiff, fk_text
from diagram_regenerator.model import Column, Table
from diagram_regenerator.render.mermaid import (
    attribute_line,
    comment_text,
    entity_id,
    relationship_line,
)

COMMENT_MARKER = "<!-- diagram-regenerator:schema-diff -->"

ICONS = {"breaking": "🔴", "warning": "🟠", "safe": "🟢", "info": "⚪"}
_ANSI = {"breaking": "\033[31;1m", "warning": "\033[33m", "safe": "\033[32m", "info": "\033[2m"}
_RESET = "\033[0m"
_SYMBOL = {"added": "+", "removed": "-", "renamed": "→"}
# The summary already says everything for these.
_SELF_DESCRIBING = {
    "table_renamed",
    "column_renamed",
    "index_added",
    "index_removed",
    "foreign_key_added",
    "foreign_key_removed",
}


def _symbol(change: Change) -> str:
    for word, symbol in _SYMBOL.items():
        if change.kind.endswith(word):
            return symbol
    return "~"


def _detail(change: Change) -> str:
    if change.kind in _SELF_DESCRIBING:
        return ""
    if change.before is not None and change.after is not None:
        return f"{_fmt(change.before)} → {_fmt(change.after)}"
    if change.after is not None:
        return _fmt(change.after)
    if change.before is not None:
        return f"was {_fmt(change.before)}"
    return ""


def _fmt(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


# --------------------------------------------------------------------------- text


def format_text(diff: SchemaDiff, color: bool = False) -> str:
    if not diff.has_changes:
        return "No schema changes.\n"
    lines: list[str] = []
    current = object()
    for change in diff.changes:
        if change.table != current:
            current = change.table
            if lines:
                lines.append("")
            lines.append(change.table or "(enums)")
        label = change.severity.upper() if change.severity == "breaking" else change.severity
        if color:
            label = f"{_ANSI[change.severity]}{label}{_RESET}"
        text = change.summary.replace("`", "")
        detail = _detail(change)
        if detail:
            text += f": {detail}"
        lines.append(f"  {_symbol(change)} {text}  [{label}]")
        if change.note and change.severity in {"breaking", "warning"}:
            lines.append(f"      {change.note}")
    lines += ["", diff.summary()]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- JSON


def format_json(diff: SchemaDiff) -> str:
    return json.dumps(diff.to_dict(), indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- Markdown


def format_markdown(
    diff: SchemaDiff,
    title: str = "Schema changes",
    diagram: bool = True,
    footer: str | None = None,
    marker: bool = True,
) -> str:
    out = [COMMENT_MARKER] if marker else []
    out += [f"### {title}", ""]
    if not diff.has_changes:
        out += ["No schema changes. ✅", ""]
        if footer:
            out += [footer, ""]
        return "\n".join(out)

    counts = diff.counts()
    badges = " · ".join(f"{ICONS[s]} {counts[s]} {s}" for s in SEVERITIES if counts[s])
    out += [f"**{diff.summary().split(' (')[0]}** · {badges}", ""]
    if counts["breaking"]:
        out += [
            "> [!WARNING]",
            f"> {counts['breaking']} change{'s' * (counts['breaking'] != 1)} can break running "
            "code or lose data. Check the deploy order (expand → migrate → contract).",
            "",
        ]

    out += ["| | Change | Detail |", "| --- | --- | --- |"]
    for change in diff.changes:
        detail = _cell(_detail(change))
        if change.note and change.severity in {"breaking", "warning"}:
            detail = f"{detail}<br>_{_cell(change.note)}_" if detail else f"_{_cell(change.note)}_"
        out.append(f"| {ICONS[change.severity]} | {_cell(change.summary)} | {detail} |")
    out.append("")

    if diagram and diff.changed_tables():
        out += [
            "<details open><summary>Diagram of the changed tables</summary>",
            "",
            "```mermaid",
            render_diff_mermaid(diff).rstrip(),
            "```",
            "",
            "🟢 added · 🔴 removed · 🟡 changed · unmarked columns are unchanged",
            "",
            "</details>",
            "",
        ]
    if footer:
        out += [footer, ""]
    return "\n".join(out)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- visual diff


def render_diff_mermaid(diff: SchemaDiff, max_comment: int = 50) -> str:
    """An ER diagram of changed tables, annotated with what changed.

    Unchanged neighbours are drawn as bare boxes so the new relationships have
    context. Markers live in attribute comments, which every Mermaid version
    (including GitHub's) renders.
    """
    old, new = diff.old, diff.new
    changed = diff.changed_tables()
    renamed_from = {c.after: c.before for c in diff.changes if c.kind == "table_renamed"}
    marks: dict[tuple[str, str], str] = {}
    for change in diff.changes:
        if change.column is None:
            continue
        key = (change.table, change.column)
        if change.kind == "column_added":
            marks[key] = "🟢 added"
        elif change.kind == "column_renamed":
            marks[key] = f"🟡 renamed from {change.before}"
        elif change.kind == "column_type_changed":
            marks[key] = f"🟡 was {change.before}"
        elif change.kind in {"column_not_null", "column_nullable"}:
            marks.setdefault(key, f"🟡 now {change.after}")
        elif change.kind in {"column_default_changed", "column_comment_changed"}:
            marks.setdefault(key, "🟡 " + change.kind.split("_")[1] + " changed")

    lines = ["erDiagram"]
    drawn: set[str] = set()
    for key in changed:
        table_new, table_old = new.tables.get(key), old.tables.get(key)
        if table_new is None and table_old is None:
            continue
        drawn.add(key)
        lines.append(f"    {entity_id(key)} {{")
        if table_new is None:
            for column in table_old.columns:
                lines.append(f"        {_attr(table_old, column, '🔴 removed table', max_comment)}")
        else:
            whole_table = key not in old.tables and key not in renamed_from
            for column in table_new.columns:
                mark = "🟢 new table" if whole_table else marks.get((key, column.name))
                lines.append(f"        {_attr(table_new, column, mark, max_comment)}")
            source = old.tables.get(renamed_from.get(key, key))
            if source is not None:
                renamed_away = {
                    c.before for c in diff.changes if c.kind == "column_renamed" and c.table == key
                }
                for column in source.columns:
                    if table_new.column(column.name) is None and column.name not in renamed_away:
                        lines.append(f"        {_attr(source, column, '🔴 removed', max_comment)}")
        lines.append("    }")

    relations: list[str] = []
    stubs: set[str] = set()
    fk_changes = {
        (c.table, c.kind, c.after or c.before)
        for c in diff.changes
        if c.kind in {"foreign_key_added", "foreign_key_removed"}
    }
    for key in changed:
        for schema, kind, label_prefix in (
            (new, "foreign_key_added", "🟢 "),
            (old, "foreign_key_removed", "🔴 "),
        ):
            table = schema.tables.get(key)
            for fk in table.foreign_keys if table else []:
                status = (key, kind, fk_text(fk))
                if schema is old and status not in fk_changes:
                    continue
                prefix = label_prefix if status in fk_changes else ""
                label = prefix + ", ".join(fk.columns)
                relations.append(f"    {relationship_line(schema, table, fk, label)}")
                if fk.ref_table not in drawn:
                    stubs.add(fk.ref_table)
        for other, fk in new.referencing(key):
            if other.key not in drawn:
                stubs.add(other.key)
                relations.append(f"    {relationship_line(new, other, fk)}")
    for stub in sorted(stubs):
        lines.append(f"    {entity_id(stub)}")
    lines += list(dict.fromkeys(relations))
    return "\n".join(lines) + "\n"


def _attr(table: Table, column: Column, mark: str | None, limit: int) -> str:
    return attribute_line(table, column, comment_text(mark, limit) if mark else None)
