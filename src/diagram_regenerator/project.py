"""One place that applies the project's config to sources, filters and outputs.

Every command goes through :class:`Project`, so both sides of a diff are read
with the same dialect, filters and bookkeeping-table rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from diagram_regenerator.config import Config, ConfigError
from diagram_regenerator.descriptions import Descriptions
from diagram_regenerator.model import Schema
from diagram_regenerator.render import MarkdownOptions, render
from diagram_regenerator.sources import SourceError, describe_source, detect_kind, load_source
from diagram_regenerator.sources.git import load_git, path_exists_at


@dataclass
class OutputFile:
    fmt: str
    path: Path
    content: str

    @property
    def current(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    @property
    def stale(self) -> bool:
        return self.current != self.content

    def write(self) -> bool:
        """Write if the content changed; returns whether it did."""
        if not self.stale:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.content, encoding="utf-8")
        return True


class Project:
    def __init__(self, config: Config, source_override: str | None = None) -> None:
        self.config = config
        self.source_override = source_override

    # ------------------------------------------------------------------ sources

    def source_spec(self) -> str:
        if self.source_override:
            return self.source_override
        return self.config.source_spec()

    def describe(self, spec: str | None = None) -> str:
        """A label for a source that is safe to print or commit.

        Database URLs are reduced to backend and database name: even with the
        password masked, hostnames don't belong in committed docs.
        """
        spec = spec or self.source_spec()
        if spec.startswith(("git:", "env:")):
            return spec
        try:
            kind = detect_kind(spec)
        except SourceError:
            return spec
        if kind == "database":
            try:
                url = make_url(spec)
                name = Path(url.database).name if url.database else ""
                return f"{url.get_backend_name()} database" + (f" {name}" if name else "")
            except ArgumentError:
                return describe_source(spec)
        root = str(self.config.root) + "/"
        return spec[len(root) :] if spec.startswith(root) else spec

    def load(self, spec: str | None = None) -> Schema:
        """Read a source spec (default: the configured source) with filters applied."""
        spec = spec or self.source_spec()
        if spec.startswith("env:"):
            schema = load_source(
                self.config.environment(spec[4:]), schemas=self.config.schemas or None
            )
        elif spec.startswith("git:"):
            schema = self._load_git(spec[4:])
        else:
            schema = load_source(
                spec, dialect=self.config.dialect, schemas=self.config.schemas or None
            )
        return self.filter(schema)

    def filter(self, schema: Schema) -> Schema:
        return schema.filtered(
            include=self.config.include,
            exclude=self.config.exclude,
            skip_bookkeeping=self.config.skip_bookkeeping,
        )

    def _load_git(self, rest: str) -> Schema:
        """``REF`` or ``REF:path``: the snapshot at REF, else the file-based source at REF."""
        ref, _, explicit = rest.partition(":")
        if not ref:
            raise SourceError("git source needs a ref, e.g. git:origin/main")
        if explicit:
            return load_git(ref, self.config.root / explicit, self.config.dialect)
        snapshot = self.config.resolve(self.config.output.snapshot)
        if snapshot is not None and path_exists_at(ref, snapshot):
            return load_git(ref, snapshot, self.config.dialect)
        spec = self.source_spec()
        if detect_kind(spec) == "sql":
            path = Path(spec)
            if path_exists_at(ref, path):
                return load_git(ref, path, self.config.dialect)
            return Schema(dialect=self.config.dialect)  # nothing at that ref yet
        raise SourceError(
            f"nothing to compare at {ref}: commit the snapshot "
            f"({self.config.output.snapshot}) so other refs can be diffed"
        )

    def committed_snapshot(self) -> Schema | None:
        path = self.config.resolve(self.config.output.snapshot)
        if path is None or not path.is_file():
            return None
        return self.filter(Schema.load(path))

    # ------------------------------------------------------------------ docs

    def descriptions(self) -> Descriptions:
        try:
            return Descriptions.load(self.config.resolve(self.config.descriptions))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def markdown_options(self, source_label: str | None = None) -> MarkdownOptions:
        output = self.config.output
        return MarkdownOptions(
            title=output.title,
            source=source_label or output.source_label or self.describe(),
            diagram=output.diagram,
            comments_in_diagram=output.comments_in_diagram,
            layout=output.layout,
            groups=self.config.groups,
        )

    def outputs(self, schema: Schema) -> list[OutputFile]:
        """Every configured output, rendered. The snapshot is the bare schema;
        human-facing docs also carry the descriptions file."""
        files = self.config.output.files()
        if not files:
            raise ConfigError("no outputs configured: set at least one path under [output]")
        documented = self.descriptions().apply(schema)
        options = self.markdown_options()
        options.fingerprint = schema.fingerprint()
        rendered = []
        for fmt, path in files.items():
            content = render(schema if fmt == "json" else documented, fmt, options)
            rendered.append(OutputFile(fmt, self.config.resolve(path), content))
        return rendered

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return str(path)
