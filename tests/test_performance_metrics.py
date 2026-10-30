"""Unit and integration tests for Performance & Speed Metrics (WP14a/b).

Covers:
- Boundary conditions (empty sets, single samples, missing fields).
- Deterministic percentile computation (nearest-rank).
- Weighted TPS vs per-request mean/median differences.
- Eligible 0-output tokens inclusion.
- Untimed / missing generation and TTFT exclusion (never synthetic zero).
- Long requests retention.
- Separation of timing bases (measured vs log_span_proxy never pooled).
- Multi-dimensional slicing (model, provider, route, task, context, output size, time).
- Request-linked anomaly detection.
- Explicit task labels in shared rows.
- Actual SQLite temporary DB smoke test using canonical schema.
"""

from datetime import datetime, timezone
import sqlite3
import unittest

from token_ledger.metrics.performance import (
    analyze,
    compute_percentiles,
)
from token_ledger.store import open_database


class TestPerformanceMetricsUnit(unittest.TestCase):
    def test_compute_percentiles_empty(self):
        res = compute_percentiles([])
        self.assertEqual(res["count"], 0)
        self.assertIsNone(res["mean"])
        self.assertIsNone(res["p50"])
        self.assertIsNone(res["p90"])
        self.assertIsNone(res["p95"])
        self.assertEqual(res["method"], "nearest_rank")

    def test_compute_percentiles_deterministic(self):
        # 10 values: 10, 20, 30, ..., 100
        vals = [float(x * 10) for x in range(1, 11)]
        res = compute_percentiles(vals)
        self.assertEqual(res["count"], 10)
        self.assertEqual(res["mean"], 55.0)
        self.assertEqual(res["min"], 10.0)
        self.assertEqual(res["max"], 100.0)
        # ceil(0.50 * 10) - 1 = index 4 -> 50.0
        self.assertEqual(res["p50"], 50.0)
        # ceil(0.90 * 10) - 1 = index 8 -> 90.0
        self.assertEqual(res["p90"], 90.0)
        # ceil(0.95 * 10) - 1 = index 9 -> 100.0
        self.assertEqual(res["p95"], 100.0)

    def test_weighted_vs_mean_tps_and_zero_output(self):
        """Tests that weighted TPS differs from per-request arithmetic mean,
        and eligible 0-output tokens are accepted.
        """
        rows = [
            # Large request: 1000 tokens in 10s = 100 TPS
            {
                "canonical_id": "req-1",
                "timing_basis": "measured",
                "generation_duration_ms": 10000,
                "output_tokens": 1000,
                "duration_ms": 11000,
                "ttft_ms": 1000,
                "normalized_model": "claude-3-5-sonnet",
            },
            # Small request: 10 tokens in 1s = 10 TPS
            {
                "canonical_id": "req-2",
                "timing_basis": "measured",
                "generation_duration_ms": 1000,
                "output_tokens": 10,
                "duration_ms": 1200,
                "ttft_ms": 200,
                "normalized_model": "claude-3-5-sonnet",
            },
            # Zero output request with valid generation duration: 0 tokens in 2s = 0.0 TPS
            {
                "canonical_id": "req-3",
                "timing_basis": "measured",
                "generation_duration_ms": 2000,
                "output_tokens": 0,
                "duration_ms": 2500,
                "ttft_ms": 500,
                "normalized_model": "claude-3-5-sonnet",
            },
        ]
        envelope = {"contract_version": "1", "harness_scope": "combined"}
        res = analyze(conn=None, envelope=envelope, rows=rows)

        # Per request TPS values: [100.0, 10.0, 0.0]
        # Mean per request = (100 + 10 + 0) / 3 = 36.6667
        # Median per request = 10.0
        # Weighted TPS = sum(output) / sum(gen_sec) = (1000 + 10 + 0) / (10 + 1 + 2) = 1010 / 13 = 77.6923
        self.assertAlmostEqual(res["values"]["generation_speed_tps_mean"], 36.6667, places=3)
        self.assertEqual(res["values"]["generation_speed_tps_median"], 10.0)
        self.assertAlmostEqual(res["values"]["generation_speed_tps_weighted"], 77.6923, places=3)
        self.assertEqual(res["counts"]["eligible_zero_output_requests"], 1)
        self.assertEqual(res["counts"]["eligible_generation_requests"], 3)

    def test_missing_timing_never_zero_and_long_requests_retained(self):
        """Tests that missing timing fields are never treated as 0 ms or 0 TPS,
        and long requests are retained in distributions.
        """
        rows = [
            # Long request (5000 output tokens, 50s duration)
            {
                "canonical_id": "req-long",
                "timing_basis": "measured",
                "generation_duration_ms": 50000,
                "output_tokens": 5000,
                "duration_ms": 52000,
                "ttft_ms": 2000,
                "normalized_model": "claude-3-5-sonnet",
            },
            # Untimed request (timing_basis = 'none', missing gen/ttft)
            {
                "canonical_id": "req-untimed",
                "timing_basis": "none",
                "generation_duration_ms": None,
                "output_tokens": 500,
                "duration_ms": None,
                "ttft_ms": None,
                "normalized_model": "claude-3-5-sonnet",
            },
            # Request with total duration but missing TTFT and generation duration
            {
                "canonical_id": "req-part-timed",
                "timing_basis": "measured",
                "generation_duration_ms": None,
                "output_tokens": 100,
                "duration_ms": 1500,
                "ttft_ms": None,
                "normalized_model": "claude-3-5-sonnet",
            },
        ]
        envelope = {"contract_version": "1"}
        res = analyze(conn=None, envelope=envelope, rows=rows)

        # Only 1 request is generation-timed
        self.assertEqual(res["counts"]["eligible_generation_requests"], 1)
        self.assertEqual(res["values"]["generation_speed_tps_weighted"], 100.0)
        self.assertEqual(res["values"]["generation_speed_tps_mean"], 100.0)
        # Only 1 request has TTFT
        self.assertEqual(res["counts"]["eligible_ttft_requests"], 1)
        self.assertEqual(res["values"]["ttft_ms_mean"], 2000.0)
        # 2 requests have total duration
        self.assertEqual(res["counts"]["eligible_total_duration_requests"], 2)
        # Missing timing is not zero
        self.assertEqual(res["missingness"]["unobserved_timing_count"], 1)
        self.assertEqual(res["missingness"]["missing_ttft_count"], 2)

    def test_separation_of_timing_bases(self):
        """Tests that measured vs log_span_proxy are never pooled together."""
        rows = [
            {
                "canonical_id": "req-m1",
                "timing_basis": "measured",
                "generation_duration_ms": 1000,
                "output_tokens": 100,
                "ttft_ms": 300,
                "normalized_model": "gpt-4o",
            },
            {
                "canonical_id": "req-p1",
                "timing_basis": "log_span_proxy",
                "generation_duration_ms": 2000,
                "output_tokens": 40,
                "ttft_ms": 1500,
                "normalized_model": "gpt-4o",
            },
        ]
        envelope = {"contract_version": "1"}
        res = analyze(conn=None, envelope=envelope, rows=rows)

        # Warning emitted for mixed timing basis
        self.assertTrue(any("never pooled" in w for w in res["warnings"]))
        # Primary basis is measured
        self.assertEqual(res["primary_timing_basis"], "measured")
        self.assertEqual(res["values"]["generation_speed_tps_weighted"], 100.0)

        # Separate slices for each basis
        basis_slices = res["slices"]["timing_basis"]
        self.assertEqual(basis_slices["measured"]["tps"]["weighted"], 100.0)
        self.assertEqual(basis_slices["log_span_proxy"]["tps"]["weighted"], 20.0)

    def test_task_labels_and_absent_independent_baseline(self):
        """Selected-period requests never become their own anomaly baseline."""
        rows = [
            # Normal baseline requests
            {
                "canonical_id": f"req-norm-{i}",
                "timing_basis": "measured",
                "generation_duration_ms": 1000,
                "output_tokens": 100,  # 100 TPS
                "ttft_ms": 300,
                "normalized_model": "claude-3-5-sonnet",
                "task_label": "refactoring",
            }
            for i in range(5)
        ] + [
            # Severe TPS drop anomaly (20 tokens in 2s = 10 TPS, baseline 100 TPS)
            {
                "canonical_id": "req-slow",
                "request_timestamp": "2026-10-15T12:00:00Z",
                "timing_basis": "measured",
                "generation_duration_ms": 2000,
                "output_tokens": 20,
                "ttft_ms": 350,
                "normalized_model": "claude-3-5-sonnet",
                "provider": "anthropic",
                "access_route": "direct",
                "task_label": "debugging",
            },
            # TTFT spike anomaly (3500ms vs baseline 300ms)
            {
                "canonical_id": "req-lag",
                "request_timestamp": "2026-10-15T12:05:00Z",
                "timing_basis": "measured",
                "generation_duration_ms": 1000,
                "output_tokens": 100,
                "ttft_ms": 3500,
                "normalized_model": "claude-3-5-sonnet",
                "provider": "anthropic",
                "access_route": "direct",
                "task_label": "debugging",
            },
        ]

        envelope = {"contract_version": "1"}
        res = analyze(conn=None, envelope=envelope, rows=rows)

        # Slices by task exist and contain explicit labels
        task_slices = res["slices"]["task"]
        self.assertIn("refactoring", task_slices)
        self.assertIn("debugging", task_slices)


        self.assertEqual(res["anomalies"], [])
        self.assertTrue(all(group["tps_samples"] == 0 for group in res["anomaly_baseline"]["groups"]))


class TestPerformanceMetricsDatabaseIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = open_database(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_smoke_database_performance_analytics(self):
        """End-to-end database smoke test using canonical schema."""
        now_iso = "2026-10-20T10:00:00Z"
        cur = self.conn.cursor()

        # Insert session
        cur.execute(
            "INSERT INTO sessions (session_id, origin_harness, account_alias, started_at, created_at) "
            "VALUES ('sess-100', 'claude-code', 'work', ?, ?)",
            (now_iso, now_iso),
        )

        # Insert canonical request 1 (measured, 50 TPS, TTFT 400ms)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, session_id, request_status, request_timestamp,
                timing_basis, duration_ms, ttft_ms, generation_duration_ms, generation_speed_tps, created_at
            ) VALUES (
                'req-smoke-1', 'anthropic', 'work', 'claude-code', 'direct',
                'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-100', 'ok', ?,
                'measured', 2500, 400, 2000, 50.0, ?
            )
            """,
            (now_iso, now_iso),
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens
            ) VALUES ('req-smoke-1', 1200, 500, 100)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, calculated_at
            ) VALUES ('req-smoke-1', 'priced', 0.05, ?)
            """,
            (now_iso,),
        )

        # Insert canonical request 2 (untimed request)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, session_id, request_status, request_timestamp,
                timing_basis, duration_ms, ttft_ms, generation_duration_ms, generation_speed_tps, created_at
            ) VALUES (
                'req-smoke-2', 'anthropic', 'work', 'claude-code', 'direct',
                'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-100', 'ok', ?,
                'none', NULL, NULL, NULL, NULL, ?
            )
            """,
            (now_iso, now_iso),
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens
            ) VALUES ('req-smoke-2', 800, 0, 50)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, calculated_at
            ) VALUES ('req-smoke-2', 'priced', 0.02, ?)
            """,
            (now_iso,),
        )

        # Insert explicit manual outcome / task label
        cur.execute(
            """
            INSERT INTO manual_settings_and_outcomes (
                outcome_id, entity_type, entity_id, task_label, created_at, updated_at
            ) VALUES ('out-1', 'session', 'sess-100', 'architecture-review', ?, ?)
            """,
            (now_iso, now_iso),
        )
        self.conn.commit()

        # Execute analyze with query envelope
        envelope = {
            "contract_version": "1",
            "harness_scope": "claude-code",
            "metric_id": "generation_speed_tps",
            "filters": {"model": ["claude-3-5-sonnet"]},
        }
        res = analyze(self.conn, envelope)

        self.assertEqual(res["contract_version"], "1")
        self.assertEqual(res["family"], "speed")
        self.assertEqual(res["primary_timing_basis"], "measured")
        self.assertEqual(res["values"]["generation_speed_tps_weighted"], 50.0)
        self.assertEqual(res["values"]["ttft_ms_mean"], 400.0)
        self.assertEqual(res["values"]["percentile_method"], "nearest_rank")

        # Denominators and counts
        self.assertEqual(res["counts"]["total_requests"], 1)
        self.assertEqual(res["eligible"]["total_requests"], 2)
        self.assertEqual(res["counts"]["eligible_generation_requests"], 1)
        self.assertEqual(res["counts"]["unobserved_timing_requests"], 0)
        self.assertEqual(res["observed"]["unobserved_timing"], 1)

        # Slices
        self.assertIn("claude-3-5-sonnet", res["slices"]["model"])
        self.assertIn("architecture-review", res["slices"]["task"])
        self.assertEqual(
            res["slices"]["task"]["architecture-review"]["counts"]["eligible_generation_requests"], 1
        )
        self.assertIn("1k-8k", res["slices"]["context_size"])


if __name__ == "__main__":
    unittest.main()
