-- ResolveOS schema (idempotent; safe to re-run)

CREATE TABLE IF NOT EXISTS api_keys (
    id          SERIAL PRIMARY KEY,
    principal   TEXT UNIQUE NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('agent','approver','operator','admin')),
    key_hash    TEXT UNIQUE NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- business domain ----------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    email          TEXT NOT NULL,
    tier           TEXT NOT NULL DEFAULT 'standard',
    fraud_flag     BOOLEAN NOT NULL DEFAULT FALSE,
    credit_balance NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL REFERENCES customers(id),
    status           TEXT NOT NULL CHECK (status IN ('placed','processing','shipped','delivered','cancelled')),
    total            NUMERIC(12,2) NOT NULL,
    refunded         NUMERIC(12,2) NOT NULL DEFAULT 0,
    placed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at     TIMESTAMPTZ,
    shipping_address TEXT NOT NULL,
    item_summary     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders(customer_id);

CREATE TABLE IF NOT EXISTS shipments (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    status       TEXT NOT NULL CHECK (status IN ('label_created','in_transit','delayed','lost','delivered')),
    carrier      TEXT NOT NULL DEFAULT 'ParcelCo',
    tracking     TEXT NOT NULL DEFAULT '',
    days_delayed INT  NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inventory (
    sku           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    on_hand       INT NOT NULL,
    reorder_point INT NOT NULL,
    unit_cost     NUMERIC(12,2) NOT NULL,
    discontinued  BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT PRIMARY KEY,
    email             TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    locked            BOOLEAN NOT NULL DEFAULT FALSE,
    identity_verified BOOLEAN NOT NULL DEFAULT FALSE,
    credential_resets INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','escalated')),
    resolution  TEXT,
    workflow_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);

-- ---- side-effect ledgers (one row per committed mutation) -----------------
CREATE TABLE IF NOT EXISTS refunds (
    id          TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    amount      NUMERIC(12,2) NOT NULL,
    reason      TEXT,
    workflow_id TEXT,
    approval_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS refunds_order_idx ON refunds(order_id);

CREATE TABLE IF NOT EXISTS credits (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    amount      NUMERIC(12,2) NOT NULL,
    reason      TEXT,
    workflow_id TEXT,
    approval_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reships (
    id          TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    reason      TEXT,
    workflow_id TEXT,
    approval_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id          TEXT PRIMARY KEY,
    sku         TEXT NOT NULL,
    quantity    INT NOT NULL,
    total       NUMERIC(12,2) NOT NULL,
    reason      TEXT,
    workflow_id TEXT,
    approval_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- workflow engine ------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflows (
    id               TEXT PRIMARY KEY,
    ticket_id        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','awaiting_approval','completed','escalated','failed')),
    submitted_by     TEXT NOT NULL,
    chaos            JSONB NOT NULL DEFAULT '{}'::jsonb,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    wake             BOOLEAN NOT NULL DEFAULT FALSE,
    attempts         INT NOT NULL DEFAULT 0,
    resumes          INT NOT NULL DEFAULT 0,
    result           JSONB,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS workflows_claim_idx ON workflows(status, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    action      TEXT NOT NULL,
    args        JSONB NOT NULL,
    action_hash TEXT NOT NULL,
    justification TEXT,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired')),
    requested_by TEXT NOT NULL,
    decided_by  TEXT,
    comment     TEXT,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approvals(status);
CREATE INDEX IF NOT EXISTS approvals_wf_idx ON approvals(workflow_id);

-- ---- reliability plumbing -------------------------------------------------
CREATE TABLE IF NOT EXISTS idempotency_keys (
    principal      TEXT NOT NULL,
    key            TEXT NOT NULL,
    request_hash   TEXT NOT NULL,
    status_code    INT  NOT NULL,
    response       JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, key)
);

CREATE TABLE IF NOT EXISTS chaos_hits (
    key  TEXT PRIMARY KEY,
    hits INT NOT NULL DEFAULT 0
);

-- Hash-chained, append-only audit log (one chain per workflow)
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    ts_iso       TEXT NOT NULL,
    workflow_id  TEXT NOT NULL,
    actor        TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload      JSONB NOT NULL,
    payload_text TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_wf_idx ON audit_log(workflow_id, id);

-- Make the audit log append-only at the database level
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- =====================================================================
-- v2: product layer (orgs, users, autonomy, rules, connectors, knowledge,
--     replies, regression cases, LLM usage, alerts)
-- =====================================================================
CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO orgs(id, name) VALUES ('default', 'Default Org') ON CONFLICT DO NOTHING;

ALTER TABLE api_keys  ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS flags JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE tickets   ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE tickets   ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'api';
ALTER TABLE tickets   ADD COLUMN IF NOT EXISTS reply_to TEXT;
ALTER TABLE tickets   ADD COLUMN IF NOT EXISTS guardrails JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS org_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS rule TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS base_effect TEXT;
CREATE INDEX IF NOT EXISTS workflows_org_idx ON workflows(org_id, created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('approver','operator','admin')),
    pw_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS org_settings (
    org_id TEXT PRIMARY KEY REFERENCES orgs(id),
    autonomy_mode TEXT NOT NULL DEFAULT 'autonomous' CHECK (autonomy_mode IN ('shadow','progressive','autonomous')),
    trust_threshold REAL NOT NULL DEFAULT 0.95,
    trust_min_samples INT NOT NULL DEFAULT 10,
    brand_tone TEXT NOT NULL DEFAULT 'warm, concise and professional',
    reply_approval BOOLEAN NOT NULL DEFAULT TRUE,
    handle_minutes REAL NOT NULL DEFAULT 8,
    hourly_cost REAL NOT NULL DEFAULT 30,
    slack_webhook TEXT,
    widget_token TEXT,
    alert_escalation_rate REAL NOT NULL DEFAULT 0.35,
    alert_backlog INT NOT NULL DEFAULT 10,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO org_settings(org_id) VALUES ('default') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS trust_stats (
    org_id TEXT NOT NULL, action TEXT NOT NULL,
    proposals INT NOT NULL DEFAULT 0, approved INT NOT NULL DEFAULT 0, rejected INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, action)
);

CREATE TABLE IF NOT EXISTS policy_rules (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, nl_text TEXT NOT NULL DEFAULT '',
    rule JSONB NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE, created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS connectors (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, base_url TEXT NOT NULL,
    auth_header TEXT, auth_value TEXT, operations JSONB NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS kb_docs (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, title TEXT NOT NULL, source TEXT,
    chunks INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id BIGSERIAL PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL, ord INT NOT NULL, text TEXT NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS kb_tsv_idx ON kb_chunks USING GIN (tsv);

CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, workflow_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
    channel TEXT NOT NULL, to_addr TEXT, draft TEXT NOT NULL, final TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','discarded')),
    citations JSONB NOT NULL DEFAULT '[]'::jsonb, model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), sent_at TIMESTAMPTZ, sent_by TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL, channel TEXT NOT NULL, to_addr TEXT,
    subject TEXT, body TEXT NOT NULL, ticket_id TEXT, status TEXT NOT NULL, error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_cases (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, source_workflow TEXT, reason TEXT,
    ticket JSONB NOT NULL, snapshot JSONB NOT NULL, expect JSONB NOT NULL,
    created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL DEFAULT 'default', workflow_id TEXT, purpose TEXT,
    provider TEXT, model TEXT, route_reason TEXT, input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0, cost_usd NUMERIC(14,6) NOT NULL DEFAULT 0, latency_s REAL,
    ok BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_usage_wf_idx ON llm_usage(workflow_id);

CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY, org_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ
);
