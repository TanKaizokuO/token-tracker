"""Substantive unit and integration tests for WP12 Token Metrics & Cache Efficiency.

Tests cover:
1. Disjoint billable token counters (fresh_input, cache_read, 5m, 1h, unknown TTL, output).
2. Non-additive reasoning subset semantics (never double-counted into total or output).
3. Reported source total preservation.
4. Cache hit rate eligible denominator (strictly fresh_input + cache_read).
5. Null vs measured zero counter preservation.
6. Unresolved overlap detection and partial status.
7. Priced vs unpriced no-cache estimates.
8. Per-TTL break-even simulator and explicit parameter validation.
9. Groupings by model, project, session, and day.
"""

from __future__ import annotations

import sqlite3
import unittest

from token_ledger.metrics.tokens import analyze, cache_scenario
from token_ledger.store.database import open_database


class TestTokenMetrics(unittest.TestCase):
    def setUp(self) -> None:
        # In-memory database with standard migrations applied
        self.conn = open_database(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_fixture_data(self) -> None:
        """Populates comprehensive fixture rows for testing."""
        cur = self.conn.cursor()

        # Insert project and session
        cur.execute(
            """
            INSERT INTO projects (project_id, project_name, account_alias, created_at)
            VALUES ('proj-1', 'Alpha', 'default', '2026-10-15T00:00:00Z')
            """
        )
        cur.execute(
            """
            INSERT INTO sessions (session_id, origin_harness, project_id, started_at, created_at)
            VALUES ('sess-1', 'claude-code', 'proj-1', '2026-10-15T10:00:00Z', '2026-10-15T10:00:00Z'),
                   ('sess-2', 'oh-my-pi', 'proj-1', '2026-10-16T10:00:00Z', '2026-10-16T10:00:00Z')
            """
        )

        # 1. Canonical request 1: Priced Claude request with 5m write & reasoning subset
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, is_unresolved_overlap, created_at
            ) VALUES (
                'req-1', 'anthropic', 'default', 'req_id_1', 'claude-code',
                'default', 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-1', 'proj-1',
                'ok', '2026-10-15T10:05:00Z', 'none', 0, '2026-10-15T10:05:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (
                'req-1', 4000, 6000, 2000, 0, 0, 1000, 300, 13000
            )
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, api_equivalent_usd, cache_savings_usd, pricing_status, calculated_at
            ) VALUES (
                'req-1', 0.0405, 0.0162, 'priced', '2026-10-15T10:05:00Z'
            )
            """
        )

        # 2. Canonical request 2: Priced request with 1h write, no reasoning
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, is_unresolved_overlap, created_at
            ) VALUES (
                'req-2', 'anthropic', 'default', 'req_id_2', 'claude-code',
                'default', 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-1', 'proj-1',
                'ok', '2026-10-15T10:10:00Z', 'none', 0, '2026-10-15T10:10:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (
                'req-2', 1000, 9000, 0, 3000, 0, 2000, 0, 15000
            )
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, api_equivalent_usd, cache_savings_usd, pricing_status, calculated_at
            ) VALUES (
                'req-2', 0.0540, 0.0243, 'priced', '2026-10-15T10:10:00Z'
            )
            """
        )

        # 3. Canonical request 3: Unpriced request with unknown TTL cache write on 2026-10-16
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, is_unresolved_overlap, created_at
            ) VALUES (
                'req-3', 'google', 'default', 'req_id_3', 'oh-my-pi',
                'default', 'gemini-3-8-flash', 'gemini-3-8-flash', 'sess-2', 'proj-1',
                'ok', '2026-10-16T12:00:00Z', 'measured', 0, '2026-10-16T12:00:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (
                'req-3', 5000, 0, 0, 0, 500, 500, NULL, 6000
            )
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, api_equivalent_usd, cache_savings_usd, pricing_status, calculated_at
            ) VALUES (
                'req-3', NULL, NULL, 'unpriced', '2026-10-16T12:00:00Z'
            )
            """
        )

        self.conn.commit()

    def test_cache_scenario_validation_and_math(self) -> None:
        """Tests parameter validation and break-even math in cache_scenario."""
        unavailable = cache_scenario({})
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(
            unavailable["missing_rate_assumptions"],
            ["fresh_rate_per_m", "read_rate_per_m", "write_5m_rate_per_m", "write_1h_rate_per_m"],
        )
        self.assertIsNone(unavailable["single_block_projection"].get("no_cache_cost_usd"))
        self.assertIsNone(unavailable["baseline_evaluation"])

        # 1. Invalid fresh rate
        with self.assertRaises(ValueError):
            cache_scenario({"fresh_rate_per_m": 0.0})
        with self.assertRaises(ValueError):
            cache_scenario({"fresh_rate_per_m": -1.0})

        # 2. Invalid read rate (> fresh rate)
        with self.assertRaises(ValueError):
            cache_scenario({"fresh_rate_per_m": 3.0, "read_rate_per_m": 4.0})

        # 3. Invalid write rate (< fresh rate)
        with self.assertRaises(ValueError):
            cache_scenario({"fresh_rate_per_m": 3.0, "write_5m_rate_per_m": 2.5})
        with self.assertRaises(ValueError):
            cache_scenario({"fresh_rate_per_m": 3.0, "write_1h_rate_per_m": 2.0})

        # Explicit hypothetical rates: fresh = 3.0, read = 0.30 -> discount = 2.70
        # write_5m = 3.75 -> premium = 0.75 -> break_even = 0.2778
        # write_1h = 6.00 -> premium = 3.00 -> break_even = 1.1111
        res = cache_scenario({
            "fresh_rate_per_m": 3.0,
            "read_rate_per_m": 0.30,
            "write_5m_rate_per_m": 3.75,
            "write_1h_rate_per_m": 6.00,
            "reuse_count": 3.0,
            "token_count": 1_000_000,
        })
        self.assertEqual(res["status"], "available")
        self.assertEqual(res["rate_basis"], "user-supplied hypothetical rates")

        self.assertAlmostEqual(res["break_even_reuses"]["ttl_5m"], 0.2778, places=4)
        self.assertAlmostEqual(res["break_even_reuses"]["ttl_1h"], 1.1111, places=4)
        self.assertIsNone(res["break_even_reuses"]["ttl_unknown"])

        # Check non-causality disclaimer
        self.assertFalse(res["policy_assumptions"]["causality_claim"])
        self.assertIn("Does not assert or prove", res["policy_assumptions"]["disclaimer"])

        # Check single block projection
        # 1M tokens, 3 reuses -> no-cache = 1M * 3.0 * (1 + 3) = $12.00
        # 5m cache = 1M * (3.75 + 3 * 0.30) = $4.65 -> savings = $7.35
        # 1h cache = 1M * (6.00 + 3 * 0.30) = $6.90 -> savings = $5.10
        sb = res["single_block_projection"]
        self.assertEqual(sb["no_cache_cost_usd"], 12.0)
        self.assertEqual(sb["ttl_5m"]["with_cache_cost_usd"], 4.65)
        self.assertEqual(sb["ttl_5m"]["net_savings_usd"], 7.35)
        self.assertTrue(sb["ttl_5m"]["is_profitable"])
        self.assertEqual(sb["ttl_1h"]["with_cache_cost_usd"], 6.9)
        self.assertEqual(sb["ttl_1h"]["net_savings_usd"], 5.1)
        self.assertTrue(sb["ttl_1h"]["is_profitable"])

    def test_analyze_disjoint_tokens_and_nonadditive_reasoning(self) -> None:
        """Tests that disjoint counters add accurately and reasoning tokens are not double-counted."""
        self._insert_fixture_data()

        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "tokens",
        }
        res = analyze(self.conn, envelope)

        vals = res["values"]
        # req-1: fresh=4000, read=6000, w5=2000, w1=0, wunk=0, out=1000, reasoning=300 (tot=13000)
        # req-2: fresh=1000, read=9000, w5=0, w1=3000, wunk=0, out=2000, reasoning=0 (tot=15000)
        # req-3: fresh=5000, read=0, w5=0, w1=0, wunk=500, out=500, reasoning=None (tot=6000)
        # Totals:
        # fresh: 4000 + 1000 + 5000 = 10000
        # read: 6000 + 9000 + 0 = 15000
        # w5: 2000 + 0 + 0 = 2000
        # w1: 0 + 3000 + 0 = 3000
        # wunk: 0 + 0 + 500 = 500
        # out: 1000 + 2000 + 500 = 3500
        # Disjoint Total = 10000 + 15000 + 2000 + 3000 + 500 + 3500 = 34000
        self.assertEqual(vals["fresh_input_tokens"], 10000)
        self.assertEqual(vals["cache_read_tokens"], 15000)
        self.assertEqual(vals["cache_write_5m_tokens"], 2000)
        self.assertEqual(vals["cache_write_1h_tokens"], 3000)
        self.assertEqual(vals["cache_write_unknown_ttl_tokens"], 500)
        self.assertEqual(vals["output_tokens"], 3500)
        self.assertEqual(vals["total_tokens"], 34000)
        self.assertEqual(vals["total_tokens_status"], "exact")

        # Reasoning tokens is 300; it is NOT added to disjoint total or output
        self.assertEqual(vals["reasoning_tokens"], 300)
        self.assertAlmostEqual(vals["reasoning_percentage_of_output"], (300 / 3500) * 100.0, places=2)

        # Reported source total
        self.assertEqual(vals["reported_total_tokens"], 13000 + 15000 + 6000)

        unavailable_break_even = res["break_even"]
        self.assertEqual(unavailable_break_even["status"], "unavailable")
        self.assertIsNone(unavailable_break_even["baseline_evaluation"])
        self.assertIsNone(unavailable_break_even["single_block_projection"].get("no_cache_cost_usd"))
        self.assertIn("fresh_rate_per_m", unavailable_break_even["missing_rate_assumptions"])

        rated = analyze(
            self.conn,
            {
                **envelope,
                "simulator_parameters": {
                    "fresh_rate_per_m": 3.0,
                    "read_rate_per_m": 0.30,
                    "write_5m_rate_per_m": 3.75,
                    "write_1h_rate_per_m": 6.00,
                },
            },
        )
        baseline = rated["break_even"]["baseline_evaluation"]
        self.assertEqual(rated["break_even"]["status"], "available")
        self.assertEqual(baseline["cache_write_unknown_ttl_tokens"], 500)
        self.assertEqual(baseline["simulated_with_cache_cost_usd"], 0.06)
        self.assertEqual(baseline["simulated_no_cache_cost_usd"], 0.075)
        self.assertEqual(baseline["simulated_net_savings_usd"], 0.015)
        self.assertIn("Unknown TTL tokens remain unpriced", baseline["unknown_ttl_note"])

        # Percentages
        pcts = vals["percentages"]
        self.assertAlmostEqual(pcts["fresh_input"], (10000 / 34000) * 100.0, places=2)
        self.assertAlmostEqual(pcts["cache_read"], (15000 / 34000) * 100.0, places=2)
        self.assertAlmostEqual(pcts["output"], (3500 / 34000) * 100.0, places=2)

    def test_cache_hit_rate_denominator(self) -> None:
        """Tests that cache hit rate denominator is strictly fresh_input + cache_read."""
        self._insert_fixture_data()

        envelope = {"contract_version": "1"}
        res = analyze(self.conn, envelope)

        vals = res["values"]
        # fresh = 10000, read = 15000
        # eligible prompt = 25000
        # hit rate = 15000 / 25000 = 0.6000
        self.assertEqual(vals["eligible_prompt_tokens"], 25000)
        self.assertEqual(vals["cache_hit_rate"], 0.6)
        self.assertEqual(res["cache_efficiency"]["eligible_denominator_formula"], "fresh_input_tokens + cache_read_tokens")

    def test_priced_vs_unpriced_no_cache_estimates(self) -> None:
        """Tests that unpriced requests are tracked separately and not conflated with $0.00."""
        self._insert_fixture_data()

        envelope = {"contract_version": "1"}
        res = analyze(self.conn, envelope)

        vals = res["values"]
        # req-1: api_usd = 0.0405, savings = 0.0162 -> no_cache = 0.0567
        # req-2: api_usd = 0.0540, savings = 0.0243 -> no_cache = 0.0783
        # req-3: unpriced (NULL)
        # Sum api_usd = 0.0945
        # Sum savings = 0.0405
        # Sum no_cache = 0.1350
        # Difference = 0.0405
        self.assertEqual(vals["priced_requests_count"], 2)
        self.assertEqual(vals["unpriced_requests_count"], 1)
        self.assertAlmostEqual(vals["api_equivalent_usd"], 0.0945, places=4)
        self.assertAlmostEqual(vals["cache_savings_usd"], 0.0405, places=4)
        self.assertAlmostEqual(vals["no_cache_estimated_usd"], 0.1350, places=4)
        self.assertAlmostEqual(vals["no_cache_cost_difference_usd"], 0.0405, places=4)

        # Warning about unpriced request
        self.assertTrue(any("lack pricing rate cards" in w for w in res["warnings"]))

    def test_null_counters_and_all_null_request(self) -> None:
        """Tests null counter preservation and all-null request yields None total."""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, request_status, request_timestamp, created_at
            ) VALUES (
                'req-null', 'unknown', 'default', 'omp', 'unknown',
                'unknown', 'unknown', 'ok', '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (
                'req-null', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
            )
            """
        )
        self.conn.commit()

        envelope = {
            "contract_version": "1",
            "filters": {"model": ["unknown"]},
        }
        res = analyze(self.conn, envelope)
        vals = res["values"]

        # When all counters are null, total is None and status is 'unavailable'
        self.assertIsNone(vals["total_tokens"])
        self.assertEqual(vals["total_tokens_status"], "unavailable")
        self.assertIsNone(vals["fresh_input_tokens"])
        self.assertIsNone(vals["cache_read_tokens"])
        self.assertIsNone(vals["output_tokens"])
        self.assertEqual(res["missingness"]["null_counters_requests_count"], 1)


    def test_mixed_counter_coverage_is_partial_and_preserves_reported_total(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, request_status, request_timestamp, created_at
            ) VALUES (
                'req-mixed', 'anthropic', 'default', 'claude-code', 'default',
                'claude-3-5-sonnet', 'claude-3-5-sonnet', 'ok',
                '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES ('req-mixed', 100, 0, NULL, NULL, NULL, 20, NULL, 1234)
            """
        )
        self.conn.commit()

        res = analyze(self.conn, {"contract_version": "1"})
        vals = res["values"]

        self.assertEqual(vals["fresh_input_tokens"], 100)
        self.assertEqual(vals["cache_read_tokens"], 0)
        self.assertIsNone(vals["cache_write_5m_tokens"])
        self.assertIsNone(vals["cache_write_1h_tokens"])
        self.assertIsNone(vals["cache_write_unknown_ttl_tokens"])
        self.assertEqual(vals["output_tokens"], 20)
        self.assertEqual(vals["total_tokens"], 120)
        self.assertEqual(vals["total_tokens_status"], "partial")
        self.assertEqual(vals["reported_total_tokens"], 1234)
        self.assertEqual(res["missingness"]["null_counters_requests_count"], 0)
        self.assertEqual(res["missingness"]["partial_counters_requests_count"], 1)
        self.assertEqual(res["rows"][0]["total_tokens_status"], "partial")

    def test_one_cache_write_bucket_counts_as_observed(self) -> None:
        """Cache-write buckets are alternatives; one filled bucket makes the request exact."""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, request_status, request_timestamp, created_at
            ) VALUES (
                'req-one-bucket', 'anthropic', 'default', 'claude-code', 'default',
                'claude-3-5-sonnet', 'claude-3-5-sonnet', 'ok',
                '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES ('req-one-bucket', 100, 0, NULL, 0, NULL, 20, NULL, NULL)
            """
        )
        self.conn.commit()

        res = analyze(self.conn, {"contract_version": "1"})
        self.assertEqual(res["values"]["total_tokens"], 120)
        self.assertEqual(res["values"]["total_tokens_status"], "exact")
        self.assertEqual(res["missingness"]["partial_counters_requests_count"], 0)
        self.assertEqual(res["rows"][0]["total_tokens_status"], "exact")

    def test_unresolved_overlap_flagging(self) -> None:
        """Tests that unresolved cross-source overlap flags partial status and warning."""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, request_status, request_timestamp,
                is_unresolved_overlap, created_at
            ) VALUES (
                'req-overlap', 'anthropic', 'default', 'claude-code', 'default',
                'claude-3-5-sonnet', 'claude-3-5-sonnet', 'ok', '2026-10-20T10:00:00Z',
                1, '2026-10-20T10:00:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens
            ) VALUES (
                'req-overlap', 100, 200, 0, 0, 0, 50
            )
            """
        )
        self.conn.commit()

        envelope = {"contract_version": "1"}
        res = analyze(self.conn, envelope)
        vals = res["values"]

        self.assertEqual(vals["total_tokens_status"], "partial")
        self.assertTrue(res["coverage"]["has_unresolved_overlap"])
        self.assertEqual(res["coverage"]["unresolved_overlap_requests_count"], 1)
        self.assertTrue(any("unresolved cross-source overlap" in w for w in res["warnings"]))

    def test_groupings_and_trends(self) -> None:
        """Tests group_by model and day trends."""
        self._insert_fixture_data()

        # Group by model
        envelope_model = {
            "contract_version": "1",
            "group_by": ["model"],
        }
        res_model = analyze(self.conn, envelope_model)
        rows = res_model["rows"]
        # Models should be claude-3-5-sonnet (2 reqs) and gemini-3-8-flash (1 req)
        model_keys = {r["group_key"] for r in rows}
        self.assertIn("claude-3-5-sonnet", model_keys)
        self.assertIn("gemini-3-8-flash", model_keys)

        # Daily trends
        trends = res_model["trends"]
        self.assertEqual(len(trends), 2)  # 2026-10-15 and 2026-10-16
        day_keys = [t["day"] for t in trends]
        self.assertEqual(day_keys, ["2026-10-15", "2026-10-16"])


if __name__ == "__main__":
    unittest.main()
