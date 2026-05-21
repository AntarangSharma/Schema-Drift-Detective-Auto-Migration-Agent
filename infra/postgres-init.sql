-- Initialize the two logical environments in a single Postgres instance.
-- source_raw  : the "source system" we monitor for drift
-- analytics   : the dbt-target "warehouse"
-- schema_drift: agent's own metadata (snapshots, drift_events, audit_log)

CREATE SCHEMA IF NOT EXISTS source_raw;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS schema_drift;

-- Agent metadata tables (created here as a convenience; production would
-- migrate these via Alembic). Keep schema in sync with src/schema_drift/models.py.

CREATE TABLE IF NOT EXISTS schema_drift.schema_snapshots (
    snapshot_id   TEXT        PRIMARY KEY,
    source_kind   TEXT        NOT NULL,
    source_identifier TEXT    NOT NULL,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_blob   JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_source
    ON schema_drift.schema_snapshots (source_identifier, captured_at DESC);

CREATE TABLE IF NOT EXISTS schema_drift.drift_events (
    id             TEXT        PRIMARY KEY,
    detected_at    TIMESTAMPTZ NOT NULL,
    source_system  TEXT        NOT NULL,
    source_identifier TEXT     NOT NULL,
    change_type    TEXT        NOT NULL,
    severity       TEXT        NOT NULL,
    payload        JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_events_detected
    ON schema_drift.drift_events (detected_at DESC);

CREATE TABLE IF NOT EXISTS schema_drift.audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT        NOT NULL,            -- agent component name
    action      TEXT        NOT NULL,
    target      TEXT,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_occurred
    ON schema_drift.audit_log (occurred_at DESC);
