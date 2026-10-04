import json
import sqlite3
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from diagram_regenerator.cli import main
from diagram_regenerator.describe import (
    DEFAULT_MODEL,
    ClaudeDescriber,
    DescribeError,
    HeuristicDescriber,
    draft_descriptions,
    fetch_samples,
    redact_value,
    table_context,
)
from diagram_regenerator.descriptions import Descriptions
from diagram_regenerator.sources.sql import replay

SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    published_at timestamptz,
    balance_cents integer,
    login_count integer,
    avatar_url text,
    legacy_crm_id text,
    mystery jsonb
);
COMMENT ON COLUMN users.mystery IS 'Set by the billing job';
CREATE TABLE groups (id bigint PRIMARY KEY, name text);
CREATE TABLE group_members (
    group_id bigint REFERENCES groups(id) ON DELETE CASCADE,
    user_id uuid REFERENCES users(id),
    PRIMARY KEY (group_id, user_id)
);
"""


@pytest.fixture
def schema():
    return replay([("s.sql", SQL)]).schema


def test_heuristics(schema):
    describer = HeuristicDescriber()
    users = schema.get("users")
    draft = describer.describe(users, schema, [c.name for c in users.columns], True)
    assert draft.table is None  # "Users." would add nothing
    assert draft.columns == {
        "id": "Primary key (UUID).",
        "email": "Email address.",
        "is_active": "Whether the user is active.",
        "created_at": "When the user was created.",
        "deleted_at": "When the user was deleted; NULL while it is active (soft delete).",
        "published_at": "When the user was published.",
        "balance_cents": "Balance in cents.",
        "login_count": "Number of logins.",
        "avatar_url": "URL of the avatar.",
        "legacy_crm_id": "Identifier of the related legacy crm (no foreign key).",
    }
    members = schema.get("group_members")
    draft = describer.describe(members, schema, ["group_id", "user_id"], True)
    assert draft.table == "Links groups and users (many-to-many)."
    assert draft.columns["group_id"] == "References groups.id; rows are deleted with it."


def test_draft_descriptions_respects_people_and_comments(schema):
    docs = Descriptions()
    docs.set_column("users", "email", "Login address, verified")
    docs.set_column("users", "is_active", "[draft] old guess")
    result = draft_descriptions(schema, docs, HeuristicDescriber())
    assert docs.column("users", "email") == "Login address, verified"
    assert docs.column("users", "is_active") == "[draft] old guess"
    assert docs.column("users", "mystery") is None  # has a database comment
    assert docs.column("users", "created_at") == "[draft] When the user was created."
    assert docs.table("group_members") == "[draft] Links groups and users (many-to-many)."
    assert docs.table("groups") is None
    assert result.tables == 3 and result.added > 10

    redrafted = draft_descriptions(
        schema, docs, HeuristicDescriber(), redraft=True, tables=["users"]
    )
    assert docs.column("users", "is_active") == "[draft] Whether the user is active."
    assert docs.column("users", "email") == "Login address, verified"
    assert redrafted.tables == 1


def test_table_context(schema):
    text = table_context(schema.get("group_members"), schema)
    assert "Table: group_members" in text
    assert "group_id: bigint, not null, primary key, references groups(id)" in text
    users = table_context(schema.get("users"), schema)
    assert "mystery: jsonb, nullable -- Set by the billing job" in users
    assert "Referenced by: group_members.user_id" in users
    assert "Unique: (email)" in users


class FakeMessages:
    def __init__(self, reply=None, stop_reason="end_turn", error=None):
        self.reply, self.stop_reason, self.error = reply, stop_reason, error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = SimpleNamespace(type="text", text=json.dumps(self.reply))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text])


def _client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_claude_describer_request_and_parsing(schema):
    messages = FakeMessages(
        {
            "table_description": "Workspaces users collaborate in.",
            "columns": [
                {"name": "name", "description": "Display name shown in the UI."},
                {"name": "not_asked", "description": "ignored"},
                {"name": "id", "description": "  "},
            ],
        }
    )
    describer = ClaudeDescriber(client=_client(messages))
    draft = describer.describe(
        schema.get("groups"), schema, ["id", "name"], True, samples=[{"name": "<email>"}]
    )
    assert draft.table == "Workspaces users collaborate in."
    assert draft.columns == {"name": "Display name shown in the UI."}

    (call,) = messages.calls
    assert call["model"] == DEFAULT_MODEL == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["effort"] == "low"
    schema_spec = call["output_config"]["format"]["schema"]
    assert schema_spec["required"] == ["table_description", "columns"]
    assert schema_spec["additionalProperties"] is False
    prompt = call["messages"][0]["content"]
    assert "Table: groups" in prompt and "Sample rows" in prompt
    assert "Describe these columns: id, name." in prompt


def test_claude_describer_without_table_description(schema):
    messages = FakeMessages({"columns": []})
    ClaudeDescriber(client=_client(messages), model="claude-sonnet-5-5").describe(
        schema.get("groups"), schema, ["name"], False
    )
    call = messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert "table_description" not in call["output_config"]["format"]["schema"]["properties"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_claude_describer_skips_unusable_responses(schema, stop_reason, caplog):
    messages = FakeMessages({"columns": [{"name": "name", "description": "x"}]}, stop_reason)
    draft = ClaudeDescriber(client=_client(messages)).describe(
        schema.get("groups"), schema, ["name"], False
    )
    assert draft.columns == {} and draft.table is None
    assert "groups" in caplog.text


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


def test_claude_describer_errors(schema):
    auth = FakeMessages(error=_status_error(anthropic.AuthenticationError, 401))
    with pytest.raises(DescribeError, match="ANTHROPIC_API_KEY"):
        ClaudeDescriber(client=_client(auth)).describe(
            schema.get("groups"), schema, ["name"], False
        )
    missing = FakeMessages(error=_status_error(anthropic.NotFoundError, 404))
    with pytest.raises(DescribeError, match="unknown model"):
        ClaudeDescriber(client=_client(missing)).describe(
            schema.get("groups"), schema, ["name"], False
        )
    server = FakeMessages(error=_status_error(anthropic.InternalServerError, 500))
    draft = ClaudeDescriber(client=_client(server)).describe(
        schema.get("groups"), schema, ["name"], False
    )
    assert draft.columns == {}


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("note", "mail me at jane.doe@example.com", "mail me at <email>"),
        ("contact", "call +1 (415) 555-0100", "call <phone>"),
        ("password_hash", "$2b$12$abc", "<hidden>"),
        ("session", "eyJhbGciOiJIUzI1NiJ9abcdefghijk", "<token>"),
        (
            "bio",
            "Loves hiking, climbing and long walks every weekend",
            "Loves hiking, climbing and long walks e…",
        ),
        ("age", 42, 42),
        ("flag", None, None),
    ],
)
def test_redact_value(column, value, expected):
    assert redact_value(column, value) == expected


def test_fetch_samples_sqlite(tmp_path):
    db = tmp_path / "s.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            "CREATE TABLE people (id integer primary key, email text, api_key text);"
            "INSERT INTO people VALUES (1, 'a@b.co', 'k1'), (2, 'c@d.co', 'k2'), (3, 'e@f.co', 'k3');"
        )
    schema = replay(
        [("x.sql", "CREATE TABLE people (id integer primary key, email text, api_key text);")],
        "sqlite",
    ).schema
    rows = fetch_samples(f"sqlite:///{db}", schema.get("people"), 2)
    assert rows == [
        {"id": 1, "email": "<email>", "api_key": "<hidden>"},
        {"id": 2, "email": "<email>", "api_key": "<hidden>"},
    ]


def test_cli_describe_heuristic(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schema.sql").write_text(SQL)
    (tmp_path / "diagram-regen.toml").write_text('source = "schema.sql"\n')
    assert main(["describe", "--provider", "heuristic", "--dry-run"]) == 0
    assert "Would add" in capsys.readouterr().out
    assert not (tmp_path / "docs/schema/descriptions.yml").exists()

    assert main(["describe", "--provider", "heuristic", "--table", "group*"]) == 0
    out = capsys.readouterr().out
    assert "across 2 tables" in out and "Review docs/schema/descriptions.yml" in out
    docs = Descriptions.load(tmp_path / "docs/schema/descriptions.yml")
    assert docs.table("group_members").startswith("[draft] Links groups")
    assert docs.column("users", "email") is None

    assert main(["generate"]) == 0
    readme = (tmp_path / "docs/schema/README.md").read_text()
    assert "_draft:_ References groups.id; rows are deleted with it." in readme

    assert main(["describe", "--provider", "heuristic", "--table", "nope*"]) == 2
    assert main(["describe", "--provider", "heuristic", "--samples", "3"]) == 2
    assert "live database" in capsys.readouterr().err


def test_heuristic_events_join_tables_and_enums():
    schema = replay(
        [
            (
                "s.sql",
                """
                CREATE TYPE task_status AS ENUM ('todo', 'done');
                CREATE TABLE users (id int PRIMARY KEY);
                CREATE TABLE tasks (id int PRIMARY KEY, status task_status);
                CREATE TABLE comments (id int PRIMARY KEY, task_id int REFERENCES tasks(id),
                    author_id int REFERENCES users(id), body text);
                CREATE TABLE invoices (id int PRIMARY KEY, paid_at timestamptz, due_on date,
                    renews_on date, last_login_at timestamptz);
                """,
            )
        ]
    ).schema
    describer = HeuristicDescriber()
    invoices = schema.get("invoices")
    draft = describer.describe(
        invoices, schema, ["paid_at", "due_on", "renews_on", "last_login_at"], False
    )
    assert draft.columns == {
        "paid_at": "When the invoice was paid.",
        "due_on": "When the invoice is due.",
        "renews_on": "When the invoice renews.",
        "last_login_at": "Last login time.",
    }
    assert describer.describe(schema.get("comments"), schema, [], True).table is None
    status = describer.describe(schema.get("tasks"), schema, ["status"], False).columns["status"]
    assert status == "Current status of the task. One of: todo, done."
