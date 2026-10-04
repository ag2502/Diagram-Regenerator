"""Slack notifications via incoming webhooks (no SDK, no extra dependencies)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from diagram_regenerator.diff import SchemaDiff
from diagram_regenerator.drift import DriftReport
from diagram_regenerator.report import ICONS

MAX_LINES = 12


class NotifyError(RuntimeError):
    pass


def ci_run_url() -> str | None:
    """Link to the current GitHub Actions run, when running in one."""
    server, repo, run = (
        os.environ.get("GITHUB_SERVER_URL"),
        os.environ.get("GITHUB_REPOSITORY"),
        os.environ.get("GITHUB_RUN_ID"),
    )
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def post_slack(webhook_url: str, payload: dict, timeout: float = 10.0) -> None:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            if response.status >= 300 or body.strip() not in {"ok", ""}:
                raise NotifyError(f"Slack answered {response.status}: {body[:200]}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise NotifyError(f"Slack rejected the message ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError(f"could not reach Slack: {exc.reason}") from exc


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(link: str | None, label: str) -> list[dict]:
    if not link:
        return []
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{link}|{label}>"}]}]


def drift_message(report: DriftReport, link: str | None = None, project: str | None = None) -> dict:
    title = "Schema drift detected" if report.has_drift else "No schema drift"
    if project:
        title += f" in {project}"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        _mrkdwn(report.summary()),
    ]
    for name in report.environments:
        if name in report.errors:
            blocks.append(_mrkdwn(f"*{name}* ❓ could not be read: `{report.errors[name][:300]}`"))
            continue
        diff = report.diffs.get(name)
        if not diff or not diff.has_changes:
            continue
        lines = [
            f"{ICONS[c.severity]} {c.summary}"
            + (f": {c.after}" if c.kind == "column_type_changed" else "")
            for c in diff.changes[:MAX_LINES]
        ]
        if len(diff.changes) > MAX_LINES:
            lines.append(f"…and {len(diff.changes) - MAX_LINES} more")
        blocks.append(_mrkdwn(f"*{name}*\n" + "\n".join(lines)))
    blocks += _context(link, "View the full report")
    return {"text": f"{title}: {report.summary()}", "blocks": blocks}


def diff_message(diff: SchemaDiff, title: str, link: str | None = None) -> dict:
    lines = [f"{ICONS[c.severity]} {c.summary}" for c in diff.changes[:MAX_LINES]]
    if len(diff.changes) > MAX_LINES:
        lines.append(f"…and {len(diff.changes) - MAX_LINES} more")
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        _mrkdwn(diff.summary()),
    ]
    if lines:
        blocks.append(_mrkdwn("\n".join(lines)))
    blocks += _context(link, "View details")
    return {"text": f"{title}: {diff.summary()}", "blocks": blocks}
