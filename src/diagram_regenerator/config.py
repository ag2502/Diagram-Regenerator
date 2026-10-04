"""Project configuration: ``diagram-regen.toml`` or ``[tool.diagram-regen]`` in pyproject.

Every key is optional. Paths are relative to the config file, and string
values may reference environment variables as ``${NAME}`` or
``${NAME:-fallback}`` so credentials never live in the repository. Variables
are expanded only when a value is used: a missing ``PROD_DATABASE_URL`` does
not stop ``generate`` from working.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_NAME = "diagram-regen.toml"
DEFAULT_DIR = "docs/schema"


class ConfigError(ValueError):
    """The configuration is invalid; the message says how to fix it."""


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str, what: str = "value") -> str:
    """Expand ``${NAME}`` / ``${NAME:-fallback}``; unset variables without a fallback fail."""

    def substitute(match: re.Match) -> str:
        name, fallback = match.group(1), match.group(2)
        if name in os.environ and os.environ[name] != "":
            return os.environ[name]
        if fallback is not None:
            return fallback
        raise ConfigError(f"{what} uses ${{{name}}}, but that environment variable is not set")

    return _VAR.sub(substitute, value)


@dataclass
class OutputConfig:
    snapshot: str | None = f"{DEFAULT_DIR}/schema.json"
    markdown: str | None = f"{DEFAULT_DIR}/README.md"
    mermaid: str | None = None
    dbml: str | None = None
    html: str | None = None
    title: str = "Database schema"
    diagram: str = "auto"
    layout: str | None = None
    comments_in_diagram: bool = True

    def files(self) -> dict[str, str]:
        """format -> path for every output that is switched on."""
        pairs = {
            "json": self.snapshot,
            "markdown": self.markdown,
            "mermaid": self.mermaid,
            "dbml": self.dbml,
            "html": self.html,
        }
        return {fmt: path for fmt, path in pairs.items() if path}


@dataclass
class Config:
    root: Path = field(default_factory=Path.cwd)
    path: Path | None = None
    source: str | None = None
    dialect: str | None = None
    schemas: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    skip_bookkeeping: bool = True
    output: OutputConfig = field(default_factory=OutputConfig)
    groups: dict[str, list[str]] = field(default_factory=dict)
    descriptions: str | None = f"{DEFAULT_DIR}/descriptions.yml"
    fail_on: str = "breaking"
    diff_ignore: list[str] = field(default_factory=list)
    environments: dict[str, str] = field(default_factory=dict)
    drift_baseline: str = "source"
    drift_ignore: list[str] = field(default_factory=lambda: ["comments", "column_order"])
    slack_webhook: str | None = None
    llm_model: str | None = None

    # ------------------------------------------------------------------ paths

    def resolve(self, path: str | None) -> Path | None:
        """A config-relative path as an absolute ``Path``."""
        if not path:
            return None
        candidate = Path(expand(path, "a path"))
        return candidate if candidate.is_absolute() else (self.root / candidate)

    def source_spec(self) -> str:
        """The configured source, with variables expanded and paths made absolute."""
        if not self.source:
            raise ConfigError(
                "no schema source configured: pass --source or set `source` in "
                f"{CONFIG_NAME} (run `diagram-regen init` to create one)"
            )
        spec = expand(self.source, "`source`")
        return resolve_spec(spec, self.root)

    def environment(self, name: str) -> str:
        if name not in self.environments:
            known = ", ".join(sorted(self.environments)) or "none configured"
            raise ConfigError(f"unknown environment {name!r} (known: {known})")
        return resolve_spec(expand(self.environments[name], f"environment {name!r}"), self.root)

    def webhook(self) -> str | None:
        if not self.slack_webhook:
            return None
        return expand(self.slack_webhook, "`notify.slack_webhook`") or None


def resolve_spec(spec: str, root: Path) -> str:
    """Make file-system specs absolute; leave URLs and module targets alone."""
    if "://" in spec or spec.startswith(("python:", "models:", "git:", "sqlite:", "env:")):
        return spec
    path = Path(spec)
    return str(path if path.is_absolute() else root / path)


# --------------------------------------------------------------------------- loading


def find_config(start: Path | None = None) -> Path | None:
    """Nearest ``diagram-regen.toml``, else a pyproject with ``[tool.diagram-regen]``."""
    start = (start or Path.cwd()).resolve()
    for directory in [start, *start.parents]:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file() and "diagram-regen" in _read_toml(pyproject).get("tool", {}):
            return pyproject
        if (directory / ".git").exists():
            break
    return None


def load_config(path: str | Path | None = None) -> Config:
    """Load ``path`` (or the nearest config); defaults when there is none."""
    if path is None:
        found = find_config()
        if found is None:
            return Config(root=Path.cwd())
        path = found
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"config file {path} not found")
    data = _read_toml(path)
    if path.name == "pyproject.toml":
        data = data.get("tool", {}).get("diagram-regen", {})
    return parse_config(data, root=path.parent, path=path)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML ({exc})") from exc


_TOP_LEVEL = {
    "source",
    "dialect",
    "schemas",
    "include",
    "exclude",
    "skip_bookkeeping",
    "output",
    "groups",
    "descriptions",
    "diff",
    "environments",
    "drift",
    "notify",
    "llm",
}


def parse_config(data: dict[str, Any], root: Path, path: Path | None = None) -> Config:
    unknown = set(data) - _TOP_LEVEL
    if unknown:
        raise ConfigError(
            f"unknown setting(s) {', '.join(sorted(unknown))}; expected {', '.join(sorted(_TOP_LEVEL))}"
        )
    config = Config(root=root, path=path)

    source = data.get("source")
    if isinstance(source, dict):  # [source] table form
        config.dialect = source.get("dialect")
        config.schemas = _strings(source.get("schemas", []), "source.schemas")
        source = source.get("path") or source.get("url")
    if source is not None and not isinstance(source, str):
        raise ConfigError("`source` must be a string")
    config.source = source
    config.dialect = data.get("dialect", config.dialect)
    if config.dialect:
        from diagram_regenerator.dialects import canonical_dialect

        try:
            config.dialect = canonical_dialect(config.dialect)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    config.schemas = _strings(data.get("schemas", config.schemas), "schemas")
    config.include = _strings(data.get("include", []), "include")
    config.exclude = _strings(data.get("exclude", []), "exclude")
    config.skip_bookkeeping = bool(data.get("skip_bookkeeping", True))

    output = data.get("output", {})
    if not isinstance(output, dict):
        raise ConfigError("[output] must be a table")
    out = OutputConfig()
    for name in ("snapshot", "markdown", "mermaid", "dbml", "html"):
        if name in output:
            value = output[name]
            setattr(out, name, value or None)
    out.title = output.get("title", out.title)
    out.diagram = output.get("diagram", out.diagram)
    if out.diagram not in {"auto", "all", "keys", "none"}:
        raise ConfigError("output.diagram must be auto, all, keys or none")
    out.layout = output.get("layout") or None
    out.comments_in_diagram = bool(output.get("comments_in_diagram", True))
    config.output = out

    groups = data.get("groups", {})
    config.groups = {str(k): _strings(v, f"groups.{k}") for k, v in groups.items()}
    if "descriptions" in data:
        config.descriptions = data["descriptions"] or None

    diff = data.get("diff", {})
    config.fail_on = diff.get("fail_on", config.fail_on)
    if config.fail_on not in {"breaking", "warning", "any", "never"}:
        raise ConfigError("diff.fail_on must be breaking, warning, any or never")
    config.diff_ignore = _strings(diff.get("ignore", []), "diff.ignore")

    environments = data.get("environments", {})
    config.environments = {str(k): str(v) for k, v in environments.items()}
    drift = data.get("drift", {})
    config.drift_baseline = drift.get("baseline", config.drift_baseline)
    config.drift_ignore = _strings(drift.get("ignore", config.drift_ignore), "drift.ignore")
    if config.drift_baseline != "source" and config.drift_baseline not in config.environments:
        raise ConfigError(
            f"drift.baseline {config.drift_baseline!r} is neither 'source' nor an environment"
        )
    config.slack_webhook = data.get("notify", {}).get("slack_webhook") or None
    config.llm_model = data.get("llm", {}).get("model") or None
    return config


def _strings(value: Any, what: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"`{what}` must be a list of strings")
    return list(value)
