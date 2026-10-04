import subprocess
import textwrap

import pytest

from diagram_regenerator.config import ConfigError, expand, find_config, load_config, parse_config
from diagram_regenerator.descriptions import Descriptions
from diagram_regenerator.model import Column, Schema, Table
from diagram_regenerator.project import Project
from diagram_regenerator.sources import SourceError
from diagram_regenerator.sources.git import load_git

# ----------------------------------------------------------------------------- config


def test_expand_env(monkeypatch):
    monkeypatch.setenv("DR_HOST", "db.local")
    monkeypatch.delenv("DR_MISSING", raising=False)
    assert expand("postgresql://${DR_HOST}/app") == "postgresql://db.local/app"
    assert expand("${DR_MISSING:-sqlite:///x.db}") == "sqlite:///x.db"
    with pytest.raises(ConfigError, match="DR_MISSING"):
        expand("${DR_MISSING}", "environment 'prod'")


def test_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.path is None
    assert config.output.files() == {
        "json": "docs/schema/schema.json",
        "markdown": "docs/schema/README.md",
    }
    with pytest.raises(ConfigError, match="no schema source"):
        config.source_spec()


def test_full_config(tmp_path, monkeypatch):
    (tmp_path / "diagram-regen.toml").write_text(
        textwrap.dedent(
            """
            source = "db/migrations"
            dialect = "pg"
            exclude = ["tmp_*"]

            [output]
            snapshot = "schema/schema.json"
            markdown = "schema/SCHEMA.md"
            dbml = "schema/schema.dbml"
            diagram = "keys"

            [groups]
            Billing = ["invoice*"]

            [diff]
            fail_on = "warning"
            ignore = ["comments"]

            [environments]
            prod = "${DR_PROD_URL}"

            [drift]
            baseline = "prod"

            [notify]
            slack_webhook = "${DR_SLACK:-}"
            """
        )
    )
    nested = tmp_path / "app" / "sub"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    config = load_config()
    assert config.path == tmp_path / "diagram-regen.toml"
    assert config.dialect == "postgresql"
    assert config.source_spec() == str(tmp_path / "db/migrations")
    assert config.output.files()["dbml"] == "schema/schema.dbml"
    assert config.groups == {"Billing": ["invoice*"]}
    assert (config.fail_on, config.diff_ignore) == ("warning", ["comments"])
    assert config.webhook() is None
    monkeypatch.setenv("DR_PROD_URL", "postgres://u:p@h/db")
    assert config.environment("prod") == "postgres://u:p@h/db"
    with pytest.raises(ConfigError, match="unknown environment"):
        config.environment("qa")


def test_pyproject_table(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.diagram-regen]\nsource = "schema.sql"\n'
    )
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert find_config() == tmp_path / "pyproject.toml"
    assert load_config().source == "schema.sql"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"sorce": "x"}, "unknown setting"),
        ({"dialect": "dbase"}, "unknown dialect"),
        ({"output": {"diagram": "most"}}, "output.diagram"),
        ({"diff": {"fail_on": "sometimes"}}, "fail_on"),
        ({"exclude": [1]}, "list of strings"),
        ({"drift": {"baseline": "qa"}}, "baseline"),
    ],
)
def test_invalid_config(tmp_path, data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data, root=tmp_path)


def test_invalid_toml(tmp_path):
    path = tmp_path / "diagram-regen.toml"
    path.write_text("source = ")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


# ----------------------------------------------------------------------------- descriptions


def _schema() -> Schema:
    schema = Schema()
    schema.add_table(
        Table(
            "users",
            columns=[Column("id", "integer", comment="Primary key"), Column("email", "text")],
            primary_key=["id"],
        )
    )
    return schema


def test_descriptions_round_trip_and_apply(tmp_path):
    docs = Descriptions()
    assert docs.set_table("users", "Accounts")
    assert docs.set_column("users", "email", "[draft] Login")
    assert not docs.set_column("users", "email", "ignored")
    assert docs.set_column("users", "email", "Login address", overwrite=True)
    docs.set_column("ghost", "x", "gone")
    path = docs.save(tmp_path / "d" / "descriptions.yml")
    assert path.read_text().startswith("# Table and column descriptions")

    loaded = Descriptions.load(path)
    applied = loaded.apply(_schema())
    users = applied.get("users")
    assert users.comment == "Accounts"
    assert users.column("email").comment == "Login address"
    assert users.column("id").comment == "Primary key"  # database comment kept
    assert _schema().get("users").comment is None  # original untouched
    assert loaded.stale(_schema()) == ["ghost"]
    assert loaded.missing(_schema()) == []
    assert Descriptions().missing(_schema()) == [("users", None), ("users", "email")]
    assert loaded.coverage(_schema()) == (3, 0, 3)


def test_descriptions_load_shorthand_and_missing(tmp_path):
    path = tmp_path / "d.yml"
    path.write_text("tables:\n  users: Accounts\n  empty:\n")
    docs = Descriptions.load(path)
    assert docs.table("users") == "Accounts"
    assert Descriptions.load(tmp_path / "nope.yml").tables == {}
    path.write_text("- not a mapping\n")
    with pytest.raises(ValueError):
        Descriptions.load(path)


# ----------------------------------------------------------------------------- git


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    migrations = tmp_path / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "0001_init.up.sql").write_text("CREATE TABLE users (id int PRIMARY KEY);")
    (migrations / "0001_init.down.sql").write_text("DROP TABLE users;")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (migrations / "0002_posts.up.sql").write_text(
        "CREATE TABLE posts (id int PRIMARY KEY, user_id int REFERENCES users(id));"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "posts")
    return tmp_path


def test_load_git_migrations(repo):
    migrations = repo / "db" / "migrations"
    assert sorted(load_git("HEAD~1", migrations).tables) == ["users"]
    assert sorted(load_git("HEAD", migrations).tables) == ["posts", "users"]
    with pytest.raises(SourceError, match="not found"):
        load_git("no-such-ref", migrations)


def test_project_git_prefers_committed_snapshot(repo, monkeypatch):
    monkeypatch.chdir(repo)
    config = parse_config({"source": "db/migrations"}, root=repo)
    project = Project(config)
    # No snapshot committed yet: falls back to replaying migrations at the ref.
    assert sorted(project.load("git:HEAD~1").tables) == ["users"]

    snapshot = Schema()
    snapshot.add_table(Table("from_snapshot", columns=[Column("id", "int")]))
    snapshot.save(repo / "docs/schema/schema.json")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "snapshot")
    assert list(project.load("git:HEAD").tables) == ["from_snapshot"]
    assert sorted(project.load("git:HEAD:db/migrations").tables) == ["posts", "users"]


# ----------------------------------------------------------------------------- project


def test_project_outputs_and_labels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE users (id int PRIMARY KEY, email text); CREATE TABLE tmp_x (id int);"
    )
    docs = Descriptions()
    docs.set_column("users", "email", "Login address")
    docs.save(tmp_path / "docs/schema/descriptions.yml")
    config = parse_config(
        {
            "source": "schema.sql",
            "exclude": ["tmp_*"],
            "output": {"html": "docs/schema/index.html"},
        },
        root=tmp_path,
    )
    project = Project(config)
    schema = project.load()
    assert list(schema.tables) == ["users"]
    outputs = {o.fmt: o for o in project.outputs(schema)}
    assert set(outputs) == {"json", "markdown", "html"}
    assert "Login address" in outputs["markdown"].content
    assert "Login address" not in outputs["json"].content  # snapshot stays a pure schema
    assert "source `schema.sql`" in outputs["markdown"].content
    assert all(o.stale for o in outputs.values())
    assert outputs["json"].write() and not outputs["json"].write()
    assert (
        project.describe("postgresql://u:secret@db.internal:5432/app") == "postgresql database app"
    )
    assert project.relative(tmp_path / "a" / "b.md") == "a/b.md"


def test_project_env_source(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE t (id integer primary key)")
    monkeypatch.setenv("DR_TEST_DB", f"sqlite:///{db}")
    config = parse_config({"environments": {"dev": "${DR_TEST_DB}"}}, root=tmp_path)
    assert list(Project(config).load("env:dev").tables) == ["t"]
