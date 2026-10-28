-- Migration 001_initial.sql
-- Comprehensive canonical schema for Token Ledger SQLite storage engine.
-- Implements all minimum entity fields from TOKEN_LEDGER_REBUILD_PLAN.md:
-- 1. Source observations & ingestion checkpoints
-- 2. Canonical requests, identity scoping & observation links
-- 3. Normalized disjoint billable usage counters with raw reported semantics
-- 4. Sessions, projects, prompts, subagents, and trace events with parent links
-- 5. Coverage and archive partitions distinct from live observations
-- 6. Dated pricing versions, rate cards, historical FX, and billing quotas
-- 7. Manual outcomes, privacy opt-ins, views, layouts, alerts, reports, and export inventory
-- 8. Foreign key integrity and cascading deletion on owning parents

-- -----------------------------------------------------------------------------
-- 0. Schema Migration Tracker
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- 1. Ingestion Checkpoints & Source Observations
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_checkpoints (
    source_adapter TEXT NOT NULL,
    origin_harness TEXT NOT NULL,
    account_alias TEXT NOT NULL DEFAULT 'default',
    file_locator TEXT NOT NULL,
    file_generation TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_mtime TEXT,
    last_byte_offset INTEGER NOT NULL DEFAULT 0,
    last_line_index INTEGER NOT NULL DEFAULT 0,
    last_scan_at TEXT NOT NULL,
    last_success_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (source_adapter, file_locator)
);

CREATE TABLE IF NOT EXISTS source_observations (
    observation_id TEXT PRIMARY KEY,
    source_adapter TEXT NOT NULL,
    origin_harness TEXT NOT NULL,
    account_alias TEXT NOT NULL DEFAULT 'default',
    file_locator TEXT NOT NULL,
    file_generation TEXT,
    line_index INTEGER,
    event_id TEXT,
    observed_at TEXT NOT NULL,
    raw_timestamp TEXT,
    parser_version TEXT NOT NULL,
    raw_counters_json TEXT NOT NULL DEFAULT '{}',
    source_reported_total INTEGER,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    parse_status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_adapter_loc
    ON source_observations (source_adapter, file_locator, line_index);
CREATE INDEX IF NOT EXISTS idx_obs_time
    ON source_observations (observed_at);

-- -----------------------------------------------------------------------------
-- 2. Projects, Sessions, Prompts
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_alias TEXT,
    account_alias TEXT NOT NULL DEFAULT 'default',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    origin_harness TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    parent_session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    account_alias TEXT NOT NULL DEFAULT 'default',
    is_subagent INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER,
    active_duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions (project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_time
    ON sessions (started_at);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    prompt_group TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_prompts_session
    ON prompts (session_id);

-- -----------------------------------------------------------------------------
-- 3. Pricing Versions, Rates Cards, and Historical FX
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_versions (
    version_id TEXT PRIMARY KEY,
    pricing_hash TEXT NOT NULL,
    currency_code TEXT NOT NULL DEFAULT 'USD',
    effective_start TEXT NOT NULL,
    effective_end TEXT,
    source_provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_pricing_rates (
    rate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES pricing_versions(version_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    access_route TEXT NOT NULL DEFAULT 'default',
    input_rate_per_m REAL NOT NULL,
    output_rate_per_m REAL NOT NULL,
    cache_read_rate_per_m REAL,
    cache_write_5m_rate_per_m REAL,
    cache_write_1h_rate_per_m REAL,
    is_tiered INTEGER NOT NULL DEFAULT 0,
    prompt_threshold INTEGER,
    tiered_rates_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'verified',
    source_url TEXT,
    verified_on TEXT,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_rates_lookup
    ON model_pricing_rates (version_id, provider, model_identity, access_route);

CREATE TABLE IF NOT EXISTS historical_fx (
    fx_id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    target_currency TEXT NOT NULL DEFAULT 'INR',
    rate_per_usd REAL NOT NULL,
    effective_date TEXT NOT NULL,
    source_provenance TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    UNIQUE (base_currency, target_currency, effective_date)
);

CREATE INDEX IF NOT EXISTS idx_fx_date
    ON historical_fx (base_currency, target_currency, effective_date);

-- -----------------------------------------------------------------------------
-- 4. Canonical Requests, Observation Links, and Usage
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS canonical_requests (
    canonical_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    account_alias TEXT NOT NULL DEFAULT 'default',
    provider_request_id TEXT,
    fallback_identity TEXT,
    origin_harness TEXT NOT NULL,
    access_route TEXT NOT NULL DEFAULT 'unknown',
    raw_model TEXT NOT NULL,
    normalized_model TEXT NOT NULL,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    prompt_id TEXT REFERENCES prompts(prompt_id) ON DELETE SET NULL,
    parent_request_id TEXT REFERENCES canonical_requests(canonical_id) ON DELETE SET NULL,
    retry_attempt INTEGER NOT NULL DEFAULT 0,
    request_status TEXT NOT NULL DEFAULT 'ok',
    request_timestamp TEXT NOT NULL,
    timing_basis TEXT NOT NULL DEFAULT 'none',
    duration_ms INTEGER,
    ttft_ms INTEGER,
    generation_duration_ms INTEGER,
    generation_speed_tps REAL,
    is_unresolved_overlap INTEGER NOT NULL DEFAULT 0,
    pricing_version_id TEXT REFERENCES pricing_versions(version_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_req_provider_acct_id
    ON canonical_requests (provider, account_alias, provider_request_id)
    WHERE provider_request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_req_time
    ON canonical_requests (request_timestamp);
CREATE INDEX IF NOT EXISTS idx_req_session
    ON canonical_requests (session_id);
CREATE INDEX IF NOT EXISTS idx_req_model
    ON canonical_requests (normalized_model);
CREATE INDEX IF NOT EXISTS idx_req_provider
    ON canonical_requests (provider);
CREATE INDEX IF NOT EXISTS idx_req_route
    ON canonical_requests (access_route);
CREATE INDEX IF NOT EXISTS idx_req_harness
    ON canonical_requests (origin_harness);

CREATE TABLE IF NOT EXISTS request_observation_links (
    canonical_id TEXT NOT NULL REFERENCES canonical_requests(canonical_id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL REFERENCES source_observations(observation_id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (canonical_id, observation_id)
);

CREATE TABLE IF NOT EXISTS normalized_usage (
    canonical_id TEXT PRIMARY KEY REFERENCES canonical_requests(canonical_id) ON DELETE CASCADE,
    fresh_input_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_5m_tokens INTEGER,
    cache_write_1h_tokens INTEGER,
    cache_write_unknown_ttl_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    reported_total_tokens INTEGER,
    counter_inclusion_semantics TEXT NOT NULL DEFAULT 'disjoint',
    normalization_rule_version TEXT NOT NULL DEFAULT '1.0',
    is_partial INTEGER NOT NULL DEFAULT 0,
    missing_counters_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS financial_facts (
    canonical_id TEXT PRIMARY KEY REFERENCES canonical_requests(canonical_id) ON DELETE CASCADE,
    pricing_version_id TEXT REFERENCES pricing_versions(version_id) ON DELETE SET NULL,
    actual_billed_amount REAL,
    actual_billed_currency TEXT,
    api_equivalent_usd REAL,
    api_equivalent_inr REAL,
    subscription_fee_usd REAL,
    cache_savings_usd REAL,
    pricing_status TEXT NOT NULL DEFAULT 'unpriced',
    fx_rate_applied REAL,
    calculated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fin_status
    ON financial_facts (pricing_status);

-- -----------------------------------------------------------------------------
-- 5. Trace Events (Tool calls, Compaction, Switches)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    canonical_id TEXT REFERENCES canonical_requests(canonical_id) ON DELETE SET NULL,
    parent_event_id TEXT REFERENCES events(event_id) ON DELETE CASCADE,
    event_kind TEXT NOT NULL,
    event_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    output_size_bytes INTEGER,
    status TEXT NOT NULL DEFAULT 'ok',
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_session_time
    ON events (session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_events_request
    ON events (canonical_id);
CREATE INDEX IF NOT EXISTS idx_events_kind
    ON events (event_kind);

-- -----------------------------------------------------------------------------
-- 6. Subscription Quotas & Usage Limits
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscription_quotas (
    quota_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_alias TEXT NOT NULL DEFAULT 'default',
    access_route TEXT NOT NULL DEFAULT 'default',
    window_type TEXT NOT NULL,
    limit_value REAL,
    used_value REAL,
    remaining_value REAL,
    reset_at TEXT,
    measured_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unavailable',
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_quota_lookup
    ON subscription_quotas (provider, account_alias, measured_at);

-- -----------------------------------------------------------------------------
-- 7. Archive Partitions Distinct from Live Data
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS archive_partitions (
    partition_id TEXT PRIMARY KEY,
    partition_type TEXT NOT NULL,
    grain TEXT NOT NULL,
    source_name TEXT NOT NULL,
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0.0,
    records_count INTEGER NOT NULL DEFAULT 0,
    is_reconciled INTEGER NOT NULL DEFAULT 0,
    is_live_replaced INTEGER NOT NULL DEFAULT 0,
    reconciliation_notes TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_span
    ON archive_partitions (start_time, end_time);

-- -----------------------------------------------------------------------------
-- 8. Manual Outcomes, Ratings, Settings, and Privacy Opt-Ins
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_settings_and_outcomes (
    outcome_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    account_alias TEXT NOT NULL DEFAULT 'default',
    task_label TEXT,
    completion_rating TEXT,
    manual_rating REAL,
    is_inferred INTEGER NOT NULL DEFAULT 0,
    inference_method TEXT,
    inference_confidence REAL,
    git_commit_link TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcomes_entity
    ON manual_settings_and_outcomes (entity_type, entity_id);

CREATE TABLE IF NOT EXISTS privacy_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    opt_in_enabled INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_views_and_layouts (
    view_id TEXT PRIMARY KEY,
    view_type TEXT NOT NULL,
    view_name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    page_name TEXT,
    filter_state_json TEXT NOT NULL DEFAULT '{}',
    layout_state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_and_reports (
    item_id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    name TEXT NOT NULL,
    frequency TEXT,
    threshold_config_json TEXT NOT NULL DEFAULT '{}',
    filter_state_json TEXT NOT NULL DEFAULT '{}',
    pricing_assumptions_json TEXT NOT NULL DEFAULT '{}',
    last_generated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_inventory (
    export_id TEXT PRIMARY KEY,
    export_format TEXT NOT NULL,
    file_path TEXT NOT NULL,
    query_filter_json TEXT NOT NULL DEFAULT '{}',
    pricing_fx_assumptions_json TEXT NOT NULL DEFAULT '{}',
    redaction_applied INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
