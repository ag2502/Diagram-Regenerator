"""GitHub Actions plumbing: PR comments, step summaries and step outputs.

Only the REST API and the files Actions provides are used, so there are no
extra dependencies and everything works the same in any CI that sets the
standard ``GITHUB_*`` variables.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

MAX_COMMENT = 65000  # GitHub rejects comment bodies over 65,536 characters


class GitHubError(RuntimeError):
    pass


@dataclass
class PullRequest:
    repository: str
    number: int
    base_ref: str | None = None
    head_sha: str | None = None


def current_pull_request() -> PullRequest | None:
    """The PR this workflow run is for, from ``GITHUB_EVENT_PATH``."""
    path, repository = os.environ.get("GITHUB_EVENT_PATH"), os.environ.get("GITHUB_REPOSITORY")
    if not path or not repository or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        event = json.load(handle)
    pull = event.get("pull_request")
    if not pull:
        return None
    return PullRequest(
        repository=repository,
        number=int(pull.get("number") or event.get("number")),
        base_ref=(pull.get("base") or {}).get("ref"),
        head_sha=(pull.get("head") or {}).get("sha"),
    )


class GitHubClient:
    def __init__(self, token: str, api_url: str | None = None) -> None:
        self.token = token
        self.api_url = (
            api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        ).rstrip("/")

    def _request(self, method: str, path: str, payload: dict | None = None):
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "diagram-regenerator",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = (
                " (fork PRs get a read-only token; the job summary still has the report)"
                if exc.code in {403, 404}
                else ""
            )
            raise GitHubError(
                f"GitHub API {method} {path} failed: {exc.code} {detail}{hint}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"could not reach the GitHub API: {exc.reason}") from exc

    def find_comment(self, pr: PullRequest, marker: str) -> dict | None:
        for page in range(1, 11):
            comments = self._request(
                "GET",
                f"/repos/{pr.repository}/issues/{pr.number}/comments?per_page=100&page={page}",
            )
            for comment in comments or []:
                if marker in (comment.get("body") or ""):
                    return comment
            if not comments or len(comments) < 100:
                return None
        return None

    def upsert_comment(self, pr: PullRequest, body: str, marker: str, create: bool = True) -> str:
        """Update the comment carrying ``marker``, or create one. Returns what happened."""
        existing = self.find_comment(pr, marker)
        if existing:
            if existing.get("body") == body:
                return "unchanged"
            self._request(
                "PATCH", f"/repos/{pr.repository}/issues/comments/{existing['id']}", {"body": body}
            )
            return "updated"
        if not create:
            return "skipped"
        self._request("POST", f"/repos/{pr.repository}/issues/{pr.number}/comments", {"body": body})
        return "created"


def write_step_summary(markdown: str) -> bool:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(markdown.rstrip() + "\n\n")
    return True


def set_output(name: str, value: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    text = str(value).lower() if isinstance(value, bool) else str(value)
    with open(path, "a", encoding="utf-8") as handle:
        if "\n" in text:
            delimiter = f"EOF_{uuid.uuid4().hex}"
            handle.write(f"{name}<<{delimiter}\n{text}\n{delimiter}\n")
        else:
            handle.write(f"{name}={text}\n")
