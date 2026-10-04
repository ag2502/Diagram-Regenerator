"""Shared fixtures-as-functions and a Mermaid erDiagram grammar check."""

from __future__ import annotations

import re

from diagram_regenerator.model import Column, ForeignKey, Index, Schema, Table


def shop_schema() -> Schema:
    schema = Schema(dialect="postgresql")
    schema.add_table(
        Table(
            name="customers",
            comment="People who buy things.",
            columns=[
                Column("id", "bigint", nullable=False, autoincrement=True),
                Column("email", "varchar(255)", nullable=False, comment='Login "email"'),
                Column("balance", "numeric(10,2)", default="0"),
                Column("kind", "enum('retail','wholesale')"),
                Column("deleted_at", "timestamptz", comment="[draft] Soft-delete time."),
            ],
            primary_key=["id"],
            indexes=[Index(["email"], unique=True, name="customers_email_key")],
        )
    )
    schema.add_table(
        Table(
            name="orders",
            columns=[
                Column("id", "bigint", nullable=False),
                Column("customer_id", "bigint", nullable=False),
                Column("status", "text", default="'new'"),
                Column("note | pipe", "text"),
            ],
            primary_key=["id"],
            foreign_keys=[ForeignKey(["customer_id"], "customers", ["id"], on_delete="CASCADE")],
            indexes=[Index(["status"], name="ix_orders_status")],
        )
    )
    schema.add_table(
        Table(
            name="order_items",
            columns=[
                Column("order_id", "bigint", nullable=False),
                Column("line", "integer", nullable=False),
                Column("sku", "text"),
            ],
            primary_key=["order_id", "line"],
            foreign_keys=[ForeignKey(["order_id"], "orders", ["id"])],
        )
    )
    schema.add_table(
        Table(
            name="invoices",
            schema="billing",
            columns=[
                Column("id", "integer", nullable=False),
                Column("order_id", "bigint"),
                Column("parent_id", "integer"),
            ],
            primary_key=["id"],
            foreign_keys=[
                ForeignKey(["order_id"], "orders", ["id"]),
                ForeignKey(["parent_id"], "billing.invoices", ["id"]),
            ],
            indexes=[Index(["order_id"], unique=True)],
        )
    )
    schema.enums["mood"] = ["sad", "ok"]
    return schema


_ENTITY = r"[A-Za-z_][A-Za-z0-9_\-]*"
_ATTR_WORD = r"[A-Za-z_*][A-Za-z0-9_\-\[\]\(\)]*"
_CARD_LEFT = r"(\|o|\|\||\}o|\}\|)"
_CARD_RIGHT = r"(o\||\|\||o\{|\|\{)"
_PATTERNS = [
    re.compile(rf"^{_ENTITY}$"),
    re.compile(rf"^{_ENTITY} \{{$"),
    re.compile(r"^\}$"),
    re.compile(rf'^{_ATTR_WORD} {_ATTR_WORD}( (PK|FK|UK)(, (PK|FK|UK))*)?( "[^"]*")?$'),
    re.compile(rf'^{_ENTITY} {_CARD_LEFT}(--|\.\.){_CARD_RIGHT} {_ENTITY} : "[^"]*"$'),
]


def assert_valid_mermaid_er(text: str) -> None:
    """Every line must match Mermaid's erDiagram grammar (as GitHub renders it)."""
    lines = text.strip().splitlines()
    if lines[0] == "---":
        end = lines.index("---", 1)
        lines = lines[end + 1 :]
    assert lines[0] == "erDiagram", lines[0]
    for raw in lines[1:]:
        line = raw.strip()
        assert any(p.match(line) for p in _PATTERNS), f"invalid Mermaid line: {raw!r}"
