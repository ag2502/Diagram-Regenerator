"""Command-line entry point: ``diagram-regen``.

Exit codes: 0 success, 1 a check failed (stale docs, risky diff, drift),
2 the command could not run (bad config, unreadable source).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from diagram_regenerator import __version__
from diagram_regenerator.config import ConfigError, load_config
from diagram_regenerator.diff import IGNORABLE, diff_schemas
from diagram_regenerator.model import Schema
from diagram_regenerator.project import Project
from diagram_regenerator.render import FORMATS, canonical_format, render
from diagram_regenerator.report import format_json, format_markdown, format_text
from diagram_regenerator.sources import SourceError

EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2
FAIL_LEVELS = ("breaking", "warning", "any", "never")


class CommandError(Exception):
    """A user-facing error that stops the command with exit code 2."""


# --------------------------------------------------------------------------- helpers


def _color(args: argparse.Namespace) -> bool:
    return not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _project(args: argparse.Namespace) -> Project:
    return Project(load_config(args.config), source_override=getattr(args, "source", None))


def _write_or_print(text: str, output: str | None) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _fails(diff, level: str) -> bool:
    if level == "never" or not diff.has_changes:
        return False
    if level == "any":
        return True
    return diff.at_least(level)


def _stats(schema: Schema) -> str:
    return (
        f"{len(schema.tables)} tables, {schema.column_count} columns "
        f"(fingerprint {schema.fingerprint()})"
    )


# --------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    from diagram_regenerator.scaffold import detect, render_config, write_config

    root = Path.cwd()
    detected = detect(root)
    if args.source:
        detected.source = args.source
    if args.dialect:
        detected.dialect = args.dialect
    try:
        path = write_config(root, render_config(detected, out_dir=args.output_dir), args.force)
    except FileExistsError as exc:
        raise CommandError(str(exc)) from exc
    print(f"Created {path.name}: {detected.reason}.")
    print("\nNext steps:")
    step = 1
    if not detected.source:
        print(f"  {step}. Set `source` in {path.name}.")
        step += 1
    print(f"  {step}. diagram-regen generate        # write the diagram and snapshot")
    print(f"  {step + 1}. git add {args.output_dir} {path.name} && git commit")
    return EXIT_OK


def cmd_generate(args: argparse.Namespace) -> int:
    project = _project(args)
    previous = project.committed_snapshot()
    schema = project.load()
    print(f"Read {project.describe()}: {_stats(schema)}")
    if previous is not None:
        diff = diff_schemas(previous, schema)
        if diff.has_changes:
            print(f"Changes since the last snapshot: {diff.summary()}")
    for output in project.outputs(schema):
        if args.dry_run:
            state = "would update" if output.stale else "unchanged"
        else:
            state = "updated" if output.write() else "unchanged"
        print(f"  {state:<12} {project.relative(output.path)}")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    project = _project(args)
    schema = project.load()
    stale = [o for o in project.outputs(schema) if o.stale]
    if not stale:
        print(f"Schema docs are up to date: {_stats(schema)}")
        return EXIT_OK
    for output in stale:
        state = "missing" if output.current is None else "out of date"
        print(f"{project.relative(output.path)} is {state}")
    previous = project.committed_snapshot()
    if previous is not None:
        diff = diff_schemas(previous, schema, ignore=project.config.diff_ignore)
        if diff.has_changes:
            print("\n" + format_text(diff, color=_color(args)).rstrip())
    print("\nRun `diagram-regen generate` and commit the result.")
    return EXIT_FAIL


def cmd_diff(args: argparse.Namespace) -> int:
    project = _project(args)
    config = project.config
    if args.old:
        old = project.load(args.old)
        old_label = project.describe(args.old)
    else:
        old = project.committed_snapshot()
        old_label = config.output.snapshot or "snapshot"
        if old is None:
            print(
                f"note: no snapshot at {old_label} yet; comparing against an empty schema",
                file=sys.stderr,
            )
            old = Schema()
    new = project.load(args.new) if args.new else project.load()
    new_label = project.describe(args.new) if args.new else project.describe()

    ignore = sorted(set(config.diff_ignore) | set(args.ignore or []))
    diff = diff_schemas(old, new, ignore=ignore)
    if args.format == "json":
        text = format_json(diff)
    elif args.format == "markdown":
        title = args.title or "Schema changes"
        text = format_markdown(
            diff,
            title=title,
            diagram=not args.no_diagram,
            footer=f"<sub>Compared `{old_label}` → `{new_label}` by diagram-regenerator.</sub>",
            marker=False,
        )
    else:
        text = format_text(diff, color=_color(args) and not args.output)
    _write_or_print(text, args.output)
    return EXIT_FAIL if _fails(diff, args.fail_on or config.fail_on) else EXIT_OK


def cmd_drift(args: argparse.Namespace) -> int:
    from diagram_regenerator.drift import (
        detect_drift,
        format_drift_json,
        format_drift_markdown,
        format_drift_text,
    )
    from diagram_regenerator.notify import NotifyError, ci_run_url, drift_message, post_slack

    project = _project(args)
    config = project.config
    if args.env:
        specs = {}
        for item in args.env:
            name, _, spec = item.partition("=")
            if not name or not spec:
                raise CommandError(f"--env expects NAME=SOURCE, got {item!r}")
            specs[name] = spec
    elif config.environments:
        specs = {name: f"env:{name}" for name in config.environments}
    else:
        raise CommandError(
            "no environments to check: add an [environments] table to the config "
            "or pass --env NAME=URL (repeatable)"
        )

    baseline = args.baseline or config.drift_baseline
    if baseline == "source":
        baseline_schema, baseline_label = project.load(), project.describe()
    elif baseline in specs:
        baseline_schema, baseline_label = project.load(specs.pop(baseline)), baseline
    else:
        baseline_schema, baseline_label = project.load(baseline), project.describe(baseline)
    if not specs:
        raise CommandError("nothing to compare: the baseline was the only environment")

    loaders = {name: (lambda spec=spec: project.load(spec)) for name, spec in specs.items()}
    ignore = sorted(set(config.drift_ignore) | set(args.ignore or []))
    report = detect_drift(baseline_label, baseline_schema, loaders, ignore=ignore)

    if args.format == "json":
        text = format_drift_json(report)
    elif args.format == "markdown":
        link = ci_run_url()
        text = format_drift_markdown(report, footer=f"<sub>[Run]({link})</sub>" if link else None)
    else:
        text = format_drift_text(report)
    _write_or_print(text, args.output)

    if args.slack or args.slack_webhook:
        webhook = args.slack_webhook or config.webhook() or os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook:
            raise CommandError(
                "--slack needs a webhook: set notify.slack_webhook, SLACK_WEBHOOK_URL "
                "or --slack-webhook"
            )
        if report.has_drift or args.notify_always:
            try:
                post_slack(webhook, drift_message(report, link=ci_run_url()))
                print("notified Slack", file=sys.stderr)
            except NotifyError as exc:
                print(f"warning: {exc}", file=sys.stderr)
    return EXIT_FAIL if report.has_drift and not args.no_fail else EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    project = _project(args)
    schema = project.load(args.target) if args.target else project.load()
    fmt = canonical_format(args.format)
    documented = schema if fmt == "json" else project.descriptions().apply(schema)
    options = project.markdown_options(project.describe(args.target) if args.target else None)
    _write_or_print(render(documented, fmt, options), args.output)
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="config file (default: nearest diagram-regen.toml)")
    common.add_argument("-q", "--quiet", action="store_true", help="hide warnings")
    common.add_argument("-v", "--verbose", action="store_true", help="show debug detail")
    common.add_argument("--no-color", action="store_true", help="plain output")

    with_source = argparse.ArgumentParser(add_help=False)
    with_source.add_argument(
        "-s", "--source", help="schema source, overriding the config (URL, path or python:mod:Base)"
    )

    parser = argparse.ArgumentParser(
        prog="diagram-regen",
        description="Always-current database schema diagrams, PR schema diffs and drift alerts.",
        epilog="Sources: a database URL, a .sql file, a migrations directory, a .json "
        "snapshot, python:module:Base, env:NAME (from [environments]) or git:REF[:path].",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = commands.add_parser(
        "init", parents=[common], help="create diagram-regen.toml for this project"
    )
    init.add_argument("-s", "--source", help="schema source (default: auto-detect)")
    init.add_argument("--dialect", help="SQL dialect (default: auto-detect)")
    init.add_argument("--output-dir", default="docs/schema", help="where docs are written")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.set_defaults(handler=cmd_init)

    generate = commands.add_parser(
        "generate",
        parents=[common, with_source],
        help="regenerate the diagram, docs and snapshot",
    )
    generate.add_argument("--dry-run", action="store_true", help="report without writing")
    generate.set_defaults(handler=cmd_generate)

    check = commands.add_parser(
        "check",
        parents=[common, with_source],
        help="fail if the committed docs are out of date (for CI and pre-commit)",
    )
    check.set_defaults(handler=cmd_check)

    diff = commands.add_parser(
        "diff",
        parents=[common, with_source],
        help="compare two schemas and rate each change",
        description="Compare OLD (default: the committed snapshot) with NEW (default: the "
        "configured source). Examples: `diagram-regen diff git:origin/main`, "
        "`diagram-regen diff env:prod env:staging`.",
    )
    diff.add_argument("old", nargs="?", help="the before schema")
    diff.add_argument("new", nargs="?", help="the after schema")
    diff.add_argument("-f", "--format", choices=("text", "markdown", "json"), default="text")
    diff.add_argument("-o", "--output", help="write to a file instead of stdout")
    diff.add_argument(
        "--fail-on", choices=FAIL_LEVELS, help="exit 1 at this severity (default: config)"
    )
    diff.add_argument("--ignore", action="append", choices=IGNORABLE, help="skip a kind of change")
    diff.add_argument("--title", help="heading for Markdown output")
    diff.add_argument("--no-diagram", action="store_true", help="omit the Mermaid visual diff")
    diff.set_defaults(handler=cmd_diff)

    drift = commands.add_parser(
        "drift",
        parents=[common, with_source],
        help="compare live environments with the baseline and alert on drift",
        description="Diff each environment against a baseline (default: the configured "
        "source, i.e. what the code expects). Exit 1 when any environment differs or "
        "can't be read.",
    )
    drift.add_argument(
        "--env",
        action="append",
        metavar="NAME=SOURCE",
        help="environment to check (repeatable; default: [environments] in the config)",
    )
    drift.add_argument(
        "--baseline", help="'source', an environment name or any source (default: config)"
    )
    drift.add_argument("-f", "--format", choices=("text", "markdown", "json"), default="text")
    drift.add_argument("-o", "--output", help="write to a file instead of stdout")
    drift.add_argument("--ignore", action="append", choices=IGNORABLE, help="skip a kind of change")
    drift.add_argument("--slack", action="store_true", help="post to Slack when drift is found")
    drift.add_argument("--slack-webhook", help="Slack incoming-webhook URL (implies --slack)")
    drift.add_argument(
        "--notify-always", action="store_true", help="post to Slack even when nothing drifted"
    )
    drift.add_argument("--no-fail", action="store_true", help="exit 0 even when drift is found")
    drift.set_defaults(handler=cmd_drift)

    render_cmd = commands.add_parser(
        "render",
        parents=[common, with_source],
        help="render any source in one format, without writing project files",
    )
    render_cmd.add_argument("target", nargs="?", help="source to render (default: configured)")
    render_cmd.add_argument(
        "-f", "--format", default="mermaid", help=f"one of {', '.join(FORMATS)} (default: mermaid)"
    )
    render_cmd.add_argument("-o", "--output", help="write to a file instead of stdout")
    render_cmd.set_defaults(handler=cmd_render)
    return parser


def _setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root = logging.getLogger("diagram_regenerator")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_OK
    _setup_logging(args)
    try:
        return args.handler(args)
    except (CommandError, ConfigError, SourceError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
