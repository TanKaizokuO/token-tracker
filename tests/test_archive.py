"""Unit and integration tests for Token Ledger archive ingestion, reconciliation, and privacy.

Verifies WP05 requirements:
1. Versioned JSON aggregate partition ingestion into archive_partitions (schema002).
2. Strict cost distinction: NULL total_cost_usd (unknown/unpriced) vs 0.0 (measured zero).
3. Idempotent replay: Re-importing does not throw duplicate key errors and increments replayed_count.
4. Zero request manufacture: No rows inserted into canonical_requests or normalized_usage.
5. Privacy sanitization: Account aliases (emails/secrets) and project IDs (paths) are scrubbed.
6. Reconciliation compatibility: Refuses reconciliation on grain, timezone, or token semantics mismatch.
7. Complete replacement vs gap:
   - Live reconciliation succeeds and marks is_live_replaced = 1 only on verified complete coverage.
   - Non-contiguous intervals or checkpoint gaps leave partition active (is_live_replaced = 0).
   - Incompatible totals are never subtracted.
   - Archive partitions and private source logs are never deleted upon replacement.
8. Unsupported artifact rejection: Malformed or unversioned JSON structures are rejected cleanly.
9. Combined partition selection: Excludes superseded partitions and marks partial overlaps.
"""

import json
import os
import sqlite3
import unittest
from datetime import datetime, timezone

from token_ledger.ingest.archive import (
    ArchivePartitionRecord,
    CombinedPartitionSelection,
    ImportSummary,
    IncompatibleReconciliationError,
    ReconciliationProof,
    ReconciliationResult,
    UnsupportedArchiveArtifactError,
    import_archive_file,
    import_archive_partitions,
    reconcile_archive_partition,
    select_combined_partitions,
    validate_archive_artifact,
)
from token_ledger.store.database import connect, migrate, open_database


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
BASELINE_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "archive-baseline.json")


class TestArchiveIngestionAndReconciliation(unittest.TestCase):
    """Test suite for archive partition ingestion, reconciliation, and privacy."""

    def setUp(self):
        """Set up in-memory SQLite database with schema002 applied."""
        self.conn = open_database(":memory:", run_migrations=True)

    def tearDown(self):
        """Close connection."""
        self.conn.close()

    def test_import_archive_baseline_fixture(self):
        """Verify baseline fixture imports all partitions with full metadata."""
        summary = import_archive_file(self.conn, BASELINE_FIXTURE_PATH)
        self.assertEqual(summary.imported_count, 4)
        self.assertEqual(summary.replayed_count, 0)
        self.assertEqual(len(summary.partitions), 4)

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM archive_partitions;")
        self.assertEqual(cur.fetchone()[0], 4)

    def test_cost_unknown_null_vs_measured_zero(self):
        """Verify unknown cost (NULL) vs measured zero (0.0) are preserved distinctly."""
        import_archive_file(self.conn, BASELINE_FIXTURE_PATH)

        cur = self.conn.cursor()

        # Query unpriced partition (total_cost_usd IS NULL)
        cur.execute(
            """
            SELECT partition_id, total_cost_usd, pricing_status
            FROM archive_partitions
            WHERE partition_id = 'arch_omp_daily_20260101_ny';
            """
        )
        row_null = cur.fetchone()
        self.assertIsNotNone(row_null)
        self.assertIsNone(row_null[1])
        self.assertEqual(row_null[2], "unpriced")

        # Query measured zero partition (total_cost_usd = 0.0)
        cur.execute(
            """
            SELECT partition_id, total_cost_usd, pricing_status
            FROM archive_partitions
            WHERE partition_id = 'arch_claude_daily_20260101_utc';
            """
        )
        row_zero = cur.fetchone()
        self.assertIsNotNone(row_zero)
        self.assertEqual(row_zero[1], 0.0)
        self.assertEqual(row_zero[2], "priced")

        # Direct SQL assertions for NULL and 0.0 rows
        cur.execute("SELECT partition_id FROM archive_partitions WHERE total_cost_usd IS NULL;")
        null_ids = [r[0] for r in cur.fetchall()]
        self.assertIn("arch_omp_daily_20260101_ny", null_ids)

        cur.execute("SELECT partition_id FROM archive_partitions WHERE total_cost_usd = 0.0;")
        zero_ids = [r[0] for r in cur.fetchall()]
        self.assertIn("arch_claude_daily_20260101_utc", zero_ids)

    def test_idempotent_replay(self):
        """Re-importing the same archive bundle must not fail or create duplicate rows."""
        summary1 = import_archive_file(self.conn, BASELINE_FIXTURE_PATH)
        self.assertEqual(summary1.imported_count, 4)
        self.assertEqual(summary1.replayed_count, 0)

        # Re-import identical artifact
        summary2 = import_archive_file(self.conn, BASELINE_FIXTURE_PATH)
        self.assertEqual(summary2.imported_count, 0)
        self.assertEqual(summary2.replayed_count, 4)

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM archive_partitions;")
        self.assertEqual(cur.fetchone()[0], 4)

    def test_no_request_manufacture(self):
        """Archive import must never manufacture fake canonical requests or normalized usage."""
        import_archive_file(self.conn, BASELINE_FIXTURE_PATH)

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests;")
        self.assertEqual(cur.fetchone()[0], 0)

        cur.execute("SELECT COUNT(*) FROM normalized_usage;")
        self.assertEqual(cur.fetchone()[0], 0)

        cur.execute("SELECT COUNT(*) FROM source_observations;")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_privacy_sanitization_before_storage(self):
        """Account emails/secrets and project filesystem paths must be scrubbed before DB insert."""
        import_archive_file(self.conn, BASELINE_FIXTURE_PATH)

        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT account_alias, project_id
            FROM archive_partitions
            WHERE partition_id = 'arch_omp_daily_20260101_ny';
            """
        )
        row = cur.fetchone()
        account_alias, project_id = row[0], row[1]

        # Raw account was 'user@example.com' -> should be sanitized to safe alias
        self.assertNotIn("@", account_alias)
        self.assertTrue(account_alias.startswith("acct_"))

        # Raw project was '/home/developer/workspace/project-alpha' -> should not contain slashes
        self.assertNotIn("/", project_id)
        self.assertNotIn("\\", project_id)
        self.assertTrue(project_id.startswith("acct_") or project_id == "project-alpha")
        # Foreign key parent in projects table must exist
        cur.execute("SELECT COUNT(*) FROM projects WHERE project_id = ?;", (project_id,))
        self.assertEqual(cur.fetchone()[0], 1)

    def test_date_tz_grain_incompatibility(self):
        """Reconciliation must reject mismatches in grain, timezone, or token semantics."""
        import_archive_file(self.conn, BASELINE_FIXTURE_PATH)

        # 1. Grain mismatch: Partition has grain 'day', proof asserts 'hour'
        proof_bad_grain = ReconciliationProof(
            grain="hour",
            timezone_name="America/New_York",
            source_adapter="omp",
            start_time="2026-01-01T00:00:00-05:00",
            end_time="2026-01-02T00:00:00-05:00",
            checkpoint_verified=True,
            contiguous_coverage=True,
        )
        res_grain = reconcile_archive_partition(self.conn, "arch_omp_daily_20260101_ny", proof=proof_bad_grain)
        self.assertFalse(res_grain.is_reconciled)
        self.assertFalse(res_grain.is_live_replaced)
        self.assertEqual(res_grain.status, "incompatible_grain")

        # 2. Timezone mismatch: Partition has timezone 'America/New_York', proof asserts 'UTC'
        proof_bad_tz = ReconciliationProof(
            grain="day",
            timezone_name="UTC",
            source_adapter="omp",
            start_time="2026-01-01T00:00:00-05:00",
            end_time="2026-01-02T00:00:00-05:00",
            checkpoint_verified=True,
            contiguous_coverage=True,
        )
        res_tz = reconcile_archive_partition(self.conn, "arch_omp_daily_20260101_ny", proof=proof_bad_tz)
        self.assertFalse(res_tz.is_reconciled)
        self.assertFalse(res_tz.is_live_replaced)
        self.assertEqual(res_tz.status, "incompatible_timezone")

        # 3. Token semantics mismatch: Non-disjoint billable semantics
        proof_bad_tokens = ReconciliationProof(
            grain="day",
            timezone_name="America/New_York",
            source_adapter="omp",
            start_time="2026-01-01T00:00:00-05:00",
            end_time="2026-01-02T00:00:00-05:00",
            token_semantics="lossy_flat_sum",
            checkpoint_verified=True,
            contiguous_coverage=True,
        )
        res_tokens = reconcile_archive_partition(self.conn, "arch_omp_daily_20260101_ny", proof=proof_bad_tokens)
        self.assertFalse(res_tokens.is_reconciled)
        self.assertFalse(res_tokens.is_live_replaced)
        self.assertEqual(res_tokens.status, "incompatible_token_semantics")

        # Verify partition remains active in DB
        cur = self.conn.cursor()
        cur.execute("SELECT is_live_replaced FROM archive_partitions WHERE partition_id = 'arch_omp_daily_20260101_ny';")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_complete_replacement_vs_gap(self):
        """Verifies complete replacement on proof of contiguous coverage vs gap rejection."""
        import_archive_file(self.conn, BASELINE_FIXTURE_PATH)
        partition_id = "arch_claude_hourly_20260103_00_utc"

        # Populate canonical records and checkpoint for this hourly partition
        start_ts = "2026-01-03T00:10:00Z"
        mid_ts = "2026-01-03T00:30:00Z"

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id,
                    origin_harness, access_route, raw_model, normalized_model,
                    request_status, request_timestamp, timing_basis, duration_ms, created_at
                ) VALUES
                    ('can_req_1', 'anthropic', 'default', 'req_001', 'claude_code', 'direct_api', 'claude-3-5', 'claude-3-5-sonnet', 'success', ?, 'measured', 500, ?),
                    ('can_req_2', 'anthropic', 'default', 'req_002', 'claude_code', 'direct_api', 'claude-3-5', 'claude-3-5-sonnet', 'success', ?, 'measured', 600, ?);
                """,
                (start_ts, start_ts, mid_ts, mid_ts),
            )
            self.conn.execute(
                """
                INSERT INTO normalized_usage (
                    canonical_id, fresh_input_tokens, output_tokens, reported_total_tokens
                ) VALUES
                    ('can_req_1', 4000, 2000, 6000),
                    ('can_req_2', 4000, 2000, 6000);
                """
            )
            self.conn.execute(
                """
                INSERT INTO source_checkpoints (
                    source_adapter, origin_harness, account_alias, file_locator,
                    last_byte_offset, last_line_index, file_generation, status,
                    last_scan_at, last_success_at
                ) VALUES ('claude_code', 'claude_code', 'default', 'loc_01', 50000, 100, 'gen_1', 'completed', '2026-01-03T01:30:00Z', '2026-01-03T01:30:00Z');
                """
            )

        # Case 1: Proof has gap intervals -> Must reject replacement
        gap_proof = ReconciliationProof(
            grain="hour",
            timezone_name="UTC",
            source_adapter="claude_code",
            start_time="2026-01-03T00:00:00Z",
            end_time="2026-01-03T01:00:00Z",
            account_alias="default",
            checkpoint_verified=True,
            contiguous_coverage=False,
            gap_intervals=[("2026-01-03T00:15:00Z", "2026-01-03T00:25:00Z")],
        )
        res_gap = reconcile_archive_partition(self.conn, partition_id, proof=gap_proof)
        self.assertFalse(res_gap.is_live_replaced)
        self.assertEqual(res_gap.status, "coverage_gap_detected")

        cur = self.conn.cursor()
        cur.execute("SELECT is_live_replaced FROM archive_partitions WHERE partition_id = ?;", (partition_id,))
        self.assertEqual(cur.fetchone()[0], 0)

        # Case 2: Complete coverage proof verified -> Grant replacement state
        complete_proof = ReconciliationProof(
            grain="hour",
            timezone_name="UTC",
            source_adapter="claude_code",
            start_time="2026-01-03T00:00:00Z",
            end_time="2026-01-03T01:00:00Z",
            account_alias="default",
            expected_records_count=2,
            checkpoint_verified=True,
            contiguous_coverage=True,
        )
        res_complete = reconcile_archive_partition(self.conn, partition_id, proof=complete_proof)
        self.assertTrue(res_complete.is_live_replaced)
        self.assertTrue(res_complete.is_reconciled)
        self.assertEqual(res_complete.status, "reconciled_replaced")

        # Crucial Rule: The row in archive_partitions is NOT deleted!
        cur.execute(
            """
            SELECT is_live_replaced, is_reconciled, reconciliation_notes
            FROM archive_partitions
            WHERE partition_id = ?;
            """,
            (partition_id,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 1)
        self.assertIn("Verified complete canonical coverage", row[2])

    def test_unsupported_artifact_rejection(self):
        """Unsupported schemas, missing required attributes, or bad JSON must raise UnsupportedArchiveArtifactError."""
        # 1. Missing version
        bad_artifact_1 = {
            "artifact_type": "archive_partition_bundle",
            "partitions": [],
        }
        with self.assertRaises(UnsupportedArchiveArtifactError):
            import_archive_partitions(self.conn, bad_artifact_1)

        # 2. Unsupported version
        bad_artifact_2 = {
            "version": "99.0",
            "artifact_type": "archive_partition_bundle",
            "partitions": [],
        }
        with self.assertRaises(UnsupportedArchiveArtifactError):
            import_archive_partitions(self.conn, bad_artifact_2)

        # 3. Invalid artifact_type (e.g. passing raw live logs or unversioned payload)
        bad_artifact_3 = {
            "version": "1.0",
            "artifact_type": "raw_observation_log",
            "partitions": [],
        }
        with self.assertRaises(UnsupportedArchiveArtifactError):
            import_archive_partitions(self.conn, bad_artifact_3)

        # 4. Partition missing required fields
        bad_artifact_4 = {
            "version": "1.0",
            "artifact_type": "archive_partition_bundle",
            "partitions": [
                {"partition_id": "missing_fields"}
            ],
        }
        with self.assertRaises(UnsupportedArchiveArtifactError):
            import_archive_partitions(self.conn, bad_artifact_4)

        # 5. Unsupported grain
        bad_artifact_5 = {
            "version": "1.0",
            "artifact_type": "archive_partition_bundle",
            "partitions": [
                {
                    "partition_id": "bad_grain",
                    "partition_type": "summary",
                    "grain": "century",
                    "source_name": "omp",
                    "start_time": "2026-01-01T00:00:00Z",
                    "end_time": "2026-01-02T00:00:00Z",
                    "total_tokens": 100,
                }
            ],
        }
        with self.assertRaises(UnsupportedArchiveArtifactError):
            import_archive_partitions(self.conn, bad_artifact_5)

        # 6. Malformed JSON string
        with self.assertRaises(UnsupportedArchiveArtifactError):
            import_archive_partitions(self.conn, "not valid json {")

    def test_combined_partition_selection_and_overlap(self):
        """select_combined_partitions must exclude superseded partitions and identify partial overlaps."""
        import_archive_file(self.conn, BASELINE_FIXTURE_PATH)

        # Initially, all 4 partitions are active (none superseded)
        sel_init = select_combined_partitions(self.conn)
        self.assertEqual(len(sel_init.active_partitions), 4)
        self.assertEqual(len(sel_init.superseded_partitions), 0)

        # Mark one partition as live replaced
        with self.conn:
            self.conn.execute(
                "UPDATE archive_partitions SET is_live_replaced = 1 WHERE partition_id = 'arch_claude_hourly_20260103_00_utc';"
            )

        # Query active partitions: must exclude the replaced one
        sel_after = select_combined_partitions(self.conn)
        self.assertEqual(len(sel_after.active_partitions), 3)
        self.assertEqual(len(sel_after.superseded_partitions), 1)
        active_ids = [p["partition_id"] for p in sel_after.active_partitions]
        self.assertNotIn("arch_claude_hourly_20260103_00_utc", active_ids)
        self.assertEqual(sel_after.superseded_partitions[0]["partition_id"], "arch_claude_hourly_20260103_00_utc")

        # Now insert a partially overlapping partition for omp daily 2026-01-02
        overlapping_artifact = {
            "version": "1.0",
            "artifact_type": "archive_partition_bundle",
            "partitions": [
                {
                    "partition_id": "arch_omp_daily_20260102_partial_conflict",
                    "partition_type": "daily_summary",
                    "grain": "day",
                    "source_name": "omp",
                    "timezone_name": "UTC",
                    "start_time": "2026-01-02T12:00:00Z",  # Overlaps with arch_omp_daily_20260102_utc
                    "end_time": "2026-01-03T12:00:00Z",
                    "total_tokens": 105000,
                    "total_cost_usd": 2.10,
                    "records_count": 40,
                    "account_alias": "developer_work",
                    "project_id": "proj_beta",
                }
            ],
        }
        summary_overlap = import_archive_partitions(self.conn, overlapping_artifact)
        self.assertEqual(summary_overlap.overlap_count, 1)
        self.assertEqual(len(summary_overlap.unresolved_overlaps), 1)

        # Verify combined selection flags partial overlap
        sel_overlap = select_combined_partitions(self.conn)
        self.assertTrue(sel_overlap.has_unresolved_overlap)
        self.assertGreaterEqual(len(sel_overlap.partial_overlaps), 1)
        overlap = sel_overlap.partial_overlaps[0]
        self.assertIn(overlap["partition_a"], ("arch_omp_daily_20260102_utc", "arch_omp_daily_20260102_partial_conflict"))
        self.assertIn(overlap["partition_b"], ("arch_omp_daily_20260102_utc", "arch_omp_daily_20260102_partial_conflict"))


if __name__ == "__main__":
    unittest.main()
