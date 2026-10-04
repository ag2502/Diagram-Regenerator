"""Turn a :class:`~diagram_regenerator.model.Schema` into documents."""

from __future__ import annotations

from diagram_regenerator.model import Schema
from diagram_regenerator.render.dbml import render_dbml
from diagram_regenerator.render.html import render_html
from diagram_regenerator.render.markdown import MarkdownOptions, render_markdown
from diagram_regenerator.render.mermaid import MermaidOptions, render_mermaid

# format name -> default file extension
FORMATS = {
    "markdown": ".md",
    "mermaid": ".mmd",
    "dbml": ".dbml",
    "html": ".html",
    "json": ".json",
}
_ALIASES = {"md": "markdown", "mmd": "mermaid", "snapshot": "json"}

__all__ = [
    "FORMATS",
    "MarkdownOptions",
    "MermaidOptions",
    "format_for_path",
    "render",
    "render_dbml",
    "render_html",
    "render_markdown",
    "render_mermaid",
]


def canonical_format(name: str) -> str:
    key = _ALIASES.get(name.lower(), name.lower())
    if key not in FORMATS:
        raise ValueError(f"unknown format {name!r}; choose from {', '.join(FORMATS)}")
    return key


def format_for_path(path: str) -> str:
    """Guess the output format from a file name (``docs/SCHEMA.md`` -> markdown)."""
    lowered = path.lower()
    for fmt, extension in FORMATS.items():
        if lowered.endswith(extension):
            return fmt
    if lowered.endswith(".markdown"):
        return "markdown"
    raise ValueError(f"can't tell the format of {path!r} from its extension")


def render(schema: Schema, fmt: str, markdown: MarkdownOptions | None = None) -> str:
    fmt = canonical_format(fmt)
    markdown = markdown or MarkdownOptions()
    if fmt == "markdown":
        return render_markdown(schema, markdown)
    if fmt == "mermaid":
        return render_mermaid(
            schema,
            MermaidOptions(
                columns="all" if markdown.diagram == "auto" else markdown.diagram,
                comments=markdown.comments_in_diagram,
                layout=markdown.layout,
            ),
        )
    if fmt == "dbml":
        return render_dbml(schema)
    if fmt == "html":
        return render_html(schema, title=markdown.title, source=markdown.source)
    return schema.to_json()
