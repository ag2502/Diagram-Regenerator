"""Schema drift: how far each environment has wandered from a baseline.

The baseline is usually the migrations in the repository (what the code
expects); environments are live databases. Each environment is diffed
against the baseline, so ``missing`` means the environment lacks something
the baseline has: typically a migration that never ran there, or a hotfix
applied by hand somewhere else.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from diagram_regenerator.diff import SEVERITIES, Change, SchemaDiff, diff_schemas
from diagram_regenerator.model import Schema
from diagram_regenerator.report import ICONS

_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


@dataclass
class DriftReport:
    baseline_name: str
    environments: list[str]
    diffs: dict[str, SchemaDiff] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def drifted(self) -> list[str]:
        return [n for n in self.environments if n in self.diffs and self.diffs[n].has_changes]

    @property
    def has_drift(self) -> bool:
        return bool(self.drifted or self.errors)

    def worst(self, name: str) -> str | None:
        diff = self.diffs.get(name)
        return diff.worst() if diff else None

    def summary(self) -> str:
        total = len(self.environments)
        if not self.has_drift:
            return f"All {total} environment{'s' * (total != 1)} match {self.baseline_name}."
        parts = []
        for name in self.drifted:
            count = len(self.diffs[name].changes)
            parts.append(f"{name} ({count} difference{'s' * (count != 1)})")
        parts += [f"{name} (unreachable)" for name in self.errors]
        return (
            f"{len(parts)} of {total} environments differ from {self.baseline_name}: "
            + ", ".join(parts)
        )

    def matrix(self) -> list[tuple[str, dict[str, str], str]]:
        """Rows of ``(subject, {environment: cell}, worst severity)`` for every difference."""
        rows: dict[str, dict[str, list[str]]] = {}
        severity: dict[str, str] = {}
        for name in self.environments:
            diff = self.diffs.get(name)
            for change in diff.changes if diff else []:
                subject = _subject(change)
                rows.setdefault(subject, {}).setdefault(name, []).append(_cell(change))
                current = severity.get(subject)
                if current is None or _RANK[change.severity] < _RANK[current]:
                    severity[subject] = change.severity
        result = []
        for subject in sorted(rows):
            cells = {}
            for name in self.environments:
                if name in self.errors:
                    cells[name] = "?"
                else:
                    cells[name] = ", ".join(rows[subject].get(name, [])) or "✓"
            result.append((subject, cells, severity[subject]))
        return result

    def to_dict(self) -> dict:
        return {
            "baseline": self.baseline_name,
            "summary": self.summary(),
            "has_drift": self.has_drift,
            "environments": {
                name: {
                    "status": "error"
                    if name in self.errors
                    else ("drift" if name in self.drifted else "ok"),
                    "error": self.errors.get(name),
                    "fingerprint": self.diffs[name].new.fingerprint()
                    if name in self.diffs
                    else None,
                    "changes": [c.to_dict() for c in self.diffs[name].changes]
                    if name in self.diffs
                    else [],
                }
                for name in self.environments
            },
        }


def _subject(change: Change) -> str:
    if change.kind.startswith(("index_", "foreign_key_added", "foreign_key_removed")):
        return f"{change.table}: {change.after or change.before}"
    if change.kind.startswith("enum_") or change.table is None:
        return change.summary.replace("`", "")
    if change.column:
        return f"{change.table}.{change.column}"
    return change.table


_CELLS = {
    "table_added": "extra table",
    "table_removed": "missing",
    "table_renamed": "renamed",
    "column_added": "extra",
    "column_removed": "missing",
    "column_renamed": "renamed",
    "column_nullable": "nullable",
    "column_not_null": "not null",
    "index_added": "extra",
    "index_removed": "missing",
    "foreign_key_added": "extra",
    "foreign_key_removed": "missing",
    "column_order_changed": "column order",
    "table_comment_changed": "comment",
    "column_comment_changed": "comment",
}


def _cell(change: Change) -> str:
    if change.kind in _CELLS:
        return _CELLS[change.kind]
    if change.kind == "column_type_changed":
        return str(change.after)
    if change.kind == "column_default_changed":
        return f"default {change.after if change.after is not None else 'none'}"
    if change.kind == "primary_key_changed":
        return f"pk ({', '.join(change.after or [])})"
    if change.kind == "foreign_key_action_changed":
        return str(change.after)
    return change.kind.replace("_", " ")


def drift_phrase(change: Change) -> str:
    """Describe a change from the environment's point of view ("missing", "extra")."""
    target = f"`{change.target}`"
    before, after = change.before, change.after
    phrases = {
        "table_added": f"extra table {target}",
        "table_removed": f"missing table {target}",
        "column_added": f"extra column {target}",
        "column_removed": f"missing column {target}",
        "column_type_changed": f"{target} is {after} here (baseline {before})",
        "column_nullable": f"{target} allows NULL here (baseline NOT NULL)",
        "column_not_null": f"{target} is NOT NULL here (baseline allows NULL)",
        "column_default_changed": f"{target} defaults to {after} here (baseline {before})",
        "index_added": f"extra {after} on {target}",
        "index_removed": f"missing {before} on {target}",
        "foreign_key_added": f"extra foreign key {after} on {target}",
        "foreign_key_removed": f"missing foreign key {before} on {target}",
        "primary_key_changed": f"primary key of {target} is {after} here (baseline {before})",
    }
    return phrases.get(change.kind, change.summary)


def detect_drift(
    baseline_name: str,
    baseline: Schema,
    loaders: dict[str, Callable[[], Schema]],
    ignore: Iterable[str] = (),
    workers: int = 4,
) -> DriftReport:
    """Diff every environment against ``baseline``; loaders run in parallel."""
    ignore = list(ignore)
    report = DriftReport(baseline_name=baseline_name, environments=list(loaders))

    def load(name: str) -> tuple[str, Schema | None, str | None]:
        try:
            return name, loaders[name](), None
        except Exception as exc:  # report and continue: one dead env shouldn't hide the rest
            return name, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(loaders) or 1))) as pool:
        for name, schema, error in pool.map(load, list(loaders)):
            if error is not None:
                report.errors[name] = error
            else:
                report.diffs[name] = diff_schemas(baseline, schema, ignore=ignore)
    return report


# --------------------------------------------------------------------------- formatting


def format_drift_text(report: DriftReport) -> str:
    lines = [f"Schema drift against {report.baseline_name}", ""]
    rows = report.matrix()
    if rows:
        width = max(len(subject) for subject, _, _ in rows) + 2
        widths = {
            name: max([len(name)] + [len(cells[name]) for _, cells, _ in rows]) + 2
            for name in report.environments
        }
        header = " " * width + "".join(name.ljust(widths[name]) for name in report.environments)
        lines.append(header.rstrip())
        for subject, cells, _ in rows:
            line = subject.ljust(width) + "".join(
                cells[name].ljust(widths[name]) for name in report.environments
            )
            lines.append(line.rstrip())
        lines.append("")
    for name, error in report.errors.items():
        lines.append(f"{name}: could not be read ({error})")
    if report.errors:
        lines.append("")
    lines.append(report.summary())
    return "\n".join(lines) + "\n"


def format_drift_markdown(report: DriftReport, footer: str | None = None) -> str:
    out = [f"### Schema drift against `{report.baseline_name}`", ""]
    status = []
    for name in report.environments:
        if name in report.errors:
            status.append(f"❓ **{name}** unreachable")
        elif name in report.drifted:
            worst = report.worst(name)
            count = len(report.diffs[name].changes)
            status.append(f"{ICONS[worst]} **{name}** {count} difference{'s' * (count != 1)}")
        else:
            status.append(f"✅ **{name}** matches")
    out += [" · ".join(status), ""]
    rows = report.matrix()
    if rows:
        out.append("| | " + " | ".join(f"`{n}`" for n in report.environments) + " |")
        out.append("| --- " * (len(report.environments) + 1) + "|")
        for subject, cells, severity in rows:
            values = " | ".join(cells[n].replace("|", "\\|") for n in report.environments)
            out.append(f"| {ICONS[severity]} `{subject}` | {values} |")
        out += [
            "",
            "_✓ same as the baseline · missing: the baseline has it, the environment doesn't_",
            "",
        ]
    for name, error in report.errors.items():
        out.append(f"- `{name}` could not be read: {error}")
    if report.errors:
        out.append("")
    if footer:
        out += [footer, ""]
    return "\n".join(out)


def format_drift_json(report: DriftReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
