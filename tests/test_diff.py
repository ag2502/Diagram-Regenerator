import json

import pytest
from helpers import assert_valid_mermaid_er

from diagram_regenerator.diff import diff_schemas, is_widening
from diagram_regenerator.report import (
    COMMENT_MARKER,
    format_json,
    format_markdown,
    format_text,
    render_diff_mermaid,
)
from diagram_regenerator.sources.sql import replay

BEFORE = """
CREATE TABLE customers (id bigint PRIMARY KEY, email varchar(100) NOT NULL, nickname text);
CREATE TABLE orders (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    status varchar(20) NOT NULL DEFAULT 'new',
    legacy_ref text,
    total numeric(10,2),
    shipped boolean
);
CREATE INDEX ix_orders_status ON orders (status);
CREATE TABLE audit_log (id bigint PRIMARY KEY, payload jsonb);
CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy');
"""

AFTER = """
CREATE TABLE customers (id bigint PRIMARY KEY, email varchar(255) NOT NULL UNIQUE, handle text);
COMMENT ON COLUMN customers.email IS 'Login email';
CREATE TABLE orders (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    status varchar(20) NOT NULL DEFAULT 'pending',
    total numeric(8,2),
    shipped boolean NOT NULL,
    discount_cents integer,
    region text NOT NULL
);
CREATE TABLE coupons (id bigint PRIMARY KEY, order_id bigint REFERENCES orders(id), code text);
CREATE TABLE events (id bigint PRIMARY KEY, payload jsonb);
CREATE TYPE mood AS ENUM ('sad', 'ok');
"""


def _diff(before=BEFORE, after=AFTER, **kwargs):
    old = replay([("before.sql", before)]).schema
    new = replay([("after.sql", after)]).schema
    return diff_schemas(old, new, **kwargs)


def _kinds(diff):
    return {(c.kind, c.target): c.severity for c in diff.changes}


def test_classifies_every_change():
    kinds = _kinds(_diff())
    assert kinds == {
        ("table_added", "coupons"): "safe",
        ("table_renamed", "events"): "breaking",
        ("column_renamed", "customers.handle"): "breaking",
        ("column_type_changed", "customers.email"): "safe",
        ("column_comment_changed", "customers.email"): "info",
        ("index_added", "customers"): "warning",
        ("column_removed", "orders.legacy_ref"): "breaking",
        ("column_added", "orders.discount_cents"): "safe",
        ("column_added", "orders.region"): "breaking",
        ("column_type_changed", "orders.total"): "breaking",
        ("column_not_null", "orders.shipped"): "breaking",
        ("column_default_changed", "orders.status"): "info",
        ("foreign_key_action_changed", "orders.customer_id"): "warning",
        ("index_removed", "orders"): "warning",
        ("enum_changed", ""): "breaking",
    }


def test_summary_counts_and_thresholds():
    diff = _diff()
    assert diff.counts() == {"breaking": 7, "warning": 3, "safe": 3, "info": 2}
    assert diff.worst() == "breaking"
    assert diff.at_least("warning") and diff.at_least("breaking")
    assert diff.summary() == "15 changes across 4 tables (7 breaking, 3 warning, 3 safe, 2 info)"
    assert diff.changed_tables() == ["coupons", "customers", "events", "orders"]


def test_no_changes():
    diff = _diff(BEFORE, BEFORE)
    assert not diff.has_changes and diff.worst() is None and not diff.at_least("info")
    assert format_text(diff) == "No schema changes.\n"
    assert "No schema changes. ✅" in format_markdown(diff)


def test_ignore_options():
    kinds = _kinds(_diff(ignore=["comments", "defaults", "indexes", "foreign_keys", "enums"]))
    assert not any(kind.startswith(("index", "foreign_key", "enum")) for kind, _ in kinds)
    assert ("column_comment_changed", "customers.email") not in kinds
    with pytest.raises(ValueError):
        _diff(ignore=["everything"])


def test_column_order_change_is_info():
    diff = _diff("CREATE TABLE t (a int, b int);", "CREATE TABLE t (b int, a int);")
    assert _kinds(diff) == {("column_order_changed", "t"): "info"}
    assert not _diff(
        "CREATE TABLE t (a int, b int);", "CREATE TABLE t (b int, a int);", ignore=["column_order"]
    ).has_changes


def test_ambiguous_renames_are_not_guessed():
    diff = _diff("CREATE TABLE t (a text, b text);", "CREATE TABLE t (c text, d text);")
    assert {c.kind for c in diff.changes} == {"column_removed", "column_added"}


def test_primary_key_and_nullable_changes():
    diff = _diff(
        "CREATE TABLE t (id int PRIMARY KEY, x int NOT NULL);",
        "CREATE TABLE t (id int, x int, PRIMARY KEY (id, x));",
    )
    kinds = _kinds(diff)
    assert kinds[("primary_key_changed", "t")] == "breaking"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("integer", "bigint", True),
        ("bigint", "integer", False),
        ("smallint", "numeric", True),
        ("real", "double", True),
        ("varchar(20)", "varchar(50)", True),
        ("varchar(50)", "varchar(20)", False),
        ("varchar(20)", "text", True),
        ("char(2)", "varchar", True),
        ("text", "varchar(10)", False),
        ("numeric(10,2)", "numeric(12,2)", True),
        ("numeric(10,2)", "numeric(10,4)", False),
        ("numeric(10,2)", "numeric", True),
        ("numeric", "numeric(10,2)", False),
        ("timestamp(3)", "timestamp", True),
        ("timestamp", "timestamptz", False),
        ("integer", "text", False),
    ],
)
def test_is_widening(old, new, expected):
    assert is_widening(old, new) is expected


def test_text_report():
    text = format_text(_diff())
    assert "orders\n" in text
    assert "  - Column orders.legacy_ref removed: was text  [BREAKING]" in text
    assert "      its data is dropped and queries using it fail" in text
    assert "  ~ Type of customers.email changed: varchar(100) → varchar(255)  [safe]" in text
    assert text.rstrip().endswith("(7 breaking, 3 warning, 3 safe, 2 info)")
    assert "\033[31;1mBREAKING" in format_text(_diff(), color=True)


def test_markdown_report():
    text = format_markdown(_diff(), footer="_footer_")
    assert text.startswith(COMMENT_MARKER)
    assert (
        "**15 changes across 4 tables** · 🔴 7 breaking · 🟠 3 warning · 🟢 3 safe · ⚪ 2 info"
        in text
    )
    assert "> [!WARNING]" in text
    assert "| 🔴 | Column `orders.legacy_ref` removed | was text<br>_its data is dropped" in text
    assert "```mermaid\nerDiagram" in text
    assert text.rstrip().endswith("_footer_")
    assert "```mermaid" not in format_markdown(_diff(), diagram=False)


def test_visual_diff_marks_changes():
    text = render_diff_mermaid(_diff())
    assert_valid_mermaid_er(text)
    assert 'integer discount_cents "🟢 added"' in text
    assert 'text legacy_ref "🔴 removed"' in text
    assert 'numeric(8_2) total "🟡 was numeric(10,2)"' in text
    assert 'text handle "🟡 renamed from nickname"' in text
    assert "nickname" not in text.split("renamed from nickname")[1]
    assert 'bigint id PK "🟢 new table"' in text  # coupons
    assert 'orders |o..o{ coupons : "order_id"' in text


def test_visual_diff_removed_table_and_foreign_keys():
    diff = _diff(
        "CREATE TABLE a (id int PRIMARY KEY); CREATE TABLE b (id int PRIMARY KEY, a_id int REFERENCES a(id)); CREATE TABLE gone (x int);",
        "CREATE TABLE a (id int PRIMARY KEY); CREATE TABLE b (id int PRIMARY KEY, a_id int); CREATE TABLE c (id int PRIMARY KEY, b_id int REFERENCES b(id));",
    )
    text = render_diff_mermaid(diff)
    assert_valid_mermaid_er(text)
    assert 'integer x "🔴 removed table"' in text
    assert 'a |o..o{ b : "🔴 a_id"' in text
    assert 'b |o..o{ c : "b_id"' in text


def test_json_report():
    data = json.loads(format_json(_diff()))
    assert data["counts"]["breaking"] == 7
    assert data["changes"][0]["kind"] == "table_added"
    assert {"old_fingerprint", "new_fingerprint", "summary"} <= set(data)
