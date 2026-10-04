"""Copy every ```mermaid block from Markdown files into .mmd files.

CI renders them with the official Mermaid CLI, so a diagram GitHub can't
draw fails the build instead of showing up as an error box in a README.

    python scripts/extract_mermaid.py OUT_DIR FILE_OR_DIR...
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BLOCK = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for target in argv[1:]:
        path = Path(target)
        files = sorted(path.rglob("*.md")) if path.is_dir() else [path]
        for markdown in files:
            for index, block in enumerate(BLOCK.findall(markdown.read_text(encoding="utf-8"))):
                name = "_".join(markdown.with_suffix("").parts[-3:]) + f"_{index}.mmd"
                (out / name).write_text(block, encoding="utf-8")
                count += 1
    print(f"extracted {count} diagrams into {out}")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
