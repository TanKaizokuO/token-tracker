"""Substantive unit and integration tests for WP13a/b Model Metrics & Comparison Analytics.

Covers:
1. Full all-six canonical display identities accessible in profiles with 0-request unobserved handling.
2. Disjoint billable token counters, non-additive reasoning subset preservation, cache hit rate prompt denominator.
3. Cost per request and priced vs unpriced sample separation.
4. Deep TTFT and timing distributions (p50, p90, p95 nearest-rank percentiles, untimed exclusion).
5. Side-by-side comparison of >= 2 models with sparsity disclaimers and metric alignment.
6. Task slices with reliable manual labels only (filtering out inferred labels), explicit missing effort setting.
7. Model switching timeline within ordered session requests with unknown cost/time labeling.
8. Reliability failure analysis, known-status denominators, missing-status caveats, and retry token tracking.
9. Consumer boundaries and temporary database smoke.
10. Session counts: distinct non-null, non-empty ids only; id-less groups report None, never a synthetic key.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from typing import Optional

from token_ledger.metrics.models import (
    SPARSE_SAMPLE_THRESHOLD,
    analyze,
    compute_percentiles,
)
from token_ledger.pricing import (
    CANONICAL_DISPLAY_IDENTITIES,
    GEMINI_3_8_FLASH,
    HAIKU_5_5,
    LUNA_MAX,
    OPUS_5_5,
    SOL_6_1,
    SONNET_5_5,
)
from token_ledger.store.database import open_database


class TestModelMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(":memory:")
        self._populate_fixtures()

    def tearDown(self) -> None:
        self.conn.close()

    def _populate_fixtures(self) -> None:
        cur = self.conn.cursor()

        # Insert project
        cur.execute(
            """
            INSERT INTO projects (project_id, project_name, account_alias, created_at)
            VALUES ('proj-alpha', 'Alpha Project', 'default', '2026-10-01T00:00:00Z'),
                   ('proj-beta', 'Beta Project', 'default', '2026-10-01T00:00:00Z')
            """
        )

        # Insert sessions
        cur.execute(
            """
            INSERT INTO sessions (session_id, origin_harness, project_id, started_at, created_at)
            VALUES ('sess-1', 'claude-code', 'proj-alpha', '2026-10-10T10:00:00Z', '2026-10-10T10:00:00Z'),
                   ('sess-2', 'oh-my-pi', 'proj-beta', '2026-10-10T11:00:00Z', '2026-10-10T11:00:00Z')
            """
        )

        # Session 1: Multi-request sequence with Model Switches: Sonnet 5.5 -> Opus 5.5 -> Sonnet 5.5
        # Request 1: Sonnet 5.5 (Timed, Priced)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, duration_ms, ttft_ms,
                generation_duration_ms, generation_speed_tps, created_at
            ) VALUES (
                'req-s1-1', 'anthropic', 'default', 'pr-s1-1', 'claude-code',
                'default', 'claude-sonnet-5-5-20251001', 'claude-sonnet-5-5', 'sess-1', 'proj-alpha',
                'ok', '2026-10-10T10:01:00Z', 'measured', 1200, 300, 900, 50.0, '2026-10-10T10:01:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens, reasoning_tokens,
                reported_total_tokens
            ) VALUES ('req-s1-1', 1000, 2000, 500, 0, 0, 45, 10, 3545)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES ('req-s1-1', 'priced', 0.05, 4.25, 0.05, 0.02, '2026-10-10T10:01:00Z')
            """
        )

        # Request 2: Opus 5.5 (Switch 1! Rate limited, Untimed, Unpriced)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, created_at
            ) VALUES (
                'req-s1-2', 'anthropic', 'default', 'pr-s1-2', 'claude-code',
                'default', 'claude-opus-5-5', 'claude-opus-5-5', 'sess-1', 'proj-alpha',
                'rate_limited', '2026-10-10T10:05:00Z', 'none', '2026-10-10T10:05:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens
            ) VALUES ('req-s1-2', 500, 0, 0)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, calculated_at
            ) VALUES ('req-s1-2', 'unpriced', '2026-10-10T10:05:00Z')
            """
        )

        # Request 3: Retry of Opus 5.5 with distinct ID and retry_attempt = 1 (Timed, Priced)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                parent_request_id, retry_attempt, request_status, request_timestamp,
                timing_basis, duration_ms, ttft_ms, generation_duration_ms, generation_speed_tps, created_at
            ) VALUES (
                'anthropic:default:pr-s1-2:retry_1', 'anthropic', 'default', 'pr-s1-2-retry-1', 'claude-code',
                'default', 'claude-opus-5-5', 'claude-opus-5-5', 'sess-1', 'proj-alpha',
                'req-s1-2', 1, 'ok', '2026-10-10T10:06:00Z',
                'measured', 2500, 800, 1700, 30.0, '2026-10-10T10:06:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens, reasoning_tokens
            ) VALUES ('anthropic:default:pr-s1-2:retry_1', 1200, 300, 51, 15)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES ('anthropic:default:pr-s1-2:retry_1', 'priced', 0.12, 10.20, 0.12, 0.01, '2026-10-10T10:06:00Z')
            """
        )

        # Request 4: Sonnet 5.5 (Switch 2 back to Sonnet! Timed, Priced)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, duration_ms, ttft_ms,
                generation_duration_ms, generation_speed_tps, created_at
            ) VALUES (
                'req-s1-4', 'anthropic', 'default', 'pr-s1-4', 'claude-code',
                'default', 'claude-sonnet-5-5', 'claude-sonnet-5-5', 'sess-1', 'proj-alpha',
                'ok', '2026-10-10T10:10:00Z', 'measured', 800, 200, 600, 60.0, '2026-10-10T10:10:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens, reasoning_tokens
            ) VALUES ('req-s1-4', 800, 1500, 36, 0)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES ('req-s1-4', 'priced', 0.03, 2.55, 0.03, 0.015, '2026-10-10T10:10:00Z')
            """
        )

        # Session 2: Requests for Sol 6.1 with effort tier
        # Request 5: Sol 6.1 with low effort tier
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, duration_ms, ttft_ms,
                generation_duration_ms, generation_speed_tps, created_at
            ) VALUES (
                'req-s2-1', 'openai', 'default', 'pr-s2-1', 'oh-my-pi',
                'default', 'gpt-6.1-sol-low', 'gpt-6.1-sol', 'sess-2', 'proj-beta',
                'ok', '2026-10-10T11:05:00Z', 'measured', 1500, 400, 1100, 45.0, '2026-10-10T11:05:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens, reasoning_tokens
            ) VALUES ('req-s2-1', 2000, 1000, 50, 20)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES ('req-s2-1', 'priced', 0.08, 6.80, 0.08, 0.01, '2026-10-10T11:05:00Z')
            """
        )

        # Request 6: Sol 6.1 with timeout error and unrecorded status request
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, created_at
            ) VALUES (
                'req-s2-2', 'openai', 'default', 'pr-s2-2', 'oh-my-pi',
                'default', 'gpt-6.1-sol', 'gpt-6.1-sol', 'sess-2', 'proj-beta',
                'timeout', '2026-10-10T11:10:00Z', 'none', '2026-10-10T11:10:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, output_tokens
            ) VALUES ('req-s2-2', 500, 0)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, calculated_at
            ) VALUES ('req-s2-2', 'unpriced', '2026-10-10T11:10:00Z')
            """
        )

        # Request 7: Sol 6.1 with missing/unknown status to test missing-status caveat
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, created_at
            ) VALUES (
                'req-s2-3', 'openai', 'default', 'pr-s2-3', 'oh-my-pi',
                'default', 'gpt-6.1-sol', 'gpt-6.1-sol', 'sess-2', 'proj-beta',
                'unknown_status', '2026-10-10T11:15:00Z', 'none', '2026-10-10T11:15:00Z'
            )
            """
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, output_tokens
            ) VALUES ('req-s2-3', 200, 0)
            """
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, calculated_at
            ) VALUES ('req-s2-3', 'unpriced', '2026-10-10T11:15:00Z')
            """
        )

        # Outcomes table entries:
        # One reliable manual outcome for req-s1-1
        cur.execute(
            """
            INSERT INTO manual_settings_and_outcomes (
                outcome_id, entity_type, entity_id, task_label, completion_rating,
                manual_rating, is_inferred, created_at, updated_at
            ) VALUES (
                'out-1', 'request', 'req-s1-1', 'refactoring', 'complete',
                5.0, 0, '2026-10-10T10:15:00Z', '2026-10-10T10:15:00Z'
            )
            """
        )
        # One INFERRED outcome for req-s2-1 (must be excluded from reliable manual labels)
        cur.execute(
            """
            INSERT INTO manual_settings_and_outcomes (
                outcome_id, entity_type, entity_id, task_label, completion_rating,
                manual_rating, is_inferred, inference_method, created_at, updated_at
            ) VALUES (
                'out-2', 'request', 'req-s2-1', 'feature_code', 'partial',
                3.0, 1, 'heuristic_rule', '2026-10-10T11:20:00Z', '2026-10-10T11:20:00Z'
            )
            """
        )

        self.conn.commit()

    def test_all_six_canonical_profiles_accessible(self) -> None:
        """Verifies that all 6 canonical display identities are in profiles even with 0 observations."""
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "model_profiles",
        }
        res = analyze(self.conn, envelope)

        self.assertEqual(res["contract_version"], "1")
        self.assertEqual(res["family"], "model_profiles")

        profiles = res["profiles"]
        for expected_name in CANONICAL_DISPLAY_IDENTITIES:
            self.assertIn(expected_name, profiles)
            prof = profiles[expected_name]
            self.assertEqual(prof["display_name"], expected_name)
            self.assertTrue(prof["is_canonical"])
            self.assertIn("requests_count", prof)
            self.assertIn("sample_counts", prof)
            self.assertIn("missingness", prof)

        # Observed models: Sonnet 5.5, Opus 5.5, Sol 6.1
        self.assertEqual(profiles[SONNET_5_5]["requests_count"], 2)
        self.assertEqual(profiles[OPUS_5_5]["requests_count"], 2)
        self.assertEqual(profiles[SOL_6_1]["requests_count"], 3)

        # Unobserved models: Haiku 5.5, Luna Max, Gemini 3.8 Flash
        self.assertEqual(profiles[HAIKU_5_5]["requests_count"], 0)
        self.assertEqual(profiles[LUNA_MAX]["requests_count"], 0)
        self.assertEqual(profiles[GEMINI_3_8_FLASH]["requests_count"], 0)

        # Unobserved profiles must maintain valid sample counts and missingness structure
        h_prof = profiles[HAIKU_5_5]
        self.assertEqual(h_prof["sample_counts"]["requests"], 0)
        self.assertIsNone(h_prof["cache"]["cache_hit_rate"])
        self.assertIsNone(h_prof["timing"]["weighted_tps"])
        self.assertIsNone(h_prof["deep_ttft"]["distribution_ms"]["p50"])

    def test_per_identity_token_counters_and_reasoning_subset(self) -> None:
        """Verifies disjoint billable sum, reasoning subset calculation, and cache hit rate denominator."""
        res = analyze(self.conn, {"contract_version": "1"})
        sonnet = res["profiles"][SONNET_5_5]

        # Total tokens: req1 (1000+2000+500+0+0+45 = 3545) + req4 (800+1500+0+0+0+36 = 2336) = 5881
        self.assertEqual(sonnet["tokens"]["total_tokens"], 5881)
        self.assertEqual(sonnet["tokens"]["fresh_input_tokens"], 1800)
        self.assertEqual(sonnet["tokens"]["cache_read_tokens"], 3500)
        self.assertEqual(sonnet["tokens"]["cache_write_5m_tokens"], 500)
        self.assertEqual(sonnet["tokens"]["output_tokens"], 81)
        self.assertEqual(sonnet["tokens"]["reasoning_tokens"], 10)

        # Reasoning percentage of output: 10 / 81 * 100 = 12.35%
        self.assertAlmostEqual(sonnet["tokens"]["reasoning_pct_of_output"], 12.35, places=2)

        # Cache hit rate denominator strictly fresh_input + cache_read = 1800 + 3500 = 5300
        # Hit rate = 3500 / 5300 = 0.6604
        self.assertAlmostEqual(sonnet["cache"]["cache_hit_rate"], 0.6604, places=3)
        self.assertEqual(sonnet["cache"]["eligible_prompt_tokens"], 5300)

    def test_deep_ttft_and_deterministic_percentiles(self) -> None:
        """Verifies TTFT p50, p90, p95 nearest-rank percentiles and independent latency distributions."""
        res = analyze(self.conn, {"contract_version": "1"})
        sonnet = res["profiles"][SONNET_5_5]

        # Sonnet has 2 timed requests: TTFT = [300, 200] -> sorted [200, 300]
        ttft_dist = sonnet["deep_ttft"]["distribution_ms"]
        self.assertEqual(ttft_dist["count"], 2)
        self.assertEqual(ttft_dist["min"], 200.0)
        self.assertEqual(ttft_dist["max"], 300.0)
        self.assertEqual(ttft_dist["p50"], 200.0)  # ceil(0.5 * 2) - 1 = 0 -> 200
        self.assertEqual(ttft_dist["p90"], 300.0)  # ceil(0.9 * 2) - 1 = 1 -> 300
        self.assertEqual(ttft_dist["p95"], 300.0)

        # Opus has 1 timed request (the retry) and 1 untimed request
        opus = res["profiles"][OPUS_5_5]
        self.assertEqual(opus["deep_ttft"]["eligible_ttft_count"], 1)
        self.assertEqual(opus["deep_ttft"]["distribution_ms"]["p50"], 800.0)
        self.assertEqual(opus["missingness"]["missing_ttft_count"], 1)

    def test_side_by_side_comparison_and_sparsity_label(self) -> None:
        """Verifies side-by-side comparison under identical filters and sparsity disclaimers."""
        res = analyze(self.conn, {"contract_version": "1"})
        comp = res["comparison"]

        models_comp = comp["models_compared"]
        self.assertGreaterEqual(len(models_comp), 2)

        # All models in this test fixture have < 5 requests, so all must be marked is_sparse = True
        for m in models_comp:
            self.assertTrue(m["is_sparse"])
            self.assertIsNotNone(m["sparisty_disclaimer"])
            self.assertIn("Sparse sample size", m["sparisty_disclaimer"])

        # Check required comparative metric fields exist
        m0 = models_comp[0]
        self.assertIn("tokens_per_request", m0)
        self.assertIn("output_per_request", m0)
        self.assertIn("weighted_tps", m0)
        self.assertIn("median_ttft_ms", m0)
        self.assertIn("retry_error_rate", m0)
        self.assertIn("cost_per_request_usd", m0)

    def test_task_slices_reliable_manual_labels_only(self) -> None:
        """Verifies manual vs inferred outcome filtering, and missing effort setting explicit labeling."""
        res = analyze(self.conn, {"contract_version": "1"})
        task_slices = res["task_slices"]["rows"]

        # Find the task slice for refactoring (manual outcome)
        refactor_slice = next((s for s in task_slices if s["task_label"] == "refactoring"), None)
        self.assertIsNotNone(refactor_slice)
        self.assertTrue(refactor_slice["is_reliable_manual_label"])
        self.assertEqual(refactor_slice["manual_outcome_rating"], 5.0)

        # The inferred outcome was for req-s2-1 ('feature_code') with is_inferred = 1
        feature_slice = next((s for s in task_slices if s["task_label"] == "feature_code"), None)
        self.assertIsNotNone(feature_slice)
        self.assertFalse(feature_slice["is_reliable_manual_label"])

        # Check effort tier: req-s2-1 was 'gpt-6.1-sol-low', so effort_setting should reflect 'low'
        self.assertEqual(feature_slice["effort_setting"], "low")

        # Sonnet requests had no effort tier, so effort_setting should be 'missing'
        self.assertEqual(refactor_slice["effort_setting"], "missing")

    def test_switching_timeline_within_session(self) -> None:
        """Verifies session-ordered model switching timeline with unknown cost/time labeling."""
        res = analyze(self.conn, {"contract_version": "1"})
        timeline = res["switching_timeline"]

        self.assertEqual(timeline["total_switches"], 2)
        events = timeline["events"]

        # Switch 1: Sonnet -> Opus
        # Switch 2: Opus -> Sonnet
        switch_sonnet_to_opus = next((e for e in events if e["from_model"] == SONNET_5_5 and e["to_model"] == OPUS_5_5), None)
        self.assertIsNotNone(switch_sonnet_to_opus)
        self.assertEqual(switch_sonnet_to_opus["prior_request_canonical_id"], "req-s1-1")
        self.assertEqual(switch_sonnet_to_opus["prior_request_cost_usd"], 0.05)
        self.assertEqual(switch_sonnet_to_opus["prior_request_duration_ms"], 1200)

        # Switch 2: Opus -> Sonnet (prior request was the Opus retry)
        switch_opus_to_sonnet = next((e for e in events if e["from_model"] == OPUS_5_5 and e["to_model"] == SONNET_5_5), None)
        self.assertIsNotNone(switch_opus_to_sonnet)
        self.assertEqual(switch_opus_to_sonnet["triggered_by_canonical_id"], "req-s1-4")

    def test_reliability_known_denominator_and_missing_status_caveat(self) -> None:
        """Verifies failure tracking, denominator exclusion, missing-status caveat, and retry tokens."""
        res = analyze(self.conn, {"contract_version": "1"})
        sol = res["profiles"][SOL_6_1]
        rel = sol["reliability"]

        # Sol had 3 requests: 1 'ok', 1 'timeout', 1 'unknown_status'
        self.assertEqual(rel["total_requests"], 3)
        self.assertEqual(rel["known_status_requests"], 2)  # ok + timeout
        self.assertEqual(rel["missing_status_requests"], 1)
        self.assertEqual(rel["timeout_count"], 1)
        self.assertEqual(rel["ok_count"], 1)

        # Error rate denominator is strictly known_status_requests (1 failure / 2 known = 0.50)
        self.assertEqual(rel["error_rate"], 0.50)
        self.assertIsNotNone(rel["missing_status_caveat"])
        self.assertIn("1 request(s) have unrecorded or missing status", rel["missing_status_caveat"])

        # Retry tokens for Opus 5.5: req-s1-2 retry has 1200 fresh + 300 read + 51 out = 1551 tokens
        opus = res["profiles"][OPUS_5_5]
        self.assertEqual(opus["reliability"]["retry_requests_count"], 1)
        self.assertEqual(opus["reliability"]["retry_tokens"], 1551)

    def _insert_request(
        self,
        canonical_id: str,
        *,
        provider: str,
        raw_model: str,
        normalized_model: str,
        session_id: Optional[str],
        timestamp: str,
    ) -> None:
        """Inserts one minimal, unpriced request with 100 fresh input and 10 output tokens."""
        self.conn.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, created_at
            ) VALUES (?, ?, 'default', ?, 'claude-code', 'default', ?, ?, ?, 'proj-alpha',
                      'ok', ?, 'none', ?)
            """,
            (canonical_id, provider, canonical_id + "-pr", raw_model, normalized_model,
             session_id, timestamp, timestamp),
        )
        self.conn.execute(
            "INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens) "
            "VALUES (?, 100, 0, 10)",
            (canonical_id,),
        )

    def test_null_only_session_ids_give_unknown_session_count(self) -> None:
        """A model whose requests have no session ids reports unknown (None) sessions, not zero or a synthetic one."""
        # An empty id is not a session either; the sessions row exists only to satisfy the FK.
        self.conn.execute(
            "INSERT INTO sessions (session_id, origin_harness, project_id, started_at, created_at) "
            "VALUES ('', 'claude-code', 'proj-alpha', '2026-10-13T08:00:00Z', '2026-10-13T08:00:00Z')"
        )
        for cid, ts, sid in (
            ("req-haiku-1", "2026-10-12T09:00:00Z", None),
            ("req-haiku-2", "2026-10-12T09:30:00Z", None),
            ("req-haiku-3", "2026-10-13T09:00:00Z", ""),
        ):
            self._insert_request(cid, provider="anthropic", raw_model="claude-haiku-5-5",
                                 normalized_model="claude-haiku-5-5", session_id=sid, timestamp=ts)
        self.conn.commit()

        res = analyze(self.conn, {"contract_version": "1"})
        haiku = res["profiles"][HAIKU_5_5]

        # Requests and tokens still count everywhere.
        self.assertEqual(haiku["requests_count"], 3)
        self.assertEqual(haiku["tokens"]["total_tokens"], 330)

        self.assertIsNone(haiku["sessions_count"])
        self.assertIsNone(haiku["sample_counts"]["sessions"])
        self.assertEqual(haiku["session_mix"], [])
        haiku_row = next(r for r in res["rows"] if r["display_name"] == HAIKU_5_5)
        self.assertIsNone(haiku_row["sessions_count"])

        trends = {d["date"]: d for d in haiku["session_trends"]}
        self.assertIsNone(trends["2026-10-12"]["sessions_count"])
        self.assertEqual(trends["2026-10-12"]["requests_count"], 2)
        self.assertIsNone(trends["2026-10-13"]["sessions_count"])
        self.assertEqual(trends["2026-10-13"]["requests_count"], 1)

        # Models with no requests keep a count of zero, not None.
        self.assertEqual(res["profiles"][LUNA_MAX]["sessions_count"], 0)

    def test_mixed_group_counts_only_distinct_non_null_ids(self) -> None:
        """Null and empty ids are skipped; the remaining distinct ids are the session count, per model and per day."""
        self.conn.execute(
            "INSERT INTO sessions (session_id, origin_harness, project_id, started_at, created_at) "
            "VALUES ('', 'claude-code', 'proj-alpha', '2026-10-12T08:00:00Z', '2026-10-12T08:00:00Z')"
        )
        rows = (
            ("req-g-1", "sess-1", "2026-10-12T09:00:00Z"),
            ("req-g-2", "sess-1", "2026-10-12T09:05:00Z"),
            ("req-g-3", "sess-2", "2026-10-12T09:10:00Z"),
            ("req-g-4", None, "2026-10-12T09:15:00Z"),
            ("req-g-5", "", "2026-10-12T09:20:00Z"),
            ("req-g-6", None, "2026-10-13T09:00:00Z"),
        )
        for cid, sid, ts in rows:
            self._insert_request(cid, provider="google", raw_model="gemini-3.8-flash",
                                 normalized_model="gemini-3.8-flash", session_id=sid, timestamp=ts)
        self.conn.commit()

        res = analyze(self.conn, {"contract_version": "1"})
        gemini = res["profiles"][GEMINI_3_8_FLASH]

        self.assertEqual(gemini["requests_count"], 6)
        self.assertEqual(gemini["sessions_count"], 2)
        self.assertEqual(gemini["sample_counts"]["sessions"], 2)
        self.assertEqual({s["session_id"] for s in gemini["session_mix"]}, {"sess-1", "sess-2"})
        self.assertEqual(gemini["session_mix"][0]["requests_count"] + gemini["session_mix"][1]["requests_count"], 3)

        trends = {d["date"]: d for d in gemini["session_trends"]}
        # Day with ids: 2 distinct sessions across 5 requests (the 2 id-less ones still count as requests).
        self.assertEqual(trends["2026-10-12"]["sessions_count"], 2)
        self.assertEqual(trends["2026-10-12"]["requests_count"], 5)
        # Day with only a null id: unknown, not zero.
        self.assertIsNone(trends["2026-10-13"]["sessions_count"])
        self.assertEqual(trends["2026-10-13"]["requests_count"], 1)

    def test_unknown_session_sentinel_absent_from_output(self) -> None:
        """The old 'unknown_session' placeholder must not appear anywhere in the serialized result."""
        self._insert_request("req-null-1", provider="google", raw_model="gemini-3.8-flash",
                             normalized_model="gemini-3.8-flash", session_id=None,
                             timestamp="2026-10-12T10:00:00Z")
        self.conn.commit()

        res = analyze(self.conn, {"contract_version": "1"})
        payload = json.dumps(res, default=str)
        self.assertNotIn("unknown_session", payload)
        self.assertIsNone(res["profiles"][GEMINI_3_8_FLASH]["sessions_count"])


if __name__ == "__main__":
    unittest.main()
