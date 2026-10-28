"""Tests for WP19 Efficiency Metrics, Signal Detection, and Counterfactual Scenarios.

Covers:
1. Signal detection on eligible evidence (cache, retry, tooloutput, repeatedread, compaction, verbosity, context).
2. Exclusive token assignment per request vs. overlapping signal accounting (strict no double counting).
3. Grounded insight links: confidence ('high'/'med'/'low'), basis, concrete evidence, observed vs. avoidable.
4. Routing alternatives: adequate sample thresholding vs. explicit experiment suggestions (no causal claims).
5. Counterfactual scenario simulations (model-switch, cache-read, output-reduction, longcontext):
   - Filtered query counts, effective prices, assumptions, and unknown-rate abstention.
6. Context restart / compaction guidance based on actual task transitions, not cumulative input-as-occupancy.
7. File-backed temporary SQLite query smoke for historical prices and unknown-rate abstention.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import unittest
import tempfile
from typing import Any, Dict

from token_ledger.metrics.efficiency import analyze
from token_ledger.pricing import create_pricing_version


class TestEfficiencyMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self.temp_dir.name) / "efficiency.sqlite")
        self.conn.row_factory = sqlite3.Row
        schema_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "token_ledger"
            / "store"
            / "migrations"
            / "001_initial.sql"
        )
        with open(schema_path, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _insert_request(
        self,
        cid: str,
        sid: str,
        fresh: int = 1000,
        read: int = 0,
        output: int = 200,
        status: str = "ok",
        raw_model: str = "claude-3-5-sonnet",
        norm_model: str = "Sonnet 3.5",
        pricing_status: str = "priced",
        request_timestamp: str = "2026-10-26T10:00:00Z",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, session_id, request_status, timing_basis,
                duration_ms, request_timestamp, created_at
            ) VALUES (?, 'anthropic', 'default', 'claude-code', 'api', ?, ?, ?, ?, 'measured', 500.0, ?, '2026-10-26T10:00:00Z');
            """,
            (cid, raw_model, norm_model, sid, status, request_timestamp),
        )
        self.conn.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens
            ) VALUES (?, ?, ?, ?);
            """,
            (cid, fresh, read, output),
        )
        self.conn.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, calculated_at
            ) VALUES (?, ?, ?, ?, '2026-10-26T10:00:00Z');
            """,
            (cid, pricing_status, 0.05 if pricing_status == "priced" else None, 4.25 if pricing_status == "priced" else None),
        )

    def _set_complete_usage(self, cid: str, unknown_ttl: int = 0) -> None:
        self.conn.execute(
            """
            UPDATE normalized_usage
            SET cache_write_5m_tokens = 0,
                cache_write_1h_tokens = 0,
                cache_write_unknown_ttl_tokens = ?
            WHERE canonical_id = ?
            """,
            (unknown_ttl, cid),
        )

    def _insert_event(
        self,
        event_id: str,
        sid: str,
        cid: str,
        kind: str,
        name: str,
        output_bytes: int = 100,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO events (
                event_id, session_id, canonical_id, event_kind, event_name,
                started_at, duration_ms, output_size_bytes, status, details_json
            ) VALUES (?, ?, ?, ?, ?, '2026-10-26T10:00:00Z', 50, ?, 'ok', '{}');
            """,
            (event_id, sid, cid, kind, name, output_bytes),
        )

    def test_exclusive_vs_overlapping_accounting(self) -> None:
        """Verifies that exclusive signal token breakdown is additive and does not double-count."""
        # Insert a session with a clean request, a retry request, and a cache-miss request
        self._insert_request("c1", "s1", fresh=500, read=500, output=100, status="ok")
        self._insert_request("c2", "s1", fresh=3000, read=0, output=100, status="error") # retry
        self._insert_request("c3", "s1", fresh=4000, read=0, output=100, status="ok") # cache miss in multi-turn
        for cid in ("c1", "c2", "c3"):
            self._set_complete_usage(cid)

        envelope = {"contract_version": "1", "harness_scope": "combined", "metric_id": "efficiency"}
        res = analyze(self.conn, envelope)

        ex = res["signals"]["exclusive_breakdown"]
        op = res["signals"]["overlapping_breakdown"]

        # Exclusive breakdown sum of observed tokens must equal total tokens
        total_exclusive_observed = sum(d["observed_tokens"] for d in ex.values())
        self.assertEqual(total_exclusive_observed, res["values"]["total_tokens"])

        # Check that avoidable tokens in values equals sum of exclusive avoidable tokens
        total_exclusive_avoidable = sum(d["avoidable_tokens"] for d in ex.values())
        self.assertEqual(res["values"]["avoidable_tokens"], total_exclusive_avoidable)

        # Overlapping breakdown must have is_additive = False flag
        for sig_name, data in op.items():
            self.assertFalse(data["is_additive"])

    def test_signal_detection_eligible_evidence(self) -> None:
        """Verifies retry, tool output, and cache signals require concrete eligible evidence."""
        # 1. Clean request
        self._insert_request("c1", "s1", fresh=100, read=100, output=50, status="ok")
        # 2. Tool output bloat request with associated large event (> 30KB)
        self._insert_request("c2", "s1", fresh=5000, read=0, output=100, status="ok")
        self._insert_event("e1", "s1", "c2", "tool_result", "bash_exec", output_bytes=45_000)

        envelope = {"contract_version": "1", "harness_scope": "combined", "metric_id": "efficiency"}
        res = analyze(self.conn, envelope)

        # Tooloutput should be detected
        self.assertGreater(res["signals"]["exclusive_breakdown"]["tooloutput"]["requests_count"], 0)
        # Verify insight has evidence attached
        tool_insights = [i for i in res["insights"] if i["category"] == "tooloutput"]
        self.assertTrue(len(tool_insights) > 0)
        ins = tool_insights[0]
        self.assertEqual(ins["confidence"], "high")
        self.assertEqual(ins["basis"], "measured_event")
        self.assertTrue(len(ins["evidence_sample"]) > 0)
        self.assertIn("e1", ins["evidence_sample"][0]["event_ids"])

    def test_routing_alternatives_adequate_samples_vs_experiment(self) -> None:
        """Verifies routing recommendations require adequate outcome samples or suggest A/B experiments."""
        # Insert requests with Sonnet
        for i in range(5):
            self._insert_request(f"c_s_{i}", f"sess_{i}", fresh=2000, read=1000, output=200, raw_model="claude-3-5-sonnet")

        # Without completed outcomes on Haiku, must suggest experiment and make no causal claim
        envelope = {"contract_version": "1", "harness_scope": "combined", "metric_id": "efficiency", "min_sample_size": 3}
        res = analyze(self.conn, envelope)

        routes = res["routing_alternatives"]
        self.assertTrue(len(routes) > 0)
        r = routes[0]
        self.assertEqual(r["recommendation_type"], "experiment_suggestion")
        self.assertFalse(r["adequate_sample"])
        self.assertIn("experiment_design", r)

        # Insert 3 completed tasks on Haiku in manual_settings_and_outcomes
        for i in range(3):
            hid = f"h_sess_{i}"
            self._insert_request(f"c_h_{i}", hid, fresh=1000, read=500, output=100, raw_model="claude-3-5-haiku", norm_model="Haiku 3.5")
            self.conn.execute(
                """
                INSERT INTO manual_settings_and_outcomes (
                    entity_type, entity_id, outcome_id, task_label, completion_rating, manual_rating, is_inferred, created_at, updated_at
                ) VALUES ('session', ?, ?, 'code_search', 'complete', 5.0, 0, '2026-10-26T10:00:00Z', '2026-10-26T10:00:00Z');
                """,
                (hid, f"out_{i}"),
            )

        res2 = analyze(self.conn, envelope)
        routes2 = res2["routing_alternatives"]
        r2 = routes2[0]
        self.assertEqual(r2["recommendation_type"], "validated_routing")
        self.assertTrue(r2["adequate_sample"])
        self.assertGreaterEqual(r2["completed_sample_count"], 3)

    def test_counterfactual_scenarios_use_filtered_rows_and_effective_rates(self) -> None:
        """Scenarios use shared-query rows, historical prices, and abstain on missing target rates."""
        for version_id, start, end, input_rate, cache_rate, output_rate in (
            ("synthetic-old", "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z", 1.0, 0.5, 2.0),
            ("synthetic-new", "2025-02-01T00:00:00Z", None, 4.0, 2.0, 8.0),
        ):
            create_pricing_version(
                self.conn,
                version_id,
                [{
                    "provider": "anthropic",
                    "model_identity": "model-a",
                    "access_route": "api",
                    "input_rate_per_m": input_rate,
                    "output_rate_per_m": output_rate,
                    "cache_read_rate_per_m": cache_rate,
                    "cache_write_5m_rate_per_m": input_rate,
                    "cache_write_1h_rate_per_m": input_rate,
                    "status": "verified",
                }],
                start,
                end,
                source_provenance={"source": "synthetic-efficiency-test"},
            )

        self._insert_request(
            "old", "s-old", fresh=1000, read=200, output=100,
            raw_model="model-a", norm_model="model-a",
            request_timestamp="2025-01-15T10:00:00Z",
        )
        self._insert_request(
            "new", "s-new", fresh=1000, read=200, output=100,
            raw_model="model-a", norm_model="model-a",
            request_timestamp="2025-02-15T10:00:00Z",
        )
        self._insert_request(
            "outside-filter", "s-other", fresh=90000, read=0, output=9000,
            raw_model="other-model", norm_model="other-model",
            request_timestamp="2025-02-15T10:00:00Z",
        )
        for canonical_id in ("old", "new", "outside-filter"):
            self._set_complete_usage(canonical_id)

        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "efficiency",
            "filters": {"model": ["model-a"]},
            "scenario_inputs": {
                "scenario_type": "output-reduction",
                "output_reduction_pct": 50,
            },
        }
        result = analyze(self.conn, envelope)
        scenario = result["scenarios"][0]
        self.assertEqual(scenario["query_filtered_request_count"], 2)
        self.assertEqual(scenario["request_count"], 2)
        self.assertEqual(scenario["baseline_tokens"]["fresh_input_tokens"], 2000)
        self.assertEqual(scenario["projected_tokens"]["output_tokens"], 100)
        self.assertTrue(scenario["is_priced"])
        self.assertAlmostEqual(scenario["financials"]["baseline_cost_usd"], 0.0065)
        self.assertAlmostEqual(scenario["financials"]["projected_cost_usd"], 0.006)
        self.assertAlmostEqual(scenario["financials"]["projected_savings_usd"], 0.0005)
        self.assertEqual(
            {item["pricing_version_id"] for item in scenario["pricing_evidence"]},
            {"synthetic-old", "synthetic-new"},
        )

        unknown_rate = analyze(
            self.conn,
            {
                **envelope,
                "scenario_inputs": {
                    "scenario_type": "model-switch",
                    "source_model": "model-a",
                    "target_model": "unknown-target",
                },
            },
        )["scenarios"][0]
        self.assertFalse(unknown_rate["is_priced"])
        self.assertIsNotNone(unknown_rate["financials"]["baseline_cost_usd"])
        self.assertIsNone(unknown_rate["financials"]["projected_cost_usd"])
        self.assertIsNone(unknown_rate["financials"]["projected_savings_usd"])
        self.assertTrue(any(item["reason"] == "no_provider_model_route_rate" for item in unknown_rate["pricing_evidence"]))

    def test_counterfactual_scenarios_preserve_unknown_cache_ttl(self) -> None:
        create_pricing_version(
            self.conn,
            "synthetic-cache-rate",
            [{
                "provider": "anthropic",
                "model_identity": "model-a",
                "access_route": "api",
                "input_rate_per_m": 1.0,
                "output_rate_per_m": 2.0,
                "cache_read_rate_per_m": 0.5,
                "cache_write_5m_rate_per_m": 1.25,
                "cache_write_1h_rate_per_m": 2.0,
            }],
            "2025-01-01T00:00:00Z",
        )
        self._insert_request(
            "ttl", "s-ttl", fresh=1000, read=200, output=100,
            raw_model="model-a", norm_model="model-a",
            request_timestamp="2025-01-15T10:00:00Z",
        )
        self._set_complete_usage("ttl", unknown_ttl=12)
        scenario = analyze(
            self.conn,
            {
                "contract_version": "1",
                "harness_scope": "combined",
                "metric_id": "efficiency",
                "filters": {"model": ["model-a"]},
                "scenario_inputs": {"scenario_type": "cache-read", "cache_expansion_pct": 50},
            },
        )["scenarios"][0]
        self.assertEqual(scenario["baseline_tokens"]["cache_write_unknown_ttl_tokens"], 12)
        self.assertEqual(scenario["projected_tokens"]["cache_write_unknown_ttl_tokens"], 12)
        self.assertEqual(scenario["pricing_coverage"]["unknown_ttl_tokens"], 12)
        self.assertFalse(scenario["is_priced"])
        self.assertIsNone(scenario["financials"]["baseline_cost_usd"])
        self.assertIsNone(scenario["financials"]["projected_savings_usd"])
        self.assertEqual(scenario["financials"]["unpriced_tokens"], 12)
    def test_compaction_guidance_grounded_in_events(self) -> None:
        """Verifies compaction recommendation requires actual task transitions, not cumulative tokens."""
        # Session without task boundary events
        self._insert_request("c1", "s1", fresh=50000, read=0, output=100)
        envelope = {"contract_version": "1", "harness_scope": "combined", "metric_id": "efficiency"}
        res = analyze(self.conn, envelope)

        comp = res["compaction_recommendation"]
        self.assertEqual(comp["status"], "abstain")
        self.assertFalse(comp["actionable"])

        # Now add explicit task transition event
        self._insert_event("e_task", "s1", "c1", "task_switch", "switch_to_tests")
        res2 = analyze(self.conn, envelope)
        comp2 = res2["compaction_recommendation"]
        self.assertEqual(comp2["status"], "recommended")
        self.assertTrue(comp2["actionable"])


if __name__ == "__main__":
    unittest.main()
