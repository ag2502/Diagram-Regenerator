import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from diagram_regenerator.cli import main
from diagram_regenerator.drift import detect_drift, format_drift_markdown, format_drift_text
from diagram_regenerator.notify import NotifyError, ci_run_url, diff_message, post_slack
from diagram_regenerator.sources.sql import replay

MIGRATIONS = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    discount_cents INTEGER
);
CREATE INDEX ix_orders_customer ON orders (customer_id);
"""


def _db(path, sql):
    with sqlite3.connect(path) as connection:
        connection.executescript(sql)
    return f"sqlite:///{path}"


@pytest.fixture
def envs(tmp_path):
    return {
        "dev": _db(tmp_path / "dev.db", MIGRATIONS),
        # a migration never ran on staging
        "staging": _db(
            tmp_path / "staging.db", MIGRATIONS.replace(",\n    discount_cents INTEGER", "")
        ),
        # someone hot-fixed prod by hand
        "prod": _db(
            tmp_path / "prod.db",
            MIGRATIONS.replace("VARCHAR(255)", "VARCHAR(320)").replace(
                "CREATE INDEX ix_orders_customer ON orders (customer_id);",
                "CREATE TABLE tmp_fix (id INTEGER);",
            ),
        ),
    }


def _report(envs, **kwargs):
    from diagram_regenerator.sources import load_source

    baseline = replay([("m.sql", MIGRATIONS)], "sqlite").schema
    loaders = {name: (lambda url=url: load_source(url)) for name, url in envs.items()}
    return detect_drift("migrations", baseline, loaders, **kwargs)


def test_detect_drift_matrix(envs):
    report = _report(envs)
    assert report.drifted == ["staging", "prod"]
    assert report.has_drift
    rows = {subject: (cells, severity) for subject, cells, severity in report.matrix()}
    assert rows["orders.discount_cents"] == (
        {"dev": "✓", "staging": "missing", "prod": "✓"},
        "breaking",
    )
    assert rows["customers.email"][0] == {"dev": "✓", "staging": "✓", "prod": "varchar(320)"}
    assert rows["tmp_fix"][0]["prod"] == "extra table"
    assert rows["orders: index (customer_id)"][0]["prod"] == "missing"
    assert report.summary() == (
        "2 of 3 environments differ from migrations: staging (1 difference), prod (3 differences)"
    )


def test_unreachable_environment_is_reported_not_fatal(envs, tmp_path):
    envs["qa"] = f"sqlite:///{tmp_path}/no/such/dir/qa.db"
    report = _report(envs)
    assert "qa" in report.errors
    assert "staging" in report.drifted
    assert all(cells["qa"] == "?" for _, cells, _ in report.matrix())
    assert "qa: could not be read" in format_drift_text(report)


def test_formats(envs):
    report = _report(envs)
    text = format_drift_text(report)
    assert text.splitlines()[0] == "Schema drift against migrations"
    assert "orders.discount_cents" in text and "missing" in text
    markdown = format_drift_markdown(report, footer="_run_")
    assert "✅ **dev** matches" in markdown
    assert "🔴 **staging** 1 difference" in markdown
    assert "| 🔴 `orders.discount_cents` | ✓ | missing | ✓ |" in markdown
    assert markdown.rstrip().endswith("_run_")


def test_no_drift(envs):
    report = _report({"dev": envs["dev"]})
    assert not report.has_drift
    assert report.summary() == "All 1 environment match migrations."


class _Hook(BaseHTTPRequestHandler):
    received: list = []
    status = 200

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers["Content-Length"])
        _Hook.received.append(json.loads(self.rfile.read(length)))
        self.send_response(_Hook.status)
        self.end_headers()
        self.wfile.write(b"ok" if _Hook.status == 200 else b"invalid_payload")

    def log_message(self, *args):
        pass


@pytest.fixture
def slack():
    _Hook.received = []
    _Hook.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def test_cli_drift_with_slack(envs, tmp_path, monkeypatch, slack, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schema.sql").write_text(MIGRATIONS)
    for name, url in envs.items():
        monkeypatch.setenv(f"DR_{name.upper()}_URL", url)
    monkeypatch.setenv("DR_SLACK_HOOK", slack)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/shop")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    (tmp_path / "diagram-regen.toml").write_text(
        'source = "schema.sql"\ndialect = "sqlite"\n'
        "[environments]\n"
        'dev = "${DR_DEV_URL}"\nstaging = "${DR_STAGING_URL}"\nprod = "${DR_PROD_URL}"\n'
        '[notify]\nslack_webhook = "${DR_SLACK_HOOK}"\n'
    )
    assert main(["drift", "--slack"]) == 1
    captured = capsys.readouterr()
    assert "Schema drift against schema.sql" in captured.out
    assert "notified Slack" in captured.err
    (message,) = _Hook.received
    assert message["blocks"][0]["text"]["text"] == "Schema drift detected"
    joined = json.dumps(message)
    assert "staging" in joined and "missing column `orders.discount_cents`" in joined
    assert "`customers.email` is varchar(320) here (baseline varchar(255))" in joined
    assert "https://github.com/acme/shop/actions/runs/42" in joined
    assert "sqlite:///" not in joined  # never leak connection strings

    # explicit envs, environment as baseline, JSON output, no failure
    assert (
        main(
            [
                "drift",
                "--env",
                f"a={envs['dev']}",
                "--env",
                f"b={envs['dev']}",
                "--baseline",
                "a",
                "-f",
                "json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["has_drift"] is False and data["baseline"] == "a"
    assert main(["drift", "--no-fail", "-f", "markdown"]) == 0


def test_cli_drift_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["drift"]) == 2
    assert "no environments" in capsys.readouterr().err
    assert main(["drift", "--env", "broken"]) == 2


def test_post_slack_errors(slack):
    _Hook.status = 400
    with pytest.raises(NotifyError, match="rejected"):
        post_slack(slack, {"text": "x"})
    with pytest.raises(NotifyError, match="could not reach"):
        post_slack("http://127.0.0.1:9/nothing", {"text": "x"}, timeout=2)


def test_messages_and_run_url(monkeypatch):
    from diagram_regenerator.diff import diff_schemas

    old = replay([("a.sql", "CREATE TABLE t (id int);")]).schema
    new = replay([("b.sql", "CREATE TABLE t (id int, x int);")]).schema
    message = diff_message(diff_schemas(old, new), "Schema changed on main", link="https://x")
    assert message["blocks"][0]["text"]["text"] == "Schema changed on main"
    assert "<https://x|View details>" in json.dumps(message)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    assert ci_run_url() is None
