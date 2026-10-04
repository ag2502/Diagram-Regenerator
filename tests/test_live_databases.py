"""Parsed migrations must match what a real database reports after running them.

Runs against PostgreSQL from DR_TEST_POSTGRES_URL (or an embedded server when
the ``pgserver`` package is installed) and MySQL from DR_TEST_MYSQL_URL; each
test is skipped when its server isn't available. CI provides both.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from diagram_regenerator.cli import main
from diagram_regenerator.describe import fetch_samples
from diagram_regenerator.diff import diff_schemas
from diagram_regenerator.report import format_text
from diagram_regenerator.sources import load_source
from diagram_regenerator.sources.sql import replay, split_statements
from diagram_regenerator.sources.sqla import prepare_url

POSTGRES_MIGRATIONS = {
    "V1__init.sql": """
        CREATE TYPE order_status AS ENUM ('pending', 'paid', 'shipped');
        CREATE TABLE customers (
            id BIGSERIAL PRIMARY KEY,
            email VARCHAR(255) NOT NULL UNIQUE,
            full_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            settings JSONB NOT NULL DEFAULT '{}'::jsonb,
            public_id UUID NOT NULL DEFAULT gen_random_uuid()
        );
        COMMENT ON TABLE customers IS 'People who buy things';
        COMMENT ON COLUMN customers.email IS 'Login address';
        CREATE TABLE orders (
            id BIGSERIAL PRIMARY KEY,
            customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            status order_status NOT NULL DEFAULT 'pending',
            total NUMERIC(10, 2) NOT NULL DEFAULT 0,
            placed_at TIMESTAMP(3),
            tags TEXT[]
        );
        CREATE INDEX ix_orders_customer ON orders (customer_id);
        CREATE OR REPLACE FUNCTION touch() RETURNS trigger AS $$
        BEGIN NEW.placed_at := now(); RETURN NEW; END;
        $$ LANGUAGE plpgsql;
    """,
    "V2__items_and_billing.sql": """
        ALTER TABLE customers ADD COLUMN phone VARCHAR(32);
        ALTER TABLE customers RENAME COLUMN full_name TO name;
        ALTER TABLE orders ALTER COLUMN total TYPE NUMERIC(12, 2);
        ALTER TYPE order_status ADD VALUE 'refunded';
        CREATE TABLE order_items (
            order_id BIGINT NOT NULL REFERENCES orders(id),
            line INTEGER NOT NULL,
            sku TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
            PRIMARY KEY (order_id, line)
        );
        CREATE UNIQUE INDEX ux_items_sku ON order_items (order_id, sku);
        CREATE SCHEMA billing;
        CREATE TABLE billing.invoices (
            id SERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL REFERENCES public.orders(id),
            amount NUMERIC(10, 2)
        );
        ALTER TABLE orders DROP CONSTRAINT orders_customer_id_fkey;
        ALTER TABLE orders ADD CONSTRAINT orders_customer_fk FOREIGN KEY (customer_id)
            REFERENCES customers(id) ON DELETE RESTRICT;
    """,
}

MYSQL_MIGRATIONS = {
    "001_init.up.sql": """
        CREATE TABLE customers (
            id INT NOT NULL AUTO_INCREMENT,
            email VARCHAR(255) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uk_email (email)
        ) ENGINE=InnoDB COMMENT='People who buy things';
        CREATE TABLE orders (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            customer_id INT NOT NULL,
            status ENUM('pending', 'paid') NOT NULL DEFAULT 'pending',
            total DECIMAL(10, 2) NOT NULL DEFAULT 0,
            note TEXT,
            CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """,
    "002_more.up.sql": """
        ALTER TABLE orders ADD COLUMN shipped_at DATETIME NULL AFTER status;
        ALTER TABLE orders MODIFY COLUMN note VARCHAR(500) NULL;
        ALTER TABLE customers CHANGE COLUMN is_active active BOOLEAN NOT NULL DEFAULT TRUE;
        CREATE INDEX ix_orders_status ON orders (status);
    """,
    "002_more.down.sql": "DROP INDEX ix_orders_status ON orders;",
}


def _unavailable(reason: str):
    """Skip locally; fail in CI (DR_REQUIRE_LIVE_DB=1) so a dead service can't pass silently."""
    if os.environ.get("DR_REQUIRE_LIVE_DB"):
        pytest.fail(f"live database required but unavailable: {reason}")
    pytest.skip(reason)


def _scratch_database(server_url: str, dialect: str):
    """Create an empty database on the server; returns (url, drop callback)."""
    name = f"dr_{uuid.uuid4().hex[:10]}"
    admin = create_engine(prepare_url(server_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(prepare_url(server_url)).set(database=name).render_as_string(hide_password=False)

    def drop() -> None:
        with admin.connect() as connection:
            if dialect == "postgresql":
                connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            else:
                connection.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        admin.dispose()

    return url, drop


def _apply(url: str, migrations: dict[str, str], dialect: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        for name in sorted(migrations):
            if ".down." in name:
                continue
            for _, statement in split_statements(migrations[name], dialect):
                connection.exec_driver_sql(statement)
    engine.dispose()


def _assert_same(parsed, reflected):
    diff = diff_schemas(parsed, reflected)
    assert not diff.has_changes, "parsed and live schemas differ:\n" + format_text(diff)


@pytest.fixture(scope="module")
def postgres_server(tmp_path_factory):
    url = os.environ.get("DR_TEST_POSTGRES_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        _unavailable("set DR_TEST_POSTGRES_URL or install pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
def postgres_db(postgres_server):
    url, drop = _scratch_database(postgres_server, "postgresql")
    yield url
    drop()


@pytest.fixture
def mysql_db():
    url = os.environ.get("DR_TEST_MYSQL_URL")
    if not url:
        _unavailable("set DR_TEST_MYSQL_URL to run MySQL tests")
    url, drop = _scratch_database(url, "mysql")
    yield url
    drop()


@pytest.mark.postgres
def test_postgres_migrations_match_live_database(postgres_db, tmp_path):
    _apply(postgres_db, POSTGRES_MIGRATIONS, "postgresql")
    parsed = replay(sorted(POSTGRES_MIGRATIONS.items()), "postgresql").schema
    reflected = load_source(postgres_db, schemas=["public", "billing"])

    assert sorted(reflected.tables) == ["billing.invoices", "customers", "order_items", "orders"]
    assert reflected.enums == {"order_status": ["pending", "paid", "shipped", "refunded"]}
    _assert_same(parsed, reflected)

    customers = reflected.get("customers")
    assert customers.comment == "People who buy things"
    assert customers.column("id").autoincrement and customers.column("id").default is None
    assert customers.column("settings").default == "'{}'"
    assert reflected.get("orders").column("tags").type == "text[]"
    assert reflected.get("billing.invoices").foreign_keys[0].ref_table == "orders"


@pytest.mark.postgres
def test_postgres_drift_and_samples_end_to_end(postgres_server, tmp_path, monkeypatch, capsys):
    current, drop_current = _scratch_database(postgres_server, "postgresql")
    behind, drop_behind = _scratch_database(postgres_server, "postgresql")
    try:
        _apply(current, POSTGRES_MIGRATIONS, "postgresql")
        _apply(behind, {"V1__init.sql": POSTGRES_MIGRATIONS["V1__init.sql"]}, "postgresql")
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        for name, sql in POSTGRES_MIGRATIONS.items():
            (migrations / name).write_text(sql)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "diagram-regen.toml").write_text(
            'source = "migrations"\nschemas = ["public", "billing"]\n'
            f'[environments]\ncurrent = "{current}"\nbehind = "{behind}"\n'
        )
        assert main(["drift", "--no-color"]) == 1
        out = capsys.readouterr().out
        assert "1 of 2 environments differ" in out
        assert "order_items" in out and "missing" in out

        with create_engine(current).begin() as connection:
            connection.execute(
                text("INSERT INTO customers (email, name) VALUES ('jane@example.com', 'Jane')")
            )
        rows = fetch_samples(current, load_source(current).get("customers"), 5)
        assert rows[0]["email"] == "<email>" and rows[0]["name"] == "Jane"
    finally:
        drop_current()
        drop_behind()


@pytest.mark.mysql
def test_mysql_migrations_match_live_database(mysql_db, tmp_path):
    _apply(mysql_db, MYSQL_MIGRATIONS, "mysql")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name, sql in MYSQL_MIGRATIONS.items():
        (migrations / name).write_text(sql)
    parsed = load_source(str(migrations), dialect="mysql")
    reflected = load_source(mysql_db)
    assert sorted(reflected.tables) == ["customers", "orders"]
    _assert_same(parsed, reflected)
    orders = reflected.get("orders")
    assert orders.column("id").autoincrement
    assert [c.name for c in orders.columns][:4] == ["id", "customer_id", "status", "shipped_at"]
