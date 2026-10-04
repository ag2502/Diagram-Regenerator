ALTER TABLE tasks ADD COLUMN due_on DATE;
ALTER TABLE tasks ADD COLUMN priority SMALLINT NOT NULL DEFAULT 2;
ALTER TABLE organizations DROP COLUMN plan;

CREATE TABLE audit_events (
    id               BIGSERIAL PRIMARY KEY,
    organization_id  BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    actor_id         BIGINT REFERENCES users(id) ON DELETE SET NULL,
    action           TEXT NOT NULL,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_org_created ON audit_events (organization_id, created_at);
