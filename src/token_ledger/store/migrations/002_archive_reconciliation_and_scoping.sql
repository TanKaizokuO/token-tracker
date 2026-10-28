-- Migration 002_archive_reconciliation_and_scoping.sql
-- 1. Updates archive_partitions to allow NULL total_cost_usd (preserving unknown vs measured zero).
-- 2. Adds pricing_status, missingness_json, and coverage_json to support archive reconciliation.
-- 3. Adds structured scoping fields and foreign keys for scoped retention cascades:
--    - archive_partitions: account_alias, project_id (REFERENCES projects ON DELETE SET NULL)
--    - export_inventory: session_id (REFERENCES sessions ON DELETE SET NULL), project_id (REFERENCES projects ON DELETE SET NULL), account_alias
--    - alerts_and_reports: project_id (REFERENCES projects ON DELETE SET NULL), account_alias
-- 4. Preserves all existing rows, foreign keys, and indexes.

PRAGMA foreign_keys = OFF;

-- Recreate archive_partitions with nullable total_cost_usd and reconciliation/scoping columns
CREATE TABLE archive_partitions_v2 (
    partition_id TEXT PRIMARY KEY,
    partition_type TEXT NOT NULL,
    grain TEXT NOT NULL,
    source_name TEXT NOT NULL,
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL, -- NULL means unknown/unpriced; 0.0 is explicitly measured zero
    records_count INTEGER NOT NULL DEFAULT 0,
    pricing_status TEXT NOT NULL DEFAULT 'unpriced',
    missingness_json TEXT NOT NULL DEFAULT '{}',
    coverage_json TEXT NOT NULL DEFAULT '{}',
    is_reconciled INTEGER NOT NULL DEFAULT 0,
    is_live_replaced INTEGER NOT NULL DEFAULT 0,
    reconciliation_notes TEXT,
    account_alias TEXT NOT NULL DEFAULT 'default',
    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

INSERT INTO archive_partitions_v2 (
    partition_id, partition_type, grain, source_name, timezone_name,
    start_time, end_time, total_tokens, total_cost_usd, records_count,
    pricing_status, is_reconciled, is_live_replaced, reconciliation_notes,
    account_alias, project_id, payload_json, created_at
)
SELECT
    partition_id, partition_type, grain, source_name, timezone_name,
    start_time, end_time, total_tokens, total_cost_usd, records_count,
    'priced', is_reconciled, is_live_replaced, reconciliation_notes,
    'default', NULL, payload_json, created_at
FROM archive_partitions;

DROP TABLE archive_partitions;
ALTER TABLE archive_partitions_v2 RENAME TO archive_partitions;

CREATE INDEX IF NOT EXISTS idx_archive_span
    ON archive_partitions (start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_archive_project
    ON archive_partitions (project_id);
CREATE INDEX IF NOT EXISTS idx_archive_account
    ON archive_partitions (account_alias);

-- Update export_inventory with scoped retention foreign keys and account scoping
ALTER TABLE export_inventory ADD COLUMN session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL;
ALTER TABLE export_inventory ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE export_inventory ADD COLUMN account_alias TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_export_session
    ON export_inventory (session_id);
CREATE INDEX IF NOT EXISTS idx_export_project
    ON export_inventory (project_id);
CREATE INDEX IF NOT EXISTS idx_export_account
    ON export_inventory (account_alias);

-- Update alerts_and_reports with project scoping and account scoping
ALTER TABLE alerts_and_reports ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE alerts_and_reports ADD COLUMN account_alias TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_alerts_project
    ON alerts_and_reports (project_id);
CREATE INDEX IF NOT EXISTS idx_alerts_account
    ON alerts_and_reports (account_alias);

PRAGMA foreign_keys = ON;
