import sqlite3
import textwrap

import pytest

from diagram_regenerator.dialects import canonical_dialect, default_schema_for, sqlglot_dialect
from diagram_regenerator.sources import SourceError, describe_source, detect_kind, load_source
from diagram_regenerator.sources.sqla import prepare_url, redact_url

SHOP_DDL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    total_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_orders_status ON orders (status);
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
"""


@pytest.fixture
def shop_db(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SHOP_DDL)
    return f"sqlite:///{path}"


def test_reflect_sqlite(shop_db):
    schema = load_source(shop_db)
    assert schema.dialect == "sqlite"
    assert sorted(schema.tables) == ["alembic_version", "customers", "orders"]

    customers = schema.get("customers")
    assert [c.name for c in customers.columns] == ["id", "email", "full_name", "created_at"]
    assert customers.primary_key == ["id"]
    email = customers.column("email")
    assert (email.type, email.nullable) == ("varchar(255)", False)
    assert customers.is_unique(["email"])
    assert customers.column("created_at").default == "current_timestamp"

    orders = schema.get("orders")
    (fk,) = orders.foreign_keys
    assert (fk.columns, fk.ref_table, fk.ref_columns, fk.on_delete) == (
        ["customer_id"],
        "customers",
        ["id"],
        "CASCADE",
    )
    assert orders.column("status").default == "'pending'"
    assert orders.column("total_cents").default == "0"
    assert [(ix.columns, ix.unique) for ix in orders.indexes] == [(["status"], False)]


def test_reflection_is_deterministic(shop_db):
    assert load_source(shop_db).to_json() == load_source(shop_db).to_json()


def test_bookkeeping_tables_filtered(shop_db):
    assert "alembic_version" not in load_source(shop_db).filtered().tables


def test_missing_driver_message():
    with pytest.raises(SourceError, match="driver"):
        load_source("oracle+notadriver://scott:tiger@localhost/xe")


def test_unreachable_database_hides_password(tmp_path):
    with pytest.raises(SourceError) as exc:
        load_source(f"sqlite:///{tmp_path}/missing/dir/x.db")
    assert "could not read" in str(exc.value)


def test_redact_and_prepare_url():
    assert redact_url("postgresql://bob:s3cret@db/app") == "postgresql://bob:***@db/app"
    assert prepare_url("postgres://u@h/d").startswith("postgresql")


MODELS = textwrap.dedent(
    """
    from sqlalchemy import Column, ForeignKey, Integer, String, Text, Boolean, text
    from sqlalchemy.orm import declarative_base

    Base = declarative_base()

    class Author(Base):
        __tablename__ = "authors"
        id = Column(Integer, primary_key=True)
        name = Column(String(120), nullable=False, comment="Display name")
        email = Column(String(255), unique=True, doc="Contact address")

    class Post(Base):
        __tablename__ = "posts"
        id = Column(Integer, primary_key=True)
        author_id = Column(Integer, ForeignKey("authors.id", ondelete="SET NULL"), index=True)
        body = Column(Text)
        published = Column(Boolean, nullable=False, server_default=text("false"))
        status = Column(String(16), server_default="draft")
    """
)


def test_load_models(tmp_path, monkeypatch):
    (tmp_path / "blogmodels.py").write_text(MODELS)
    monkeypatch.chdir(tmp_path)
    schema = load_source("python:blogmodels:Base", dialect="postgresql")
    assert schema.dialect == "postgresql"
    authors = schema.get("authors")
    assert authors.column("id").autoincrement
    assert authors.column("name").comment == "Display name"
    assert authors.column("email").comment == "Contact address"
    assert authors.is_unique(["email"])
    posts = schema.get("posts")
    assert posts.foreign_keys[0].on_delete == "SET NULL"
    assert posts.column("published").default == "false"
    assert posts.column("status").default == "'draft'"
    assert [ix.columns for ix in posts.indexes] == [["author_id"]]


def test_load_models_auto_discovers_metadata(tmp_path, monkeypatch):
    (tmp_path / "blogmodels2.py").write_text(MODELS)
    monkeypatch.chdir(tmp_path)
    assert sorted(load_source("models:blogmodels2").tables) == ["authors", "posts"]


def test_load_models_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SourceError, match="could not import"):
        load_source("python:does_not_exist:Base")


def test_detect_kind(tmp_path):
    (tmp_path / "migrations").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    assert detect_kind("postgresql://localhost/app") == "database"
    assert detect_kind("sqlite:///x.db") == "database"
    assert detect_kind("python:app.models:Base") == "models"
    assert detect_kind("schema/schema.json") == "snapshot"
    assert detect_kind("schema.sql") == "sql"
    assert detect_kind(str(tmp_path / "migrations")) == "sql"
    with pytest.raises(SourceError, match="not found"):
        detect_kind(str(tmp_path / "nope"))
    with pytest.raises(SourceError, match="don't know"):
        detect_kind(str(tmp_path / "notes.txt"))
    assert describe_source("postgresql://a:b@h/d") == "postgresql://a:***@h/d"


def test_dialect_names():
    assert canonical_dialect("pg") == "postgresql"
    assert canonical_dialect("mariadb") == "mysql"
    assert canonical_dialect(None) is None
    assert sqlglot_dialect("postgresql") == "postgres"
    assert sqlglot_dialect("sqlserver") == "tsql"
    assert default_schema_for("postgres") == "public"
    with pytest.raises(ValueError):
        canonical_dialect("dbase")
