"""Schemas as they were at a git ref, without checking anything out.

``git:origin/main`` loads the committed snapshot at that ref when there is
one, otherwise replays the SQL migrations as they were at that ref. This is
what a PR check compares against.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from diagram_regenerator.model import Schema
from diagram_regenerator.sources.sql import migration_order, replay
from diagram_regenerator.sources.sqla import SourceError


def _git(args: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise SourceError("git is not installed") from exc
    if completed.returncode != 0:
        raise SourceError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def repo_root(start: Path) -> Path:
    start = start if start.is_dir() else start.parent
    while not start.exists():
        start = start.parent
    return Path(_git(["rev-parse", "--show-toplevel"], start).strip()).resolve()


def _in_repo(path: Path, top: Path) -> str:
    try:
        return path.resolve().relative_to(top).as_posix()
    except ValueError as exc:
        raise SourceError(f"{path} is outside the git repository at {top}") from exc


def ref_exists(ref: str, top: Path) -> bool:
    try:
        _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], top)
        return True
    except SourceError:
        return False


def path_exists_at(ref: str, path: Path) -> bool:
    """Whether ``path`` (a file or directory in the working tree) exists at ``ref``."""
    try:
        top = repo_root(path)
        _git(["cat-file", "-e", f"{ref}:{_in_repo(path, top)}"], top)
        return True
    except SourceError:
        return False


def load_git(ref: str, path: Path, dialect: str | None = None) -> Schema:
    """Load ``path`` (snapshot, ``.sql`` file or migrations dir) as of ``ref``."""
    top = repo_root(path)
    if not ref_exists(ref, top):
        raise SourceError(
            f"git ref {ref!r} not found (in CI, fetch history first: "
            "actions/checkout with fetch-depth: 0)"
        )
    rel = _in_repo(path, top)
    if path.suffix == ".json":
        if not path_exists_at(ref, path):
            raise SourceError(f"{rel} does not exist at {ref}")
        try:
            return Schema.from_json(_git(["show", f"{ref}:{rel}"], top))
        except ValueError as exc:
            raise SourceError(f"{rel} at {ref}: {exc}") from exc

    listing = _git(["ls-tree", "-r", "-z", "--name-only", "--full-name", ref, "--", rel], top)
    listing = listing.split("\0")
    sql_files = [name for name in listing if name.endswith(".sql")]
    if not sql_files:
        raise SourceError(f"no .sql files under {rel} at {ref}")
    prefix = "" if path.suffix == ".sql" else rel.rstrip("/") + "/"
    by_relative = {name[len(prefix) :]: name for name in sql_files if name.startswith(prefix)}
    ordered = [by_relative[name] for name in migration_order(by_relative, dialect)]
    files = ((name, _git(["show", f"{ref}:{name}"], top)) for name in ordered)
    return replay(files, dialect).schema
