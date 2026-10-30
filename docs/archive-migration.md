# Archive Migration, Reconciliation, and Replacement Architecture (WP05)

## 1. Overview and Architecture

The Token Ledger archive subsystem (`token_ledger.ingest.archive`) provides a strictly governed, idempotent ingestion path for versioned JSON aggregate historical partitions into SQLite `archive_partitions` (schema002).

Historical archives represent immutable, pre-aggregated summaries of token usage across legacy sessions, days, hours, or billing intervals. The architecture guarantees:
1. **Zero Request Manufacture:** Ingesting historical aggregate partitions never manufactures artificial entries in `canonical_requests`, `normalized_usage`, or `source_observations`.
2. **Nullable Cost Semantics:** Differentiates `NULL` (unpriced or unknown cost) from `0.0` (explicitly measured zero-cost usage, such as free tiers or sponsored requests).
3. **Legacy Metadata Preservation:** Preserves legacy timezones (e.g. `America/New_York`), historical pricing status, grain, source adapter, and detected interval gaps.
4. **Idempotent Replay:** Re-importing identical partition bundles does not throw duplicate key errors; replayed partitions are counted safely.
5. **Mutual Exclusion and Overlap Detection:** Partitions sharing the same source adapter, account alias, and grain are checked for temporal overlap. Any non-identical boundary overlap is flagged as unresolved overlap.
6. **Privacy Pre-Persistence Sanitization:** Raw account strings (e.g. emails) and file paths in project references are sanitized prior to database persistence, and parent foreign-key records are created automatically.
7. **Verification-Driven Live Reconciliation:** Live canonical records replace archive partitions only when grain, timezone, and token semantics match and complete coverage is verified via an explicit proof structure and checkpoints. Partitions and private source files are never deleted upon replacement; rather, they transition to `is_live_replaced = 1`.
8. **Superseded Partition Exclusion:** Combined partition selection APIs exclude superseded (`is_live_replaced = 1`) partitions by default and expose partial overlap indicators.

---

## 2. Archive Partition Schema (schema002)

The `archive_partitions` table (upgraded in `002_archive_reconciliation_and_scoping.sql`) stores aggregate partitions:

```sql
CREATE TABLE archive_partitions (
    partition_id TEXT PRIMARY KEY,
    partition_type TEXT NOT NULL,
    grain TEXT NOT NULL,               -- 'day', 'hour', 'session', 'week', 'month'
    source_name TEXT NOT NULL,         -- 'omp', 'claude_code', 'combined', etc.
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    start_time TEXT NOT NULL,          -- ISO-8601 start bound
    end_time TEXT NOT NULL,            -- ISO-8601 end bound
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL,               -- NULL = unknown/unpriced; 0.0 = measured zero
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
```

---

## 3. Versioned JSON Artifact Specification

Archive bundles must follow schema version `1.0`:

```json
{
  "version": "1.0",
  "artifact_type": "archive_partition_bundle",
  "exported_at": "2026-01-15T12:00:00Z",
  "source_system": "token_ledger_archive_exporter",
  "partitions": [
    {
      "partition_id": "arch_omp_daily_20260101_ny",
      "partition_type": "daily_summary",
      "grain": "day",
      "source_name": "omp",
      "timezone_name": "America/New_York",
      "start_time": "2026-01-01T00:00:00-05:00",
      "end_time": "2026-01-02T00:00:00-05:00",
      "total_tokens": 154200,
      "total_cost_usd": null,
      "records_count": 55,
      "pricing_status": "unpriced",
      "account_alias": "user@example.com",
      "project_id": "/home/developer/workspace/project-alpha",
      "missingness": {
        "unpriced_models": ["legacy-custom-v1"],
        "gaps": []
      },
      "coverage": {
        "status": "complete",
        "checkpoint_offset": 54200,
        "span_hours": 24
      },
      "payload": {
        "model_breakdown": {
          "legacy-custom-v1": {
            "tokens": 154200,
            "requests": 55
          }
        }
      }
    }
  ]
}
```

---

## 4. Reconciliation and Replacement Mechanics

### 4.1 Prerequisites for Reconciliation
Reconciliation attempts between an archive partition and live canonical records must satisfy:
1. **Grain Equality:** The grain of the partition and the live aggregation window must match (e.g. daily partition reconciled against a full 24-hour canonical aggregation).
2. **Timezone Alignment:** Timezone boundaries must match legacy partitions (`timezone_name`).
3. **Disjoint Token Semantics:** Live canonical records measure disjoint billable tokens (`fresh_input_tokens`, `cache_read_tokens`, `cache_write_*`, `output_tokens`). Incompatible total formats cannot be subtracted.
4. **Explicit Proof of Complete Coverage:**
   - Callers cannot simply assert boolean `complete = True`.
   - The reconciliation engine validates `ReconciliationProof` against `source_checkpoints` and `canonical_requests`.
   - If any gap intervals exist in the live logs or if checkpoints indicate incomplete scanning, reconciliation is refused (`coverage_gap_detected` or `checkpoint_incomplete`).

### 4.2 Replacement State (`is_live_replaced = 1`)
When complete coverage is verified:
- The partition is marked `is_reconciled = 1` and `is_live_replaced = 1`.
- Reconciliation notes describe the proof verification.
- **Critical Policy:** Neither the archive partition record nor the underlying source logs are deleted. The replacement state allows downstream reporting to substitute live records while retaining full auditability of the archive.

---

## 5. Combined Partition Selection

The `select_combined_partitions` API provides clean querying for analytical consumers:
- Filters out superseded partitions (`is_live_replaced = 1`) by default.
- Detects and flags any partial temporal overlaps between active partitions.
- Returns structured results with clear provenance and overlap disclosures.
