import json

import pytest

from diagram_regenerator.model import Column, ForeignKey, Index, Schema, Table
from diagram_regenerator.normalize import (
    is_sequence_default,
    normalize_default,
    normalize_type,
    split_serial,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INTEGER", "integer"),
        ("int4", "integer"),
        ("INT(11)", "integer"),
        ("int(10) unsigned", "integer unsigned"),
        ("BIGINT", "bigint"),
        ("int8", "bigint"),
        ("BOOL", "boolean"),
        ("TINYINT(1)", "boolean"),
        ("VARCHAR(255)", "varchar(255)"),
        ("character varying(255)", "varchar(255)"),
        ('VARCHAR(255) COLLATE "C"', "varchar(255)"),
        ("varchar(191) CHARACTER SET utf8mb4", "varchar(191)"),
        ("NUMERIC(10, 2)", "numeric(10,2)"),
        ("DECIMAL(10,2)", "numeric(10,2)"),
        ("TIMESTAMP WITHOUT TIME ZONE", "timestamp"),
        ("TIMESTAMP WITH TIME ZONE", "timestamptz"),
        ("timestamp(3) without time zone", "timestamp(3)"),
        ("TIMESTAMPTZ", "timestamptz"),
        ("DOUBLE PRECISION", "double"),
        ("float8", "double"),
        ("INTEGER[]", "integer[]"),
        ("ARRAY<TEXT>", "text[]"),
        ("bpchar(2)", "char(2)"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_normalize_type(raw, expected):
    assert normalize_type(raw) == expected


def test_split_serial():
    assert split_serial("SERIAL") == ("integer", True)
    assert split_serial("bigserial") == ("bigint", True)
    assert split_serial("text") == ("text", False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("NULL", None),
        ("NULL::character varying", None),
        ("'draft'::character varying", "'draft'"),
        ("'a::b'::text", "'a::b'"),
        ("now()", "current_timestamp"),
        ("CURRENT_TIMESTAMP", "current_timestamp"),
        ("(CURRENT_TIMESTAMP)", "current_timestamp"),
        ("((0))", "0"),
        ("'0'", "0"),
        ("'It''s'", "'It''s'"),
        ("gen_random_uuid()", "gen_random_uuid()"),
        ("TRUE", "true"),
        ("'0.00'", "0"),
        ("1.50", "1.5"),
        ("-3.0", "-3"),
        ("'007'", "'007'"),
    ],
)
def test_normalize_default(raw, expected):
    assert normalize_default(raw) == expected


def test_is_sequence_default():
    assert is_sequence_default("nextval('users_id_seq'::regclass)")
    assert not is_sequence_default("0")
    assert not is_sequence_default(None)


def _shop() -> Schema:
    schema = Schema(dialect="postgresql")
    schema.add_table(
        Table(
            name="users",
            columns=[
                Column("id", "bigint", nullable=False, autoincrement=True),
                Column("email", "varchar(255)", nullable=False, comment="Login email"),
            ],
            primary_key=["id"],
            indexes=[Index(["email"], unique=True, name="users_email_key")],
        )
    )
    schema.add_table(
        Table(
            name="orders",
            columns=[
                Column("id", "bigint", nullable=False),
                Column("user_id", "bigint", nullable=False),
                Column("status", "text", default="'new'"),
            ],
            primary_key=["id"],
            foreign_keys=[ForeignKey(["user_id"], "users", ["id"], on_delete="CASCADE")],
        )
    )
    return schema


def test_snapshot_round_trip(tmp_path):
    schema = _shop()
    path = schema.save(tmp_path / "nested" / "schema.json")
    loaded = Schema.load(path)
    assert loaded.to_json() == schema.to_json()
    assert loaded.fingerprint() == schema.fingerprint()


def test_snapshot_is_deterministic_and_sorted():
    data = json.loads(_shop().to_json())
    assert data["format"] == "diagram-regenerator/schema@1"
    assert list(data["tables"]) == ["orders", "users"]
    # Optional fields stay out of the file so diffs of the snapshot stay small.
    assert data["tables"]["orders"]["columns"][0] == {
        "name": "id",
        "type": "bigint",
        "nullable": False,
    }


def test_from_dict_rejects_foreign_json():
    with pytest.raises(ValueError):
        Schema.from_dict({"format": "something-else"})


def test_table_helpers():
    schema = _shop()
    users = schema.get("users")
    orders = schema.get("orders")
    assert users.is_unique(["email"])
    assert users.is_unique(["id"])
    assert not orders.is_unique(["user_id"])
    assert orders.foreign_key_for("user_id").ref_table == "users"
    assert [t.name for t, _ in schema.referencing("users")] == ["orders"]
    assert schema.column_count == 5


def test_add_index_skips_duplicates_and_pk():
    table = Table("t", columns=[Column("id", "integer")], primary_key=["id"])
    table.add_index(Index(["id"], unique=True, name="t_pkey"))
    table.add_index(Index(["id"], unique=False, name="a"))
    table.add_index(Index(["id"], unique=False, name="b"))
    assert [ix.name for ix in table.indexes] == ["a"]


def test_filtered_globs_and_bookkeeping():
    schema = _shop()
    schema.add_table(Table("alembic_version"))
    schema.add_table(Table("audit_log", schema="ops"))
    assert sorted(schema.filtered().tables) == ["ops.audit_log", "orders", "users"]
    assert sorted(schema.filtered(exclude=["audit_*"]).tables) == ["orders", "users"]
    assert sorted(schema.filtered(include=["ops.*"]).tables) == ["ops.audit_log"]
    assert "alembic_version" in schema.filtered(skip_bookkeeping=False).tables


def test_resolve_by_bare_name():
    schema = Schema()
    schema.add_table(Table("events", schema="analytics"))
    assert schema.resolve("events").key == "analytics.events"
    assert schema.resolve("missing") is None
