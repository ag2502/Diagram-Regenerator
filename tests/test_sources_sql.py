import sqlite3
import textwrap

import pytest

from diagram_regenerator.sources import load_source
from diagram_regenerator.sources.sql import (
    migration_order,
    parse_sql_path,
    replay,
    split_statements,
    up_statements,
)


def _replay(sql: str, dialect: str = "postgresql"):
    return replay([("test.sql", textwrap.dedent(sql))], dialect)


def _schema(sql: str, dialect: str = "postgresql"):
    return _replay(sql, dialect).schema


# ----------------------------------------------------------------------------- splitting


def test_split_respects_strings_comments_and_dollar_quotes():
    sql = textwrap.dedent(
        """
        -- a comment; with a semicolon
        CREATE TABLE a (note text DEFAULT 'x;y');
        /* block ; comment */
        CREATE FUNCTION f() RETURNS trigger AS $body$
        BEGIN NEW.x := 1; RETURN NEW; END;
        $body$ LANGUAGE plpgsql;
        CREATE TABLE "b;c" (id int);
        SELECT E'it\\'s; fine';
        """
    )
    statements = [text for _, text in split_statements(sql)]
    assert len(statements) == 4
    assert statements[0] == "CREATE TABLE a (note text DEFAULT 'x;y')"
    assert statements[1].startswith("CREATE FUNCTION") and statements[1].endswith("plpgsql")
    assert statements[2] == 'CREATE TABLE "b;c" (id int)'


def test_split_reports_line_numbers():
    sql = "CREATE TABLE a (id int);\n\n-- note\nCREATE TABLE b (\n id int\n);"
    assert [line for line, _ in split_statements(sql)] == [1, 4]


def test_split_mysql_delimiter_and_hash_comments():
    sql = textwrap.dedent(
        """
        # mysql comment;
        CREATE TABLE a (id int);
        DELIMITER //
        CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END //
        DELIMITER ;
        CREATE TABLE b (s varchar(10) DEFAULT 'it\\'s');
        """
    )
    statements = [text for _, text in split_statements(sql, "mysql")]
    assert statements[0] == "CREATE TABLE a (id int)"
    assert statements[1].startswith("CREATE PROCEDURE") and statements[1].endswith("END")
    assert statements[2].startswith("CREATE TABLE b")


def test_up_sections_dbmate_and_goose():
    dbmate = "-- migrate:up\nCREATE TABLE a (id int);\n-- migrate:down\nDROP TABLE a;\n"
    assert [s for _, s in up_statements(dbmate)] == ["CREATE TABLE a (id int)"]
    goose = textwrap.dedent(
        """
        -- +goose Up
        -- +goose StatementBegin
        CREATE TABLE b (id int);
        -- +goose StatementEnd
        CREATE INDEX ix ON b (id);
        -- +goose Down
        DROP TABLE b;
        """
    )
    assert [s for _, s in up_statements(goose)] == [
        "CREATE TABLE b (id int)",
        "CREATE INDEX ix ON b (id)",
    ]


# ----------------------------------------------------------------------------- ordering


def test_migration_order_conventions():
    files = [
        "V10__later.sql",
        "V2__second.sql",
        "V1__init.sql",
        "U2__undo.sql",
        "R__views.sql",
        "0002_add.down.sql",
        "0002_add.up.sql",
        "20240101_init/up.sql",
        "20240101_init/down.sql",
        "20240102_more/migration.sql",
        "x.mysql.up.sql",
        "x.postgres.up.sql",
        ".hidden/skip.sql",
    ]
    ordered = migration_order(files, "postgresql")
    assert ordered == [
        "0002_add.up.sql",
        "20240101_init/up.sql",
        "20240102_more/migration.sql",
        "V1__init.sql",
        "V2__second.sql",
        "V10__later.sql",
        "x.postgres.up.sql",
        "R__views.sql",
    ]
    assert "x.mysql.up.sql" in migration_order(files, "mysql")


# ----------------------------------------------------------------------------- PostgreSQL


def test_create_table_with_inline_and_table_constraints():
    schema = _schema(
        """
        CREATE TABLE public.orgs (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            email VARCHAR(255) NOT NULL UNIQUE,
            org_id BIGINT REFERENCES orgs ON DELETE CASCADE,
            settings jsonb DEFAULT '{}'::jsonb,
            price NUMERIC(10, 2) DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT users_pkey PRIMARY KEY (id),
            CONSTRAINT users_org_email UNIQUE (org_id, email),
            CHECK (price >= 0)
        );
        """
    )
    orgs = schema.get("orgs")
    assert orgs.column("id").type == "bigint" and orgs.column("id").autoincrement
    users = schema.get("users")
    assert users.primary_key == ["id"]
    assert users.column("id").default == "gen_random_uuid()"
    assert users.column("settings").default == "'{}'"
    assert users.column("price").type == "numeric(10,2)"
    assert users.column("created_at").default == "current_timestamp"
    assert users.is_unique(["email"]) and users.is_unique(["email", "org_id"])
    (fk,) = users.foreign_keys
    assert (fk.ref_table, fk.ref_columns, fk.on_delete, fk.name) == (
        "orgs",
        ["id"],
        "CASCADE",
        "users_org_id_fkey",
    )


def test_alter_table_variants():
    schema = _schema(
        """
        CREATE TABLE users (id serial PRIMARY KEY, email text, legacy int, nick text);
        ALTER TABLE users ADD COLUMN age INT NOT NULL DEFAULT 1, ADD COLUMN bio TEXT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS age INT;
        ALTER TABLE users DROP COLUMN legacy;
        ALTER TABLE users DROP COLUMN IF EXISTS never_existed;
        ALTER TABLE users ALTER COLUMN email TYPE VARCHAR(320);
        ALTER TABLE users ALTER COLUMN email SET NOT NULL;
        ALTER TABLE users ALTER COLUMN bio SET DEFAULT 'hi';
        ALTER TABLE users ALTER COLUMN bio DROP DEFAULT;
        ALTER TABLE users ALTER COLUMN age DROP NOT NULL;
        ALTER TABLE users RENAME COLUMN nick TO handle;
        ALTER TABLE users RENAME TO accounts;
        """
    )
    assert "users" not in schema.tables
    accounts = schema.get("accounts")
    assert [c.name for c in accounts.columns] == ["id", "email", "handle", "age", "bio"]
    email = accounts.column("email")
    assert (email.type, email.nullable) == ("varchar(320)", False)
    assert accounts.column("bio").default is None
    age = accounts.column("age")
    assert (age.nullable, age.default) == (True, "1")


def test_constraints_added_dropped_and_renamed():
    result = _replay(
        """
        CREATE TABLE orgs (id int PRIMARY KEY);
        CREATE TABLE users (id int PRIMARY KEY, org_id int, email text);
        ALTER TABLE users ADD CONSTRAINT fk_org FOREIGN KEY (org_id) REFERENCES orgs (id) ON DELETE SET NULL;
        ALTER TABLE users ADD CONSTRAINT u_email UNIQUE (email);
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_lower_email ON users USING btree (lower(email));
        CREATE INDEX ON users (org_id);
        ALTER TABLE users RENAME CONSTRAINT u_email TO users_email_key;
        ALTER INDEX ix_lower_email RENAME TO users_lower_email_idx;
        ALTER TABLE users DROP CONSTRAINT fk_org;
        ALTER TABLE orgs RENAME COLUMN id TO org_pk;
        """
    )
    assert result.warnings == []
    users = result.schema.get("users")
    assert users.foreign_keys == []
    names = {ix.name: (ix.columns, ix.unique) for ix in users.indexes}
    assert names == {
        "users_email_key": (["email"], True),
        "users_lower_email_idx": (["lower(email)"], True),
        "users_org_id_idx": (["org_id"], False),
    }


def test_drop_table_removes_dangling_foreign_keys():
    schema = _schema(
        """
        CREATE TABLE a (id int PRIMARY KEY);
        CREATE TABLE b (id int PRIMARY KEY, a_id int REFERENCES a(id));
        CREATE INDEX ix_b ON b (a_id);
        DROP INDEX IF EXISTS ix_b;
        DROP TABLE IF EXISTS a CASCADE;
        DROP TABLE IF EXISTS never_existed;
        """
    )
    assert list(schema.tables) == ["b"]
    assert schema.get("b").foreign_keys == [] and schema.get("b").indexes == []


def test_comments_and_enums():
    schema = _schema(
        """
        CREATE TYPE mood AS ENUM ('sad', 'ok');
        CREATE TABLE people (id int PRIMARY KEY, feeling mood, note text);
        COMMENT ON TABLE people IS 'Everyone';
        COMMENT ON COLUMN public.people.feeling IS 'How they feel';
        COMMENT ON COLUMN people.note IS 'temp';
        COMMENT ON COLUMN people.note IS NULL;
        ALTER TYPE mood ADD VALUE 'happy' AFTER 'ok';
        ALTER TYPE mood ADD VALUE IF NOT EXISTS 'meh' BEFORE 'ok';
        ALTER TYPE mood RENAME VALUE 'sad' TO 'blue';
        """
    )
    people = schema.get("people")
    assert people.comment == "Everyone"
    assert people.column("feeling").comment == "How they feel"
    assert people.column("note").comment is None
    assert schema.enums == {"mood": ["blue", "meh", "ok", "happy"]}


def test_prisma_enum_swap_follows_type_rename():
    schema = _schema(
        """
        CREATE TYPE "Role" AS ENUM ('USER', 'ADMIN');
        CREATE TABLE "User" ("id" TEXT NOT NULL, "role" "Role" NOT NULL DEFAULT 'USER',
            CONSTRAINT "User_pkey" PRIMARY KEY ("id"));
        BEGIN;
        CREATE TYPE "Role_new" AS ENUM ('USER', 'ADMIN', 'OWNER');
        ALTER TABLE "User" ALTER COLUMN "role" TYPE "Role_new" USING ("role"::text::"Role_new");
        ALTER TYPE "Role" RENAME TO "Role_old";
        ALTER TYPE "Role_new" RENAME TO "Role";
        DROP TYPE "Role_old";
        COMMIT;
        """
    )
    user = schema.get("User")
    assert user.primary_key == ["id"]
    assert user.column("role").type == "role"
    assert schema.enums == {"Role": ["USER", "ADMIN", "OWNER"]}


def test_unparseable_and_out_of_order_statements_warn():
    result = _replay(
        """
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
        CREATE OR REPLACE FUNCTION touch() RETURNS trigger AS $$ BEGIN RETURN NEW; END $$ LANGUAGE plpgsql;
        ALTER TABLE ghosts ADD COLUMN x int;
        DO $$ BEGIN CREATE TABLE sneaky (id int); END $$;
        CREATE TABLE copy AS SELECT 1 AS x;
        CREATE TABLE ok (id int);
        """
    )
    assert list(result.schema.tables) == ["ok"]
    joined = "\n".join(result.warnings)
    assert "ghosts" in joined and "test.sql:4" in joined
    assert "DO block" in joined
    assert "AS SELECT" in joined
    assert len(result.warnings) == 3


def test_identifier_case_folding():
    schema = _schema('CREATE TABLE Users (ID int PRIMARY KEY, "MixedCase" text);')
    assert [c.name for c in schema.get("users").columns] == ["id", "MixedCase"]


def test_schema_qualified_tables():
    schema = _schema(
        """
        CREATE TABLE billing.invoices (id int PRIMARY KEY, org_id int REFERENCES public.orgs(id));
        CREATE TABLE public.orgs (id int PRIMARY KEY);
        """
    )
    assert sorted(schema.tables) == ["billing.invoices", "orgs"]
    assert schema.get("billing.invoices").foreign_keys[0].ref_table == "orgs"


# ----------------------------------------------------------------------------- MySQL / SQLite


def test_mysql_dump_style_ddl():
    schema = _schema(
        """
        CREATE TABLE `customers` (`id` int(11) NOT NULL AUTO_INCREMENT, PRIMARY KEY (`id`)) ENGINE=InnoDB;
        CREATE TABLE `orders` (
          `id` int(11) NOT NULL AUTO_INCREMENT,
          `customer_id` int(11) NOT NULL COMMENT 'buyer',
          `state` enum('new','paid') DEFAULT 'new',
          `paid` tinyint(1) NOT NULL DEFAULT '0',
          `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`id`),
          KEY `idx_customer` (`customer_id`),
          UNIQUE KEY `uk_state` (`state`, `customer_id`),
          CONSTRAINT `fk_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='All orders';
        ALTER TABLE orders MODIFY COLUMN state VARCHAR(10) NOT NULL;
        ALTER TABLE orders CHANGE COLUMN paid is_paid tinyint(1) NOT NULL DEFAULT 0;
        ALTER TABLE orders ADD COLUMN note TEXT AFTER customer_id;
        ALTER TABLE orders ADD INDEX idx_note (note(20));
        RENAME TABLE orders TO purchases;
        """,
        "mysql",
    )
    purchases = schema.get("purchases")
    assert purchases.comment == "All orders"
    assert [c.name for c in purchases.columns] == [
        "id",
        "customer_id",
        "note",
        "state",
        "is_paid",
        "updated_at",
    ]
    assert purchases.column("id").autoincrement and purchases.column("id").type == "integer"
    assert purchases.column("customer_id").comment == "buyer"
    assert purchases.column("state").type == "varchar(10)"
    assert purchases.column("is_paid").type == "boolean"
    assert purchases.column("updated_at").default == "current_timestamp"
    assert purchases.foreign_keys[0].ref_table == "customers"
    assert purchases.is_unique(["state", "customer_id"])


SQLITE_DDL = """
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
) WITHOUT ROWID;
CREATE INDEX ix_orders_status ON orders (status);
"""


def test_parsed_sqlite_matches_reflected_sqlite(tmp_path):
    db = tmp_path / "shop.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(SQLITE_DDL)
    reflected = load_source(f"sqlite:///{db}")
    parsed = _schema(SQLITE_DDL, "sqlite")
    for schema in (reflected, parsed):
        for table in schema.tables.values():
            for column in table.columns:
                column.autoincrement = False  # SQLite reflection can't see AUTOINCREMENT
    assert parsed.to_dict()["tables"] == reflected.to_dict()["tables"]


# ----------------------------------------------------------------------------- directories


def test_load_migrations_directory(tmp_path):
    root = tmp_path / "migrations"
    (root / "20240101000000_init").mkdir(parents=True)
    (root / "20240101000000_init" / "migration.sql").write_text(
        "CREATE TABLE users (id int PRIMARY KEY, email text);"
    )
    (root / "20240301000000_posts").mkdir()
    (root / "20240301000000_posts" / "migration.sql").write_text(
        "CREATE TABLE posts (id int PRIMARY KEY, user_id int REFERENCES users(id));"
    )
    (root / "20240301000000_posts" / "down.sql").write_text("DROP TABLE posts;")
    (root / "migration_lock.toml").write_text('provider = "postgresql"')
    result = parse_sql_path(root, "postgresql")
    assert result.files == [
        "20240101000000_init/migration.sql",
        "20240301000000_posts/migration.sql",
    ]
    assert sorted(result.schema.tables) == ["posts", "users"]
    assert sorted(load_source(str(root)).tables) == ["posts", "users"]


def test_load_single_file(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text("CREATE TABLE a (id int PRIMARY KEY);")
    assert list(load_source(str(path)).tables) == ["a"]


def test_missing_path():
    with pytest.raises(FileNotFoundError):
        parse_sql_path("/definitely/not/here")
