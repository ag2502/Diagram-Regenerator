"""``diagram-regen ci ...``: the steps the GitHub Action runs, usable in any CI.

* ``ci pr``     diff the branch against its base, comment on the PR, gate on risk
* ``ci update`` regenerate docs after a merge and commit them (optionally push)
* ``ci drift``  compare environments, write the job summary, alert Slack
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from diagram_regenerator import __version__
from diagram_regenerator.config import CONFIG_NAME
from diagram_regenerator.diff import diff_schemas
from diagram_regenerator.github import (
    MAX_COMMENT,
    GitHubClient,
    GitHubError,
    current_pull_request,
    set_output,
    write_step_summary,
)
from diagram_regenerator.model import Schema
from diagram_regenerator.project import Project
from diagram_regenerator.report import COMMENT_MARKER, format_markdown, format_text
from diagram_regenerator.sources import SourceError, detect_kind
from diagram_regenerator.sources.git import ref_exists, repo_root

ACTION_REF = f"ag2502/Diagram-Regenerator@v{__version__}"


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


# --------------------------------------------------------------------------- ci pr


def _base_ref(args: argparse.Namespace, top: Path) -> str:
    """``origin/<base>``, fetching it when a shallow checkout lacks it."""
    if args.base:
        ref = args.base
    else:
        pull = current_pull_request()
        branch = os.environ.get("GITHUB_BASE_REF") or (pull.base_ref if pull else None) or "main"
        ref = f"origin/{branch}"
    if not ref_exists(ref, top) and ref.startswith("origin/"):
        branch = ref.split("/", 1)[1]
        _git(
            [
                "fetch",
                "--no-tags",
                "--depth=50",
                "origin",
                f"{branch}:refs/remotes/origin/{branch}",
            ],
            top,
            check=False,
        )
    return ref


def run_pr(args: argparse.Namespace, project: Project) -> int:
    from diagram_regenerator.cli import EXIT_FAIL, EXIT_OK, _fails

    config = project.config
    top = repo_root(config.root)
    base = _base_ref(args, top)
    new = project.load()
    try:
        old = project.load(f"git:{base}")
        compared = f"`{base}`"
    except SourceError as exc:
        _note(f"warning: no baseline at {base} ({exc}); comparing against an empty schema")
        old, compared = Schema(dialect=config.dialect), "an empty schema"

    diff = diff_schemas(old, new, ignore=config.diff_ignore)
    print(format_text(diff).rstrip())

    stale = [o for o in project.outputs(new) if o.stale]
    footer_lines = []
    if stale and diff.has_changes:
        names = ", ".join(f"`{project.relative(o.path)}`" for o in stale)
        footer_lines.append(
            f"📄 {names} will change: run `diagram-regen generate` on this branch, "
            "or let the update job regenerate them after merge."
        )
    footer_lines.append(
        f"<sub>Compared {compared} → this branch with "
        f"[diagram-regenerator](https://github.com/ag2502/Diagram-Regenerator) {__version__}.</sub>"
    )
    footer = "\n\n".join(footer_lines)
    body = format_markdown(diff, title="🗺️ Database schema changes", footer=footer)
    if len(body) > MAX_COMMENT:
        body = format_markdown(
            diff,
            title="🗺️ Database schema changes",
            diagram=False,
            footer="_The visual diff was too large for a comment; see the job summary._\n\n"
            + footer,
        )

    write_step_summary(
        format_markdown(diff, title="Database schema changes", footer=footer, marker=False)
    )
    set_output("changed", diff.has_changes)
    set_output("breaking", len(diff.breaking))
    set_output("summary", diff.summary())

    if args.comment:
        pull = current_pull_request()
        token = os.environ.get("GITHUB_TOKEN")
        if pull and token:
            try:
                state = GitHubClient(token).upsert_comment(
                    pull, body, COMMENT_MARKER, create=diff.has_changes
                )
                _note(f"PR comment {state}")
            except GitHubError as exc:
                _note(f"warning: {exc}")
        else:
            _note("not a pull_request run with GITHUB_TOKEN set; no PR comment posted")

    level = args.fail_on or config.fail_on
    if _fails(diff, level):
        _note(f"Failing: changes at or above '{level}' severity (diff.fail_on).")
        return EXIT_FAIL
    return EXIT_OK


# --------------------------------------------------------------------------- ci update


def render_message(template: str, summary: str, fingerprint: str) -> str:
    now = datetime.now(timezone.utc)
    return template.format(
        datetime=now.strftime("%Y-%m-%d %H:%M"),
        date=now.strftime("%Y-%m-%d"),
        summary=summary,
        fingerprint=fingerprint,
    ).replace("\\n", "\n")


def run_update(args: argparse.Namespace, project: Project) -> int:
    from diagram_regenerator.cli import EXIT_OK

    previous = project.committed_snapshot()
    schema = project.load()
    diff = diff_schemas(previous or Schema(), schema)
    written = [o for o in project.outputs(schema) if o.write()]
    for output in written:
        print(f"updated {project.relative(output.path)}")
    set_output("changed", bool(written))
    set_output("summary", diff.summary())
    if not written:
        print("Schema docs already up to date.")
        return EXIT_OK

    write_step_summary(format_markdown(diff, title="Schema docs regenerated", marker=False))
    if args.slack:
        from diagram_regenerator.notify import NotifyError, ci_run_url, diff_message, post_slack

        webhook = project.config.webhook() or os.environ.get("SLACK_WEBHOOK_URL")
        if webhook and diff.has_changes:
            try:
                post_slack(webhook, diff_message(diff, "Database schema changed", ci_run_url()))
            except NotifyError as exc:
                _note(f"warning: {exc}")

    if not args.commit:
        return EXIT_OK
    top = repo_root(project.config.root)
    paths = [str(o.path) for o in written]
    _git(["add", "--", *paths], top)
    if _git(["diff", "--cached", "--quiet"], top, check=False).returncode == 0:
        print("Nothing to commit.")
        return EXIT_OK
    message = render_message(args.message, diff.summary(), schema.fingerprint())
    identity = []
    name = args.author_name or os.environ.get("DR_COMMIT_USER_NAME")
    email = args.author_email or os.environ.get("DR_COMMIT_USER_EMAIL")
    if name:
        identity += ["-c", f"user.name={name}"]
    if email:
        identity += ["-c", f"user.email={email}"]
    _git([*identity, "commit", "-q", "-m", message, "--", *paths], top)
    sha = _git(["rev-parse", "--short", "HEAD"], top).stdout.strip()
    print(f"committed {sha}: {message.splitlines()[0]}")
    set_output("commit", sha)
    if args.push:
        _push(top)
        print("pushed")
    return EXIT_OK


def _push(top: Path) -> None:
    """Push to the branch that triggered the run; rebase once if someone pushed meanwhile."""
    branch = (
        os.environ.get("GITHUB_REF_NAME")
        if os.environ.get("GITHUB_REF", "").startswith("refs/heads/")
        else None
    )
    command = ["push", "origin", f"HEAD:refs/heads/{branch}"] if branch else ["push"]
    if _git(command, top, check=False).returncode == 0:
        return
    if branch:
        _git(["pull", "--rebase", "origin", branch], top, check=False)
    pushed = _git(command, top, check=False)
    if pushed.returncode != 0:
        raise SourceError(f"git push failed: {pushed.stderr.strip()}")


# --------------------------------------------------------------------------- ci drift


def run_drift(args: argparse.Namespace, project: Project) -> int:
    from diagram_regenerator.cli import EXIT_FAIL, EXIT_OK, build_drift_report, notify_drift
    from diagram_regenerator.drift import format_drift_markdown, format_drift_text
    from diagram_regenerator.notify import ci_run_url

    report = build_drift_report(project, baseline=args.baseline)
    print(format_drift_text(report).rstrip())
    link = ci_run_url()
    write_step_summary(
        format_drift_markdown(report, footer=f"<sub>[Run]({link})</sub>" if link else None)
    )
    set_output("drift", report.has_drift)
    set_output("summary", report.summary())
    if args.slack:
        notify_drift(project, report, None)
    return EXIT_FAIL if report.has_drift and not args.no_fail else EXIT_OK


# --------------------------------------------------------------------------- init-ci

WORKFLOW_DOCS = """\
# Keeps the database schema diagram current and reviews schema changes in PRs.
# Generated by `diagram-regen init-ci`; see https://github.com/ag2502/Diagram-Regenerator
name: Schema docs

on:
  pull_request:
    paths:
{paths}
  push:
    branches: [{branch}]
    paths:
{paths}
  workflow_dispatch:

permissions:
  contents: write        # commit regenerated docs on {branch}
  pull-requests: write   # comment on PRs

concurrency:
  group: schema-docs-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  schema:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the PR check compares against the base branch
      - uses: {action}
        with:
          mode: ${{{{ github.event_name == 'pull_request' && 'pr' || 'update' }}}}
{extras}"""

WORKFLOW_DRIFT = """\
# Compares live databases with the migrations every weekday and alerts Slack on drift.
# Generated by `diagram-regen init-ci --drift`. Add the secrets referenced below.
name: Schema drift

on:
  schedule:
    - cron: "0 7 * * 1-5"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: {action}
        with:
          mode: drift
          extras: postgres
          slack-webhook: ${{{{ secrets.SLACK_WEBHOOK_URL }}}}
        env:
{env_lines}
"""


def workflow_paths(project: Project) -> list[str]:
    config = project.config
    paths = [CONFIG_NAME]
    if config.source:
        try:
            spec = project.source_spec()
            kind = detect_kind(spec)
        except Exception:
            kind, spec = None, ""
        if kind == "sql":
            rel = project.relative(Path(spec))
            paths.insert(0, rel if rel.endswith(".sql") else rel.rstrip("/") + "/**")
        elif kind == "models":
            module = spec.split(":", 2)[1].split(".")[0]
            paths.insert(0, f"{module}/**")
    if config.descriptions:
        paths.append(config.descriptions)
    return paths


def run_init_ci(args: argparse.Namespace, project: Project) -> int:
    from diagram_regenerator.cli import EXIT_OK, CommandError

    args.action = args.action or ACTION_REF
    root = Path.cwd()
    folder = root / ".github" / "workflows"
    written = []
    paths = "\n".join(f'      - "{p}"' for p in workflow_paths(project))
    extras = ""
    if project.config.source and detect_kind(project.source_spec()) == "database":
        extras = "          extras: postgres\n        env:\n          DATABASE_URL: ${{ secrets.DATABASE_URL }}\n"
    docs = WORKFLOW_DOCS.format(paths=paths, branch=args.branch, action=args.action, extras=extras)
    targets = {folder / "schema-docs.yml": docs}
    if args.drift:
        envs = project.config.environments or {"prod": "${PROD_DATABASE_URL}"}
        env_lines = "\n".join(
            f"          {name.upper()}_DATABASE_URL: ${{{{ secrets.{name.upper()}_DATABASE_URL }}}}"
            for name in envs
        )
        targets[folder / "schema-drift.yml"] = WORKFLOW_DRIFT.format(
            action=args.action, env_lines=env_lines
        )
    for path, content in targets.items():
        if path.exists() and not args.force:
            raise CommandError(f"{project.relative(path)} exists (use --force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(project.relative(path))
    for path in written:
        print(f"Created {path}")
    print(
        "\nCommit the workflow(s). Pull requests that touch the schema now get a comment with "
        f"a visual diff, and merges to {args.branch} regenerate the docs."
    )
    if args.drift:
        print(
            "For drift checks, add repository secrets for each environment URL (read-only "
            "users) and SLACK_WEBHOOK_URL, and list the environments in diagram-regen.toml."
        )
    return EXIT_OK
