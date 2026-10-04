import json

import pytest
from helpers import assert_valid_mermaid_er

from diagram_regenerator import __version__
from diagram_regenerator.cli import main
from diagram_regenerator.scaffold import detect


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "generate" in capsys.readouterr().out


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A repo with Prisma-style PostgreSQL migrations."""
    migrations = tmp_path / "prisma" / "migrations"
    (migrations / "20240101000000_init").mkdir(parents=True)
    (migrations / "20240101000000_init" / "migration.sql").write_text(
        'CREATE TABLE "users" ("id" SERIAL NOT NULL, "email" TEXT NOT NULL, '
        'CONSTRAINT "users_pkey" PRIMARY KEY ("id"));\n'
        'CREATE UNIQUE INDEX "users_email_key" ON "users"("email");\n'
    )
    (migrations / "migration_lock.toml").write_text('provider = "postgresql"\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _add_migration(root, name, sql):
    folder = root / "prisma" / "migrations" / name
    folder.mkdir()
    (folder / "migration.sql").write_text(sql)


def test_full_workflow(project, capsys):
    # init detects the Prisma migrations and their dialect
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "found SQL in prisma/migrations (Prisma migration_lock.toml)" in out
    config = (project / "diagram-regen.toml").read_text()
    assert 'source = "prisma/migrations"' in config and 'dialect = "postgresql"' in config
    assert main(["init"]) == 2  # refuses to overwrite
    capsys.readouterr()

    # check fails before anything was generated
    assert main(["check"]) == 1
    assert "docs/schema/README.md is missing" in capsys.readouterr().out

    # generate writes docs + snapshot
    assert main(["generate"]) == 0
    out = capsys.readouterr().out
    assert "Read prisma/migrations: 1 tables, 2 columns" in out
    assert "updated      docs/schema/README.md" in out
    readme = (project / "docs/schema/README.md").read_text()
    assert "```mermaid" in readme and "source `prisma/migrations`" in readme
    snapshot = json.loads((project / "docs/schema/schema.json").read_text())
    assert list(snapshot["tables"]) == ["users"]

    assert main(["generate"]) == 0
    assert "unchanged    docs/schema/README.md" in capsys.readouterr().out
    assert main(["check"]) == 0
    assert "up to date" in capsys.readouterr().out

    # a new migration makes the docs stale, and check explains why
    _add_migration(
        project,
        "20240201000000_posts",
        'CREATE TABLE "posts" ("id" SERIAL PRIMARY KEY, "user_id" INTEGER NOT NULL '
        'REFERENCES "users"("id"), "title" TEXT);\n'
        'ALTER TABLE "users" DROP COLUMN "email";\n',
    )
    assert main(["check", "--no-color"]) == 1
    out = capsys.readouterr().out
    assert "docs/schema/README.md is out of date" in out
    assert "Column users.email removed" in out
    assert "Run `diagram-regen generate`" in out

    # diff defaults: committed snapshot -> configured source
    assert main(["diff", "--no-color"]) == 1  # breaking change, fail_on = breaking
    assert "Table posts added" in capsys.readouterr().out
    assert main(["diff", "--fail-on", "never", "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["breaking"] == 1
    assert main(["diff", "-f", "markdown", "-o", "out/diff.md", "--fail-on", "never"]) == 0
    markdown = (project / "out/diff.md").read_text()
    assert "### Schema changes" in markdown and "```mermaid" in markdown
    assert "Compared `docs/schema/schema.json` → `prisma/migrations`" in markdown
    assert main(["diff", "--ignore", "indexes", "--fail-on", "any"]) == 1
    capsys.readouterr()

    assert main(["generate"]) == 0
    assert "Changes since the last snapshot: 3 changes" in capsys.readouterr().out
    assert main(["check"]) == 0


def test_render_any_source_without_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE a (id int PRIMARY KEY); CREATE TABLE b (id int, a_id int REFERENCES a(id));"
    )
    assert main(["render", "schema.sql"]) == 0
    out = capsys.readouterr().out
    assert_valid_mermaid_er(out)
    assert 'a |o..o{ b : "a_id"' in out
    for fmt in ("markdown", "dbml", "html", "json"):
        assert main(["render", "schema.sql", "-f", fmt, "-o", f"out.{fmt}"]) == 0
        assert (tmp_path / f"out.{fmt}").stat().st_size > 0
    assert main(["render", "schema.sql", "-f", "png"]) == 2
    assert "unknown format" in capsys.readouterr().err


def test_diff_between_two_explicit_sources(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "old.sql").write_text("CREATE TABLE t (id int PRIMARY KEY, v varchar(10));")
    (tmp_path / "new.sql").write_text("CREATE TABLE t (id int PRIMARY KEY, v varchar(20));")
    assert main(["diff", "old.sql", "new.sql", "--no-color"]) == 0
    assert "varchar(10) → varchar(20)  [safe]" in capsys.readouterr().out
    assert main(["diff", "new.sql", "old.sql", "--no-color"]) == 1


def test_errors_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["generate"]) == 2
    assert "no schema source configured" in capsys.readouterr().err
    assert main(["generate", "--source", "missing_dir"]) == 2
    assert "not found" in capsys.readouterr().err
    (tmp_path / "diagram-regen.toml").write_text('source = "${DR_UNSET_FOR_TEST}"\n')
    monkeypatch.delenv("DR_UNSET_FOR_TEST", raising=False)
    assert main(["generate"]) == 2
    assert "DR_UNSET_FOR_TEST" in capsys.readouterr().err


def test_sql_warnings_are_reported_and_quiet_hides_them(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schema.sql").write_text(
        "ALTER TABLE ghost ADD COLUMN x int;\nCREATE TABLE a (id int);"
    )
    assert main(["render", "schema.sql"]) == 0
    assert "WARNING: schema.sql:1: table 'ghost'" in capsys.readouterr().err
    assert main(["render", "schema.sql", "-q"]) == 0
    assert capsys.readouterr().err == ""


def test_detect_variants(tmp_path):
    assert detect(tmp_path).source is None
    (tmp_path / "manage.py").write_text("")
    assert "Django" in detect(tmp_path).reason
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    (tmp_path / "db" / "migrations" / "001.sql").write_text(
        "CREATE TABLE `t` (`id` int AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB;"
    )
    found = detect(tmp_path)
    assert (found.source, found.dialect) == ("db/migrations", "mysql")
