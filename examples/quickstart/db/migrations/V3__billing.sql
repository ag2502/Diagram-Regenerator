CREATE TABLE subscriptions (
    id                  BIGSERIAL PRIMARY KEY,
    organization_id     BIGINT NOT NULL UNIQUE REFERENCES organizations(id),
    stripe_customer_id  TEXT NOT NULL,
    seats               INTEGER NOT NULL DEFAULT 1,
    renews_on           DATE
);

CREATE TABLE invoices (
    id               BIGSERIAL PRIMARY KEY,
    subscription_id  BIGINT NOT NULL REFERENCES subscriptions(id),
    amount_cents     INTEGER NOT NULL,
    currency         CHAR(3) NOT NULL DEFAULT 'USD',
    issued_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at          TIMESTAMPTZ
);
COMMENT ON TABLE invoices IS 'One row per billing period, mirrored from Stripe.';
