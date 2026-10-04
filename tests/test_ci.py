import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from diagram_regenerator.ci import render_message
from diagram_regenerator.cli import main
from diagram_regenerator.github import GitHubClient, PullRequest, set_output, write_step_summary
from diagram_regenerator.report import COMMENT_MARKER


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class FakeGitHub(BaseHTTPRequestHandler):
    comments: list = []
    requests: list = []

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self):  # noqa: N802
        FakeGitHub.requests.append(("GET", self.path, None))
        page = int(self.path.split("page=")[-1]) if "page=" in self.path else 1
        self._json(200, FakeGitHub.comments if page == 1 else [])

    def do_POST(self):  # noqa: N802
        body = self._body()
        FakeGitHub.requests.append(("POST", self.path, body))
        comment = {"id": len(FakeGitHub.comments) + 1, "body": body["body"]}
        FakeGitHub.comments.append(comment)
        self._json(201, comment)

    def do_PATCH(self):  # noqa: N802
        body = self._body()
        FakeGitHub.requests.append(("PATCH", self.path, body))
        comment_id = int(self.path.rsplit("/", 1)[1])
        for comment in FakeGitHub.comments:
            if comment["id"] == comment_id:
                comment["body"] = body["body"]
        self._json(200, {"id": comment_id})

    def log_message(self, *args):
        pass


@pytest.fixture
def github(monkeypatch):
    FakeGitHub.comments, FakeGitHub.requests = [], []
    server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{server.server_port}")
    yield FakeGitHub
    server.shutdown()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main has one migration + generated docs; branch `feature` adds a breaking change."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(origin))
    (work / "db" / "migrations").mkdir(parents=True)
    (work / "db" / "migrations" / "V1__init.sql").write_text(
        "CREATE TABLE users (id bigint PRIMARY KEY, email text NOT NULL, nickname text);"
    )
    (work / "diagram-regen.toml").write_text('source = "db/migrations"\n')
    monkeypatch.chdir(work)
    assert main(["generate", "-q"]) == 0
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    git(work, "push", "-q", "-u", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature")
    (work / "db" / "migrations" / "V2__orders.sql").write_text(
        "CREATE TABLE orders (id bigint PRIMARY KEY, user_id bigint REFERENCES users(id));\n"
        "ALTER TABLE users DROP COLUMN nickname;\n"
    )
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "orders")
    return work


@pytest.fixture
def actions_env(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"pull_request": {"number": 7, "base": {"ref": "main"}, "head": {"sha": "abc"}}})
    )
    summary, output = tmp_path / "summary.md", tmp_path / "output.txt"
    summary.write_text("")
    output.write_text("")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/shop")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return summary, output


def test_ci_pr_comments_once_and_updates(repo, github, actions_env, capsys):
    summary, output = actions_env
    assert main(["ci", "pr", "--fail-on", "never"]) == 0
    out = capsys.readouterr()
    assert "Table orders added" in out.out and "PR comment created" in out.err

    (post,) = [r for r in github.requests if r[0] == "POST"]
    assert post[1] == "/repos/acme/shop/issues/7/comments"
    body = post[2]["body"]
    assert body.startswith(COMMENT_MARKER)
    assert "Column `users.nickname` removed" in body and "```mermaid" in body
    assert "Compared `origin/main` → this branch" in body
    assert "will change: run `diagram-regen generate`" in body

    outputs = output.read_text()
    assert "changed=true" in outputs and "breaking=1" in outputs
    assert "### Database schema changes" in summary.read_text()

    # Second run edits the same comment instead of posting a new one.
    (repo / "db" / "migrations" / "V3__note.sql").write_text(
        "ALTER TABLE orders ADD COLUMN note text;"
    )
    assert main(["ci", "pr", "--fail-on", "never"]) == 0
    assert "PR comment updated" in capsys.readouterr().err
    assert [r[0] for r in github.requests].count("POST") == 1
    assert 'text note "🟢 new table"' in github.comments[0]["body"]

    # Breaking changes fail the check with the default policy.
    assert main(["ci", "pr"]) == 1


def test_ci_pr_without_changes_does_not_comment(repo, github, actions_env, capsys):
    git(repo, "checkout", "-q", "main")
    assert main(["ci", "pr"]) == 0
    assert not [r for r in github.requests if r[0] in {"POST", "PATCH"}]
    assert "changed=false" in actions_env[1].read_text()


def test_ci_pr_outside_actions(repo, monkeypatch, capsys):
    for name in ("GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    assert main(["ci", "pr", "--base", "main", "--fail-on", "never"]) == 0
    assert "no PR comment posted" in capsys.readouterr().err


def test_ci_update_commits_and_pushes(repo, actions_env, monkeypatch, capsys):
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "feature")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert (
        main(
            [
                "ci",
                "update",
                "--commit",
                "--push",
                "--message",
                "{datetime} - Regenerate database schema docs\\n\\n{summary}",
                "--author-name",
                "Amogh Gaikwad",
                "--author-email",
                "amogh@example.com",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "updated docs/schema/README.md" in out and "pushed" in out
    log = git(repo, "log", "-1", "--format=%an <%ae>%n%s%n%b")
    assert log.startswith("Amogh Gaikwad <amogh@example.com>\n")
    assert " - Regenerate database schema docs\n" in log
    assert "changes across" in log
    assert git(repo, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")
    assert "commit=" in actions_env[1].read_text()

    # Nothing left to do on a second run.
    assert main(["ci", "update", "--commit"]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_ci_check_mode_matches_action(repo):
    assert main(["check"]) == 1  # feature branch changed the schema


def test_init_ci_writes_valid_workflows(repo, capsys):
    (repo / "diagram-regen.toml").write_text(
        'source = "db/migrations"\n[environments]\nstaging = "${STAGING_URL}"\nprod = "${PROD_URL}"\n'
    )
    assert main(["init-ci", "--drift"]) == 0
    docs = yaml.safe_load((repo / ".github/workflows/schema-docs.yml").read_text())
    trigger = docs[True]  # PyYAML reads the `on:` key as boolean True
    assert trigger["pull_request"]["paths"] == [
        "db/migrations/**",
        "diagram-regen.toml",
        "docs/schema/descriptions.yml",
    ]
    steps = docs["jobs"]["schema"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert steps[1]["uses"].startswith("ag2502/Diagram-Regenerator@v")
    drift = yaml.safe_load((repo / ".github/workflows/schema-drift.yml").read_text())
    env = drift["jobs"]["drift"]["steps"][1]["env"]
    assert set(env) == {"STAGING_DATABASE_URL", "PROD_DATABASE_URL"}
    assert main(["init-ci"]) == 2  # refuses to overwrite


def test_action_yml_is_valid():
    from pathlib import Path

    action = yaml.safe_load((Path(__file__).parents[1] / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert {"mode", "config", "slack-webhook", "extras"} <= set(action["inputs"])
    run = action["runs"]["steps"][-1]["run"]
    assert "${{" not in run  # inputs reach the script via env only (no injection)


def test_github_helpers(tmp_path, monkeypatch, github):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    set_output("flag", True)
    set_output("text", "two\nlines")
    content = out.read_text()
    assert content.startswith("flag=true\ntext<<EOF_")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert write_step_summary("x") is False

    client = GitHubClient("t")
    pr = PullRequest("acme/shop", 3)
    assert (
        client.upsert_comment(pr, f"{COMMENT_MARKER} hi", COMMENT_MARKER, create=False) == "skipped"
    )
    assert client.upsert_comment(pr, f"{COMMENT_MARKER} hi", COMMENT_MARKER) == "created"
    assert client.upsert_comment(pr, f"{COMMENT_MARKER} hi", COMMENT_MARKER) == "unchanged"


def test_render_message():
    text = render_message("{date} - docs\\n\\n{summary} ({fingerprint})", "1 change", "abc")
    first, _, rest = text.partition("\n")
    assert first.endswith(" - docs") and rest == "\n1 change (abc)"
