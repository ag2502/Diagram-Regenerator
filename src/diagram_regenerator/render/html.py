"""A single-file HTML schema browser: searchable, zoomable, per-table focus view.

Mermaid is loaded from a CDN; everything else (styles, data, script) is inline,
so the file can be opened locally, attached to a ticket or served as a CI artifact.
"""

from __future__ import annotations

import html
import json

from diagram_regenerator.model import DRAFT_PREFIX, Schema, Table
from diagram_regenerator.render.markdown import overview_columns
from diagram_regenerator.render.mermaid import MermaidOptions, neighbours, render_mermaid

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _description(text: str | None) -> str:
    if not text:
        return ""
    if text.lower().startswith(DRAFT_PREFIX):
        return f'<span class="draft">draft</span> {_e(text[len(DRAFT_PREFIX) :].strip())}'
    return _e(text)


def _table_card(schema: Schema, table: Table) -> str:
    rows = []
    for column in table.columns:
        keys = []
        if column.name in table.primary_key:
            keys.append('<span class="key pk">PK</span>')
        fk = table.foreign_key_for(column.name)
        if fk:
            target = _e(fk.ref_table)
            keys.append(
                f'<span class="key fk">FK</span> <a href="#t-{target}" data-focus="{target}">{target}</a>'
            )
        if column.name not in table.primary_key and table.is_unique([column.name]):
            keys.append('<span class="key uk">UK</span>')
        default = (
            "auto-increment"
            if column.autoincrement
            else (f"<code>{_e(column.default)}</code>" if column.default is not None else "")
        )
        rows.append(
            "<tr>"
            f"<td><code>{_e(column.name)}</code></td>"
            f"<td><code>{_e(column.type)}</code></td>"
            f"<td>{'yes' if column.nullable else ''}</td>"
            f"<td>{default}</td>"
            f"<td>{' '.join(keys)}</td>"
            f"<td>{_description(column.comment)}</td>"
            "</tr>"
        )
    extras = []
    if table.indexes:
        items = ", ".join(
            ("unique " if ix.unique else "")
            + (f"<code>{_e(ix.name)}</code> " if ix.name else "")
            + "("
            + ", ".join(_e(c) for c in ix.columns)
            + ")"
            for ix in table.indexes
        )
        extras.append(f"<p><strong>Indexes:</strong> {items}</p>")
    referenced = schema.referencing(table.key)
    if referenced:
        items = ", ".join(
            f'<a href="#t-{_e(o.key)}" data-focus="{_e(o.key)}">{_e(o.key)}</a>.{_e(", ".join(fk.columns))}'
            for o, fk in referenced
        )
        extras.append(f"<p><strong>Referenced by:</strong> {items}</p>")
    comment = f"<p>{_description(table.comment)}</p>" if table.comment else ""
    return (
        f'<section class="card" id="t-{_e(table.key)}" data-table="{_e(table.key)}">'
        f'<h3><span>{_e(table.key)}</span> <button type="button" data-focus="{_e(table.key)}">'
        "Focus</button></h3>"
        f"{comment}"
        '<div class="scroll"><table><thead><tr><th>Column</th><th>Type</th><th>Nullable</th>'
        "<th>Default</th><th>Keys</th><th>Description</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        f"{''.join(extras)}</section>"
    )


def render_html(schema: Schema, title: str = "Database schema", source: str | None = None) -> str:
    tables = schema.sorted_tables()
    overview = render_mermaid(
        schema, MermaidOptions(columns=overview_columns(len(tables), "auto"), comments=False)
    )
    focus = {
        t.key: render_mermaid(
            schema,
            MermaidOptions(columns="all", comments=False),
            tables=[t.key],
            stubs=neighbours(schema, [t.key]),
        )
        for t in tables
    }
    data = json.dumps({"overview": overview, "focus": focus}).replace("</", "<\\/")
    items = "".join(
        f'<li><a href="#t-{_e(t.key)}" data-focus="{_e(t.key)}">{_e(t.key)}</a>'
        f"<small>{len(t.columns)}</small></li>"
        for t in tables
    )
    cards = "".join(_table_card(schema, t) for t in tables)
    subtitle = f"{len(tables)} tables · {schema.column_count} columns"
    if source:
        subtitle += f" · {_e(source)}"
    subtitle += f" · fingerprint {schema.fingerprint()}"
    return _PAGE.format(
        title=_e(title),
        subtitle=subtitle,
        items=items,
        cards=cards,
        data=data,
        cdn=MERMAID_CDN,
    )


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1d1f23; --muted: #626872; --line: #e2e2de;
  --accent: #2557d6; --pk: #8a5a00; --fk: #2557d6; --uk: #6b3fb3; --draft: #a34d00;
  color-scheme: light;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #15171b; --panel: #1d2026; --ink: #e8e9ec; --muted: #9aa0aa; --line: #2e323a;
    --accent: #7aa2ff; --pk: #e2b45c; --fk: #7aa2ff; --uk: #b99af0; --draft: #f0a35c;
    color-scheme: dark;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
header {{ padding: 16px 20px; border-bottom: 1px solid var(--line); background: var(--panel); }}
header h1 {{ margin: 0; font-size: 18px; }}
header p {{ margin: 2px 0 0; color: var(--muted); font-size: 13px; }}
.layout {{ display: grid; grid-template-columns: 260px minmax(0, 1fr); min-height: calc(100vh - 66px); }}
aside {{ border-right: 1px solid var(--line); background: var(--panel); padding: 12px;
  position: sticky; top: 0; height: 100vh; overflow: auto; }}
aside input {{ width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--ink); font: inherit; }}
aside ul {{ list-style: none; margin: 10px 0 0; padding: 0; }}
aside li {{ display: flex; justify-content: space-between; gap: 8px; padding: 3px 4px; border-radius: 4px; }}
aside li.active {{ background: color-mix(in srgb, var(--accent) 14%, transparent); }}
aside a {{ color: var(--ink); text-decoration: none; overflow-wrap: anywhere; }}
aside small {{ color: var(--muted); }}
main {{ padding: 16px 20px; min-width: 0; }}
.toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }}
.toolbar strong {{ margin-right: auto; }}
button {{ font: inherit; padding: 4px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--panel); color: var(--ink); cursor: pointer; }}
button:hover {{ border-color: var(--accent); }}
#viewport {{ height: 60vh; min-height: 320px; overflow: hidden; border: 1px solid var(--line);
  border-radius: 8px; background: var(--panel); cursor: grab; position: relative; }}
#viewport.dragging {{ cursor: grabbing; }}
#canvas {{ transform-origin: 0 0; position: absolute; top: 0; left: 0; }}
#status {{ color: var(--muted); padding: 16px; }}
.card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 16px; margin: 16px 0; scroll-margin-top: 12px; }}
.card.hidden, aside li.hidden {{ display: none; }}
.card h3 {{ margin: 0 0 8px; font-size: 16px; display: flex; justify-content: space-between; gap: 8px; }}
.scroll {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; }}
code {{ font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }}
a {{ color: var(--accent); }}
.key {{ font: 600 11px ui-monospace, monospace; padding: 0 4px; border-radius: 3px;
  border: 1px solid currentColor; }}
.pk {{ color: var(--pk); }} .fk {{ color: var(--fk); }} .uk {{ color: var(--uk); }}
.draft {{ color: var(--draft); font-size: 11px; font-weight: 600; text-transform: uppercase; }}
@media (max-width: 760px) {{
  .layout {{ grid-template-columns: 1fr; }}
  aside {{ position: static; height: auto; max-height: 40vh; border-right: 0; border-bottom: 1px solid var(--line); }}
  main {{ padding: 12px 16px; }}
}}
</style>
</head>
<body>
<header><h1>{title}</h1><p>{subtitle}</p></header>
<div class="layout">
<aside>
  <input id="filter" type="search" placeholder="Filter tables…" aria-label="Filter tables">
  <ul id="list">{items}</ul>
</aside>
<main>
  <div class="toolbar">
    <strong id="view-label">All tables</strong>
    <button type="button" id="show-all">Show all</button>
    <button type="button" id="zoom-out" aria-label="Zoom out">−</button>
    <button type="button" id="zoom-in" aria-label="Zoom in">+</button>
    <button type="button" id="fit">Fit</button>
  </div>
  <div id="viewport"><div id="canvas"><p id="status">Rendering diagram…</p></div></div>
  {cards}
</main>
</div>
<script type="application/json" id="diagrams">{data}</script>
<script type="module">
import mermaid from "{cdn}";
const diagrams = JSON.parse(document.getElementById("diagrams").textContent);
const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
mermaid.initialize({{ startOnLoad: false, theme: dark ? "dark" : "default", maxTextSize: 900000,
  er: {{ useMaxWidth: false }}, securityLevel: "strict" }});
const viewport = document.getElementById("viewport");
const canvas = document.getElementById("canvas");
let scale = 1, x = 0, y = 0, counter = 0;
const apply = () => {{ canvas.style.transform = `translate(${{x}}px, ${{y}}px) scale(${{scale}})`; }};
function fit() {{
  const svg = canvas.querySelector("svg");
  if (!svg) return;
  const box = svg.getBoundingClientRect();
  const w = box.width / scale, h = box.height / scale;
  scale = Math.min(viewport.clientWidth / w, viewport.clientHeight / h, 1.5) * 0.95;
  x = (viewport.clientWidth - w * scale) / 2; y = (viewport.clientHeight - h * scale) / 2;
  apply();
}}
async function show(text, label) {{
  document.getElementById("view-label").textContent = label;
  try {{
    const {{ svg }} = await mermaid.render(`er${{counter++}}`, text);
    canvas.innerHTML = svg;
    scale = 1; x = 0; y = 0; apply(); fit();
  }} catch (error) {{
    canvas.innerHTML = `<p id="status">Could not render the diagram: ${{error.message}}</p>`;
  }}
}}
function focusTable(key) {{
  if (!diagrams.focus[key]) return;
  document.querySelectorAll("#list li").forEach(li =>
    li.classList.toggle("active", li.querySelector("a").dataset.focus === key));
  show(diagrams.focus[key], `${{key}} and its neighbours`);
}}
document.addEventListener("click", event => {{
  const target = event.target.closest("[data-focus]");
  if (target) focusTable(target.dataset.focus);
}});
document.getElementById("show-all").onclick = () => {{
  document.querySelectorAll("#list li").forEach(li => li.classList.remove("active"));
  show(diagrams.overview, "All tables");
}};
document.getElementById("zoom-in").onclick = () => {{ scale *= 1.2; apply(); }};
document.getElementById("zoom-out").onclick = () => {{ scale /= 1.2; apply(); }};
document.getElementById("fit").onclick = fit;
viewport.addEventListener("wheel", event => {{
  event.preventDefault();
  const rect = viewport.getBoundingClientRect();
  const px = event.clientX - rect.left, py = event.clientY - rect.top;
  const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1;
  x = px - (px - x) * factor; y = py - (py - y) * factor; scale *= factor; apply();
}}, {{ passive: false }});
let drag = null;
viewport.addEventListener("pointerdown", event => {{
  drag = {{ px: event.clientX, py: event.clientY, x, y }};
  viewport.classList.add("dragging"); viewport.setPointerCapture(event.pointerId);
}});
viewport.addEventListener("pointermove", event => {{
  if (!drag) return;
  x = drag.x + event.clientX - drag.px; y = drag.y + event.clientY - drag.py; apply();
}});
viewport.addEventListener("pointerup", () => {{ drag = null; viewport.classList.remove("dragging"); }});
document.getElementById("filter").addEventListener("input", event => {{
  const needle = event.target.value.trim().toLowerCase();
  document.querySelectorAll("#list li").forEach(li =>
    li.classList.toggle("hidden", !li.textContent.toLowerCase().includes(needle)));
  document.querySelectorAll(".card").forEach(card =>
    card.classList.toggle("hidden", !card.dataset.table.toLowerCase().includes(needle)));
}});
show(diagrams.overview, "All tables");
</script>
</body>
</html>
"""
