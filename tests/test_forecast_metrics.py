"""Comprehensive unit and integration tests for WP20 Forecasting, Anomalies, and Comparisons."""

from datetime import datetime, timezone
import unittest

from token_ledger.metrics.forecast import (
    analyze,
    compute_daily_rollups,
    detect_anomalies,
    forecast_usage,
    walk_forward_backtest,
)
from token_ledger.store import open_database


class TestForecastMetricsUnit(unittest.TestCase):
    def test_dst_day_fraction_uses_utc_elapsed_duration(self):
        """Local calendar days measure elapsed time across spring/fall DST shifts."""
        timezone_name = "America/New_York"
        spring_rows = [
            {"canonical_id": f"spring-{day}", "request_timestamp": f"2026-03-{day:02d}T15:00:00Z", "total_tokens": 100}
            for day in (4, 5, 6)
        ]
        fall_rows = [
            {"canonical_id": f"fall-{day}", "request_timestamp": f"2026-10-{day:02d}T15:00:00Z", "total_tokens": 100}
            for day in (28, 29, 30)
        ]

        spring = forecast_usage(
            spring_rows,
            tz_name=timezone_name,
            as_of=datetime.fromisoformat("2026-03-08T12:00:00-04:00"),
        )
        fall = forecast_usage(
            fall_rows,
            tz_name=timezone_name,
            as_of=datetime.fromisoformat("2026-11-01T12:00:00-05:00"),
        )

        self.assertEqual(spring["partial_day"]["elapsed_seconds"], 11 * 60 * 60)
        self.assertAlmostEqual(
            spring["partial_day"]["day_fraction_elapsed"], 11 / 23, places=4
        )
        self.assertEqual(fall["partial_day"]["elapsed_seconds"], 13 * 60 * 60)
        self.assertAlmostEqual(
            fall["partial_day"]["day_fraction_elapsed"], 13 / 25, places=4
        )

    def test_missing_activity_days_are_unknown_not_zero(self):
        rows = [
            {
                "canonical_id": "coverage-1",
                "request_timestamp": "2026-10-01T10:00:00Z",
                "total_tokens": 100,
                "api_equivalent_usd": 0.01,
                "pricing_status": "priced",
            },
            {
                "canonical_id": "coverage-zero",
                "request_timestamp": "2026-10-03T10:00:00Z",
                "total_tokens": 0,
                "api_equivalent_usd": 0.0,
                "pricing_status": "priced",
            },
            {
                "canonical_id": "coverage-3",
                "request_timestamp": "2026-10-05T10:00:00Z",
                "total_tokens": 100,
                "api_equivalent_usd": 0.01,
                "pricing_status": "priced",
            },
        ]

        daily = compute_daily_rollups(rows)
        self.assertEqual(daily["2026-10-03"]["total_tokens"], 0)
        self.assertEqual(daily["2026-10-03"]["requests_count"], 1)

        result = forecast_usage(
            rows,
            as_of=datetime.fromisoformat("2026-10-05T12:00:00+00:00"),
        )
        self.assertEqual(result["coverage"]["activity_days_observed"], 3)
        self.assertEqual(result["coverage"]["unknown_days_between_activity_days"], 2)
        self.assertEqual(result["coverage"]["confirmed_zero_activity_days"], 0)
        self.assertEqual(result["coverage"]["missing_days_imputed_as_zero"], 0)

        backtest = walk_forward_backtest([100.0, 0.0, 100.0, 0.0, 100.0])
        self.assertEqual(backtest["samples_evaluated"], 2)
        self.assertTrue(backtest["uncertainty_eligible"])

    def test_sparse_forecast_abstains(self):
        """Sparse history (< 3 days) must abstain with clear reasoning."""
        sparse_rows = [
            {
                "canonical_id": "req-1",
                "request_timestamp": "2026-10-20T10:00:00Z",
                "normalized_model": "claude-3-5-sonnet",
                "total_tokens": 1500,
                "fresh_input_tokens": 1000,
                "cache_read_tokens": 200,
                "output_tokens": 300,
                "api_equivalent_usd": 0.05,
                "pricing_status": "priced",
            },
            {
                "canonical_id": "req-2",
                "request_timestamp": "2026-10-21T10:00:00Z",
                "normalized_model": "claude-3-5-sonnet",
                "total_tokens": 2000,
                "fresh_input_tokens": 1500,
                "cache_read_tokens": 0,
                "output_tokens": 500,
                "api_equivalent_usd": 0.07,
                "pricing_status": "priced",
            },
        ]
        res = forecast_usage(sparse_rows, tz_name="UTC")
        self.assertFalse(res["eligible"])
        self.assertEqual(res["status"], "abstain")
        self.assertIn("Sparse history", res["abstention_reason"])
        self.assertIsNone(res["forecasts"])
        self.assertEqual(res["active_days_observed"], 2)

    def test_dense_forecast_and_uncertainty_intervals(self):
        """Dense history generates remainder-of-day/week/month with uncertainty intervals."""
        dense_rows = []
        for day in range(10, 20):
            d_str = f"2026-10-{day:02d}"
            dense_rows.append({
                "canonical_id": f"req-d{day}-1",
                "request_timestamp": f"{d_str}T09:00:00Z",
                "normalized_model": "claude-3-5-sonnet",
                "total_tokens": 10000,
                "fresh_input_tokens": 7000,
                "cache_read_tokens": 2000,
                "output_tokens": 1000,
                "api_equivalent_usd": 0.30,
                "pricing_status": "priced",
            })
            dense_rows.append({
                "canonical_id": f"req-d{day}-2",
                "request_timestamp": f"{d_str}T14:00:00Z",
                "normalized_model": "claude-3-5-haiku",
                "total_tokens": 5000,
                "fresh_input_tokens": 3000,
                "cache_read_tokens": 1000,
                "output_tokens": 1000,
                "api_equivalent_usd": 0.05,
                "pricing_status": "priced",
            })

        as_of_dt = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
        res = forecast_usage(dense_rows, tz_name="UTC", as_of=as_of_dt)

        self.assertTrue(res["eligible"])
        self.assertEqual(res["status"], "forecast_available")
        self.assertEqual(res["active_days_observed"], 10)

        vel = res["velocity"]
        self.assertIsNotNone(vel)
        self.assertEqual(vel["method"], "recent_weighted_daily_velocity")
        self.assertAlmostEqual(vel["recent_daily_tokens"], 15000.0, delta=100.0)

        partial = res["partial_day"]
        self.assertEqual(partial["date"], "2026-10-19")
        self.assertAlmostEqual(partial["day_fraction_elapsed"], 0.5, places=2)
        self.assertAlmostEqual(partial["day_fraction_remaining"], 0.5, places=2)

        fc_day = res["forecasts"]["remainder_of_day"]
        self.assertGreater(fc_day["expected_tokens"], 0)
        self.assertGreater(fc_day["projected_day_total_tokens"], fc_day["expected_tokens"])

        interval = fc_day["uncertainty_interval"]
        self.assertLessEqual(interval["lower_bound"], interval["expected_point"])
        self.assertGreaterEqual(interval["upper_bound"], interval["expected_point"])
        self.assertEqual(interval["method"], "empirical_residual_spread")

        per_model = res["forecasts"]["per_model"]
        self.assertIn("claude-3-5-sonnet", per_model)
        self.assertIn("claude-3-5-haiku", per_model)
        self.assertGreater(per_model["claude-3-5-sonnet"]["projected_month_tokens"], 0)

    def test_walk_forward_backtest(self):
        """Backtest evaluates rolling walk-forward errors correctly."""
        series = [100.0, 120.0, 110.0, 130.0, 140.0, 135.0, 150.0]
        bt = walk_forward_backtest(series, min_train_days=3, window_size=3)
        self.assertTrue(bt["eligible"])
        self.assertEqual(bt["method"], "rolling_walk_forward_1step")
        self.assertEqual(bt["samples_evaluated"], 4)
        self.assertGreater(bt["mae"], 0.0)
        self.assertGreater(bt["mape"], 0.0)
        self.assertIsNotNone(bt["residual_stddev"])
        sparse_uncertainty = walk_forward_backtest([10.0, 20.0, 15.0, 25.0])
        self.assertTrue(sparse_uncertainty["eligible"])
        self.assertEqual(sparse_uncertainty["samples_evaluated"], 1)
        self.assertFalse(sparse_uncertainty["uncertainty_eligible"])

    def test_forecast_uncertainty_abstains_with_one_residual(self):
        rows = [
            {
                "canonical_id": f"single-residual-{day}",
                "request_timestamp": f"2026-10-{day:02d}T10:00:00Z",
                "total_tokens": 1000 + day * 100,
                "api_equivalent_usd": day * 0.01,
                "pricing_status": "priced",
            }
            for day in range(1, 5)
        ]
        result = forecast_usage(
            rows,
            as_of=datetime.fromisoformat("2026-10-05T12:00:00+00:00"),
        )

        self.assertEqual(result["backtest"]["samples_evaluated"], 1)
        self.assertFalse(result["backtest"]["uncertainty_eligible"])
        token_interval = result["forecasts"]["remainder_of_day"]["uncertainty_interval"]
        self.assertEqual(token_interval["status"], "abstain")
        self.assertEqual(token_interval["samples_evaluated"], 1)
        cost_interval = result["forecasts"]["remainder_of_day"]["cost_uncertainty_interval"]
        self.assertEqual(cost_interval["status"], "abstain")
        self.assertEqual(cost_interval["samples_evaluated"], 1)



    def test_partial_pricing_handling(self):
        """Unpriced requests are separated and do not default to $0.00 cost."""
        rows = [
            {
                "canonical_id": "req-p1",
                "request_timestamp": "2026-10-01T10:00:00Z",
                "normalized_model": "claude-3-5-sonnet",
                "total_tokens": 1000,
                "api_equivalent_usd": 0.03,
                "pricing_status": "priced",
            },
            {
                "canonical_id": "req-u1",
                "request_timestamp": "2026-10-02T10:00:00Z",
                "normalized_model": "experimental-custom-model",
                "total_tokens": 2000,
                "api_equivalent_usd": None,
                "pricing_status": "unpriced",
            },
            {
                "canonical_id": "req-p2",
                "request_timestamp": "2026-10-03T10:00:00Z",
                "normalized_model": "claude-3-5-sonnet",
                "total_tokens": 1500,
                "api_equivalent_usd": 0.045,
                "pricing_status": "priced",
            },
        ]
        res = forecast_usage(rows, tz_name="UTC")
        self.assertTrue(res["eligible"])
        self.assertEqual(res["coverage"]["unpriced_requests_count"], 1)
        self.assertEqual(res["coverage"]["priced_coverage_status"], "partial")
        self.assertEqual(
            res["coverage"]["full_scope_cost_projection_status"],
            "abstain_partial_pricing",
        )
        self.assertFalse(res["coverage"]["cost_projection_is_full_scope"])
        self.assertEqual(
            res["coverage"]["cost_projection_status"], "known_priced_subset"
        )
        self.assertIsNotNone(
            res["forecasts"]["remainder_of_day"]["expected_cost_usd"]
        )
        self.assertEqual(
            res["forecasts"]["remainder_of_day"]["cost_uncertainty_interval"][
                "full_scope_cost_projection_status"
            ],
            "abstain_partial_pricing",
        )
        self.assertIn("full-scope", res["coverage"]["full_scope_cost_abstention_reason"].lower())
        self.assertTrue(any("unpriced" in w for w in res["warnings"]))

    def test_cost_forecast_abstains_without_priced_history(self):
        rows = [
            {
                "canonical_id": f"req-unpriced-{day}",
                "request_timestamp": f"2026-10-{day:02d}T10:00:00Z",
                "total_tokens": 1000,
                "api_equivalent_usd": None,
                "pricing_status": "unpriced",
            }
            for day in range(1, 5)
        ]

        result = forecast_usage(rows, as_of=datetime.fromisoformat("2026-10-04T12:00:00+00:00"))
        day_forecast = result["forecasts"]["remainder_of_day"]
        self.assertEqual(result["coverage"]["cost_projection_status"], "abstain_no_priced_history")
        self.assertIsNone(day_forecast["expected_cost_usd"])
        self.assertIsNone(day_forecast["projected_day_cost_usd"])
        self.assertEqual(
            day_forecast["cost_uncertainty_interval"]["full_scope_cost_projection_status"],
            "abstain_no_priced_history",
        )


    def test_anomaly_detection_sparse_and_dense(self):
        """Anomaly detection abstains when samples < 5 and flags outliers when dense."""
        sparse_rows = [
            {
                "canonical_id": f"req-sp-{i}",
                "request_timestamp": f"2026-10-01T1{i}:00:00Z",
                "total_tokens": 1000,
                "timing_basis": "measured",
                "generation_duration_ms": 1000,
                "generation_speed_tps": 50.0,
            }
            for i in range(4)
        ]
        res_sparse = detect_anomalies(None, sparse_rows)
        self.assertFalse(res_sparse["baselines"]["total_tokens"]["eligible"])
        self.assertFalse(res_sparse["baselines"]["generation_speed_tps"]["eligible"])
        self.assertEqual(len(res_sparse["anomalies"]), 0)

        dense_rows = [
            {
                "canonical_id": f"req-norm-{i}",
                "request_timestamp": f"2026-10-01T1{i:02d}:00:00Z",
                "total_tokens": 1000,
                "timing_basis": "measured",
                "generation_duration_ms": 1000,
                "generation_speed_tps": 50.0,
                "ttft_ms": 300.0,
                "request_status": "success",
            }
            for i in range(10)
        ]
        dense_rows.append({
            "canonical_id": "req-spike-1",
            "session_id": "sess-spike",
            "request_timestamp": "2026-10-01T20:00:00Z",
            "total_tokens": 100000,
            "timing_basis": "measured",
            "generation_duration_ms": 5000,
            "generation_speed_tps": 5.0,
            "ttft_ms": 8000.0,
            "request_status": "success",
        })

        envelope = {
            "anomaly_settings": {
                "expected_anomalies": ["req-spike-1"]
            }
        }
        res_dense = detect_anomalies(None, dense_rows, envelope=envelope)
        self.assertTrue(res_dense["baselines"]["total_tokens"]["eligible"])
        self.assertEqual(res_dense["expected_anomalies_count"], 3)
        self.assertEqual(res_dense["unexpected_anomalies_count"], 0)

        anom = res_dense["anomalies"][0]
        self.assertEqual(anom["canonical_id"], "req-spike-1")
        self.assertTrue(anom["is_expected"])
        self.assertIn("fresh_input_tokens", anom["evidence"])


    def test_persisted_expected_anomaly_state_is_loaded(self):
        conn = open_database(":memory:")
        try:
            conn.execute(
                """
                INSERT INTO privacy_settings (
                    setting_key, setting_value, description, updated_at
                ) VALUES (
                    'efficiency_expected_anomalies',
                    '[{"id":"req-persisted-spike","notes":"Synthetic scheduled load"}]',
                    'Expected anomaly states',
                    '2026-10-26T12:00:00Z'
                )
                """
            )
            rows = [
                {
                    "canonical_id": f"req-persisted-{index}",
                    "request_timestamp": f"2026-10-20T{index:02d}:00:00Z",
                    "total_tokens": 1000,
                }
                for index in range(5)
            ]
            rows.append({
                "canonical_id": "req-persisted-spike",
                "request_timestamp": "2026-10-21T12:00:00Z",
                "total_tokens": 10000,
            })

            result = detect_anomalies(conn, rows)

            spike = next(
                anomaly for anomaly in result["anomalies"]
                if anomaly["canonical_id"] == "req-persisted-spike"
            )
            self.assertTrue(spike["is_expected"])
            self.assertEqual(
                spike["expected_state"]["notes"], "Synthetic scheduled load"
            )
        finally:
            conn.close()


class TestForecastMetricsDatabaseIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = open_database(":memory:")
        self.now_iso = "2026-10-26T12:00:00Z"

    def tearDown(self):
        self.conn.close()

    def test_smoke_database_forecast_anomalies_comparisons(self):
        """End-to-end database integration test using canonical schema."""
        cur = self.conn.cursor()

        for i in range(10, 18):
            d_str = f"2026-10-{i:02d}"
            req_id = f"req-smoke-{i}"
            sess_id = f"sess-smoke-{i}"

            model = "claude-3-5-sonnet" if i < 14 else "claude-3-5-haiku"
            tot_tokens = 5000 if i != 16 else 80000
            fresh = 3500 if i != 16 else 60000
            cw5 = 500 if i != 16 else 15000
            out_tok = 1000 if i != 16 else 5000

            cur.execute(
                """
                INSERT INTO sessions (session_id, origin_harness, created_at)
                VALUES (?, 'claude-code', ?)
                """,
                (sess_id, self.now_iso),
            )

            cur.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, request_timestamp, provider, account_alias,
                    origin_harness, access_route, raw_model, normalized_model,
                    session_id, request_status, timing_basis, duration_ms,
                    ttft_ms, generation_duration_ms, generation_speed_tps,
                    is_unresolved_overlap, created_at
                ) VALUES (
                    ?, ?, 'anthropic', 'default',
                    'claude-code', 'direct', ?, ?,
                    ?, 'success', 'measured', 2000.0,
                    250.0, 1500.0, 50.0,
                    0, ?
                )
                """,
                (req_id, f"{d_str}T10:00:00Z", model, model, sess_id, self.now_iso),
            )

            cur.execute(
                """
                INSERT INTO normalized_usage (
                    canonical_id, fresh_input_tokens, cache_read_tokens,
                    cache_write_5m_tokens, cache_write_1h_tokens,
                    cache_write_unknown_ttl_tokens, output_tokens,
                    reasoning_tokens
                ) VALUES (?, ?, 0, ?, 0, 0, ?, 0)
                """,
                (req_id, fresh, cw5, out_tok),
            )

            cur.execute(
                """
                INSERT INTO financial_facts (
                    canonical_id, pricing_status, api_equivalent_usd,
                    api_equivalent_inr, actual_billed_amount, cache_savings_usd,
                    calculated_at
                ) VALUES (?, 'priced', 0.05, 4.25, NULL, 0.0, ?)
                """,
                (req_id, self.now_iso),
            )

        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, request_timestamp, provider, origin_harness,
                access_route, raw_model, normalized_model, request_status,
                created_at
            ) VALUES (
                'req-smoke-unfiltered', '2026-10-17T11:00:00Z', 'openai',
                'omp', 'direct', 'unfiltered-model', 'unfiltered-model',
                'success', ?
            )
            """,
            (self.now_iso,),
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, output_tokens
            ) VALUES ('req-smoke-unfiltered', 900000, 0)
            """
        )

        cur.execute(
            """
            INSERT INTO manual_settings_and_outcomes (
                outcome_id, entity_type, entity_id, task_label, notes,
                created_at, updated_at
            ) VALUES ('out-exp-1', 'anomaly', 'req-smoke-16', 'expected_load_test', 'Scheduled stress test', ?, ?)
            """,
            (self.now_iso, self.now_iso),
        )

        self.conn.commit()

        fc_envelope = {
            "contract_version": "1",
            "family": "forecasts",
            "harness_scope": "combined",
            "filters": {
                "provider": ["anthropic"],
                "application": ["claude-code"],
            },
            "time_range": {
                "start": "2026-10-16T00:00:00Z",
                "end": "2026-10-17T00:00:00Z",
                "timezone": "UTC",
            },
            "history_window": {
                "start": "2026-10-10T00:00:00Z",
                "end": "2026-10-18T00:00:00Z",
                "timezone": "UTC",
            },
        }
        res_fc = analyze(self.conn, fc_envelope)
        self.assertEqual(res_fc["contract_version"], "1")
        self.assertEqual(res_fc["family"], "forecasts")
        self.assertTrue(res_fc["eligible"])
        self.assertIn("remainder_of_day", res_fc["values"])
        self.assertIn("remainder_of_week", res_fc["values"])
        self.assertIn("remainder_of_month", res_fc["values"])
        self.assertEqual(res_fc["coverage"]["activity_days_observed"], 8)
        self.assertEqual(res_fc["partial_day"]["observed_tokens"], 5000)
        self.assertEqual(
            res_fc["query"]["time_range"]["start"], "2026-10-16T00:00:00Z"
        )
        self.assertEqual(
            res_fc["history_window"]["start"], "2026-10-10T00:00:00Z"
        )
        self.assertEqual(
            res_fc["history_window"]["end"], "2026-10-18T00:00:00Z"
        )
        self.assertEqual(
            res_fc["query"]["filters"],
            {"provider": ["anthropic"], "application": ["claude-code"]},
        )

        anom_envelope = {
            "contract_version": "1",
            "family": "anomalies",
            "harness_scope": "combined",
            "filters": {},
        }
        res_anom = analyze(self.conn, anom_envelope)
        self.assertEqual(res_anom["family"], "anomalies")
        self.assertGreater(res_anom["values"]["total_anomalies"], 0)
        found_expected = any(
            a["canonical_id"] == "req-smoke-16" and a["is_expected"] for a in res_anom["rows"]
        )
        self.assertTrue(found_expected)

        comp_envelope = {
            "contract_version": "1",
            "family": "comparisons",
            "harness_scope": "combined",
            "filters": {},
            "comparison_periods": {
                "period_a": {
                    "label": "Baseline Sonnet",
                    "start": "2026-10-10T00:00:00Z",
                    "end": "2026-10-14T00:00:00Z",
                    "timezone": "UTC",
                },
                "period_b": {
                    "label": "Test Haiku",
                    "start": "2026-10-14T00:00:00Z",
                    "end": "2026-10-18T00:00:00Z",
                    "timezone": "UTC",
                },
            },
        }
        res_comp = analyze(self.conn, comp_envelope)
        self.assertEqual(res_comp["family"], "comparisons")
        self.assertTrue(res_comp["period_a"]["duration_days"] == 4.0)
        self.assertTrue(res_comp["period_b"]["duration_days"] == 4.0)
        self.assertIn("Model mix shift detected", " ".join(res_comp["confounders"]))
        self.assertEqual(res_comp["matched_status"], "non_matched_or_confounded")
        self.assertIn("Descriptive observational comparison only", res_comp["causal_claim_disclaimer"])
    def test_comparison_preserves_global_filters_for_both_periods(self):
        cur = self.conn.cursor()
        requests = [
            ("match-a", "2026-10-10T12:00:00Z", "anthropic", "claude-code", 100),
            ("wrong-provider-a", "2026-10-10T13:00:00Z", "openai", "claude-code", 900),
            ("match-b", "2026-10-11T12:00:00Z", "anthropic", "claude-code", 200),
            ("wrong-app-b", "2026-10-11T13:00:00Z", "anthropic", "omp", 800),
        ]
        for request_id, timestamp, provider, harness, tokens in requests:
            cur.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, request_timestamp, provider, origin_harness,
                    access_route, raw_model, normalized_model, request_status,
                    created_at
                ) VALUES (?, ?, ?, ?, 'direct', 'model-x', 'model-x', 'success', ?)
                """,
                (request_id, timestamp, provider, harness, self.now_iso),
            )
            cur.execute(
                """
                INSERT INTO normalized_usage (
                    canonical_id, fresh_input_tokens, output_tokens
                ) VALUES (?, ?, 0)
                """,
                (request_id, tokens),
            )

        result = analyze(self.conn, {
            "contract_version": "1",
            "family": "comparisons",
            "harness_scope": "combined",
            "filters": {
                "provider": ["anthropic"],
                "application": ["claude-code"],
            },
            "time_range": {"timezone": "UTC"},
            "comparison_periods": {
                "period_a": {
                    "start": "2026-10-10",
                    "end": "2026-10-11",
                    "timezone": "UTC",
                },
                "period_b": {
                    "start": "2026-10-11",
                    "end": "2026-10-12",
                    "timezone": "UTC",
                },
            },
        })

        self.assertEqual(result["period_a"]["metrics"]["requests_count"], 1)
        self.assertEqual(result["period_a"]["metrics"]["total_tokens"], 100)
        self.assertEqual(result["period_b"]["metrics"]["requests_count"], 1)
        self.assertEqual(result["period_b"]["metrics"]["total_tokens"], 200)

    def test_comparison_period_duration_uses_utc_dst_midnights(self):
        result = analyze(self.conn, {
            "contract_version": "1",
            "family": "comparisons",
            "harness_scope": "combined",
            "filters": {},
            "time_range": {"timezone": "America/New_York"},
            "comparison_periods": {
                "period_a": {
                    "start": "2026-03-08",
                    "end": "2026-03-09",
                    "timezone": "America/New_York",
                },
                "period_b": {
                    "start": "2026-11-01",
                    "end": "2026-11-02",
                    "timezone": "America/New_York",
                },
            },
        })

        self.assertEqual(result["period_a"]["duration_seconds"], 23 * 60 * 60)
        self.assertEqual(result["period_b"]["duration_seconds"], 25 * 60 * 60)
        self.assertTrue(result["confounders"])



if __name__ == "__main__":
    unittest.main()
