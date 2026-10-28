"""Targeted unit and integration tests for WP15 Trace Attribution & Context Analysis.

Tests cover Rebuild Plan Rows 10, 11, 12, 14, 29, 32, 33, 34, 40, 41:
- Exclusive ownership counts vs inclusive rollups (no double-counting).
- User prompt grouping and unlinked missingness tracking.
- Context window occupancy vs distinct cumulative input proxy.
- Compaction timeline with nullable before/after semantics.
- Tool usage metadata preservation (never bodies).
- Suspected retry and loop detection with evidence links.
- Repetition analysis and privacy opt-in consent gating.
- Actual SQLite temporary DB synthetic nested trace smoke tests.
"""

import json
import sqlite3
import unittest

from token_ledger.metrics.traces import (
    analyze,
    analyze_repetition,
    build_session_tree,
    calculate_context_occupancy,
    detect_retry_loops,
    group_prompts_attribution,
    lookup_context_window,
)
from token_ledger.privacy import (
    OPTIN_CONTENT_FINGERPRINTS,
    save_privacy_setting,
)
from token_ledger.store import connect, migrate


class TestTraceAttributionMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        migrate(self.conn)
        self._seed_synthetic_data()

    def tearDown(self) -> None:
        self.conn.close()

    def _seed_synthetic_data(self) -> None:
        cur = self.conn.cursor()
        # 1. Projects
        cur.execute(
            """
            INSERT INTO projects (project_id, project_name, project_alias, created_at)
            VALUES ('proj-alpha', 'Alpha Project', 'alpha', '2026-10-20T10:00:00Z')
            """
        )

        # 2. Sessions (Parent sess-root, Child sess-sub1, Child sess-sub2)
        cur.execute(
            """
            INSERT INTO sessions (
                session_id, origin_harness, parent_session_id, project_id,
                started_at, duration_ms, created_at, metadata_json
            ) VALUES
                ('sess-root', 'claude-code', NULL, 'proj-alpha',
                 '2026-10-20T10:00:00Z', 60000, '2026-10-20T10:00:00Z',
                 '{"session_name": "Root Orchestrator"}'),
                ('sess-sub1', 'claude-code', 'sess-root', 'proj-alpha',
                 '2026-10-20T10:05:00Z', 20000, '2026-10-20T10:05:00Z', '{}'),
                ('sess-sub2', 'claude-code', 'sess-root', 'proj-alpha',
                 '2026-10-20T10:10:00Z', 15000, '2026-10-20T10:10:00Z', '{}')
            """
        )

        # 3. Prompts
        cur.execute(
            """
            INSERT INTO prompts (prompt_id, session_id, prompt_group, created_at, metadata_json)
            VALUES
                ('prompt-01', 'sess-root', 'refactor', '2026-10-20T10:00:01Z', '{"user": "alice"}'),
                ('prompt-02', 'sess-sub1', 'explore', '2026-10-20T10:05:01Z', '{"user": "alice"}')
            """
        )

        # 4. Canonical Requests
        # sess-root: req-1 (prompt-01), req-2 (prompt-01)
        # sess-sub1: req-3 (prompt-02)
        # sess-sub2: req-4 (unlinked prompt)
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id,
                origin_harness, access_route, raw_model, normalized_model,
                session_id, project_id, prompt_id, retry_attempt, request_status,
                request_timestamp, timing_basis, duration_ms, ttft_ms, generation_duration_ms,
                generation_speed_tps, created_at
            ) VALUES
                ('req-1', 'anthropic', 'default', 'pr-1', 'claude-code', 'direct',
                 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet',
                 'sess-root', 'proj-alpha', 'prompt-01', 0, 'ok',
                 '2026-10-20T10:00:05Z', 'measured', 1200, 200, 1000, 50.0, '2026-10-20T10:00:05Z'),
                ('req-2', 'anthropic', 'default', 'pr-2', 'claude-code', 'direct',
                 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet',
                 'sess-root', 'proj-alpha', 'prompt-01', 1, 'retry',
                 '2026-10-20T10:01:00Z', 'measured', 800, 150, 650, 40.0, '2026-10-20T10:01:00Z'),
                ('req-3', 'anthropic', 'default', 'pr-3', 'claude-code', 'direct',
                 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet',
                 'sess-sub1', 'proj-alpha', 'prompt-02', 0, 'ok',
                 '2026-10-20T10:05:10Z', 'measured', 2500, 300, 2200, 60.0, '2026-10-20T10:05:10Z'),
                ('req-4', 'anthropic', 'default', 'pr-4', 'claude-code', 'direct',
                 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet',
                 'sess-sub2', 'proj-alpha', NULL, 0, 'ok',
                 '2026-10-20T10:10:15Z', 'measured', 1500, 250, 1250, 55.0, '2026-10-20T10:10:15Z')
            """
        )

        # 5. Normalized Usage
        # req-1: 1000 in, 200 out (total 1200)
        # req-2: 1500 in, 300 out (total 1800)
        # req-3: 4000 in, 500 out (total 4500)
        # req-4: 2000 in, 400 out (total 2400)
        # Total tokens = 1200 + 1800 + 4500 + 2400 = 9900
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens,
                cache_write_5m_tokens, cache_write_1h_tokens, cache_write_unknown_ttl_tokens,
                output_tokens, reasoning_tokens
            ) VALUES
                ('req-1', 1000, 0, 0, 0, 0, 200, 50),
                ('req-2', 1500, 0, 0, 0, 0, 300, 80),
                ('req-3', 3000, 1000, 0, 0, 0, 500, 120),
                ('req-4', 1800, 200, 0, 0, 0, 400, 100)
            """
        )

        # 6. Financial Facts
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, actual_billed_amount, api_equivalent_usd,
                api_equivalent_inr, pricing_status, calculated_at
            ) VALUES
                ('req-1', 0.01, 0.012, 1.02, 'priced', '2026-10-20T10:00:05Z'),
                ('req-2', 0.015, 0.018, 1.53, 'priced', '2026-10-20T10:01:00Z'),
                ('req-3', 0.04, 0.045, 3.82, 'priced', '2026-10-20T10:05:10Z'),
                ('req-4', 0.02, 0.024, 2.04, 'priced', '2026-10-20T10:10:15Z')
            """
        )

        # 7. Trace Events (Tools, Compaction)
        cur.execute(
            """
            INSERT INTO events (
                event_id, session_id, canonical_id, parent_event_id,
                event_kind, event_name, started_at, ended_at,
                duration_ms, output_size_bytes, status, details_json
            ) VALUES
                ('ev-tool-1', 'sess-root', 'req-1', NULL, 'tool_use', 'read_file',
                 '2026-10-20T10:00:02Z', '2026-10-20T10:00:03Z', 500, 4096, 'ok',
                 '{"path": "/tmp/alpha/src/main.py", "tool_id": "call_1"}'),
                ('ev-tool-2', 'sess-root', 'req-2', NULL, 'tool_use', 'read_file',
                 '2026-10-20T10:00:55Z', '2026-10-20T10:00:56Z', 450, 4096, 'ok',
                 '{"path": "/tmp/alpha/src/main.py", "tool_id": "call_2"}'),
                ('ev-tool-3', 'sess-sub1', 'req-3', NULL, 'tool_use', 'bash',
                 '2026-10-20T10:05:05Z', '2026-10-20T10:05:07Z', 2000, 1024, 'error',
                 '{"command_type": "pytest", "tool_id": "call_3"}'),
                ('ev-compact-1', 'sess-sub1', 'req-3', NULL, 'compaction', 'auto_compact',
                 '2026-10-20T10:05:08Z', '2026-10-20T10:05:09Z', 300, NULL, 'ok',
                 '{"before_tokens": 150000, "after_tokens": 80000, "turns_compacted": 5}'),
                ('ev-compact-null', 'sess-sub2', 'req-4', NULL, 'compaction', 'manual_compact',
                 '2026-10-20T10:10:10Z', '2026-10-20T10:10:11Z', 200, NULL, 'ok',
                 '{"turns_compacted": 2}')
            """
        )
        self.conn.commit()

    def test_lookup_context_window(self) -> None:
        self.assertEqual(lookup_context_window("claude-3-5-sonnet-20241022"), 200000)
        self.assertEqual(lookup_context_window("gpt-4o"), 128000)
        self.assertEqual(lookup_context_window("gemini-1.5-pro"), 2000000)
        self.assertIsNone(lookup_context_window("custom-finetuned-llama"))

    def test_session_tree_uses_metadata_label_and_session_id_fallback(self) -> None:
        tree = build_session_tree(
            self.conn,
            [{
                "session_id": "sess-sub1",
                "total_tokens": 12,
                "fresh_input_tokens": 10,
                "output_tokens": 2,
                "reasoning_tokens": None,
                "api_equivalent_usd": None,
            }],
        )

        root = tree["root_nodes"][0]
        self.assertEqual(root["session_id"], "sess-root")
        self.assertEqual(root["session_name"], "Root Orchestrator")
        self.assertEqual(tree["all_nodes"]["sess-sub1"]["session_name"], "sess-sub1")

    def test_exclusive_vs_inclusive_session_attribution(self) -> None:
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "traces",
            "group_by": ["session"],
        }
        res = analyze(self.conn, envelope)

        self.assertEqual(res["contract_version"], "1")
        self.assertEqual(res["family"], "traces")

        # Global totals must be strictly non-overlapping exclusive sum:
        # req-1: 1200, req-2: 1800, req-3: 4500, req-4: 2400 => total 9900
        self.assertEqual(res["values"]["total_tokens"], 9900)
        self.assertEqual(res["values"]["total_requests"], 4)
        self.assertEqual(res["values"]["total_sessions"], 3)
        self.assertEqual(res["values"]["root_sessions_count"], 1)
        self.assertEqual(res["values"]["subagent_sessions_count"], 2)

        # Check session nodes
        tree = res["session_tree"]
        self.assertEqual(len(tree), 1)  # 1 root node: sess-root
        root = tree[0]
        self.assertEqual(root["session_id"], "sess-root")
        self.assertEqual(root["exclusive_requests_count"], 2)
        self.assertEqual(root["exclusive_tokens"], 3000)  # 1200 + 1800

        # Inclusive rollup includes children sess-sub1 (4500) and sess-sub2 (2400)
        # 3000 + 4500 + 2400 = 9900
        self.assertEqual(root["inclusive_tokens"], 9900)
        self.assertEqual(root["inclusive_requests_count"], 4)

        # Ensure exclusive counts never equal inclusive counts when children exist
        self.assertNotEqual(root["exclusive_tokens"], root["inclusive_tokens"])

    def test_prompt_grouping_no_double_count(self) -> None:
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "traces",
            "group_by": ["prompt"],
        }
        res = analyze(self.conn, envelope)

        prompts = res["prompt_groups"]
        self.assertEqual(len(prompts), 2)  # prompt-01 and prompt-02

        p1 = next(p for p in prompts if p["prompt_id"] == "prompt-01")
        self.assertEqual(p1["requests_count"], 2)
        self.assertEqual(p1["total_tokens"], 3000)  # 1200 + 1800

        p2 = next(p for p in prompts if p["prompt_id"] == "prompt-02")
        self.assertEqual(p2["requests_count"], 1)
        self.assertEqual(p2["total_tokens"], 4500)

        # req-4 had NULL prompt_id => request and token missingness remain explicit.
        self.assertEqual(res["missingness"]["unlinked_prompt_requests_count"], 1)
        self.assertEqual(res["missingness"]["unlinked_prompt_tokens"], 2400)

    def test_context_occupancy_vs_cumulative_input_proxy(self) -> None:
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "traces",
        }
        # 1. Without explicit context event: occupancy is unavailable, proxy is tracked separately
        res = analyze(self.conn, envelope)
        ctx = res["context_occupancy"]
        self.assertIsNone(ctx["window_size"])
        self.assertIsNone(ctx["peak_occupancy_tokens"])
        self.assertEqual(ctx["cumulative_input_tokens"], 8500)
        self.assertEqual(res["values"]["cumulative_input_tokens"], 8500)

        turns = ctx["per_turn_history"]
        self.assertEqual(len(turns), 4)
        # Proxy stays in input_volume_proxy_tokens; reported_context_tokens is None
        self.assertEqual(turns[0]["input_volume_proxy_tokens"], 1000)
        self.assertIsNone(turns[0]["reported_context_tokens"])
        self.assertEqual(turns[0]["occupancy_basis"], "unavailable")
        self.assertEqual(turns[1]["input_volume_proxy_tokens"], 1500)
        self.assertIsNone(turns[1]["reported_context_tokens"])

        # 2. With explicit context event: reported_context_tokens matches explicit measurement
        self.conn.execute(
            """
            INSERT INTO events (
                event_id, session_id, canonical_id, parent_event_id,
                event_kind, event_name, started_at, ended_at,
                duration_ms, output_size_bytes, status, details_json
            ) VALUES (
                'ev-ctx-1', 'sess-root', 'req-1', NULL,
                'context', 'context_measurement',
                '2026-10-20T10:00:02Z', '2026-10-20T10:00:03Z', 50, NULL, 'ok',
                '{"reported_context_tokens": 12000, "context_window_size": 200000}'
            )
            """
        )
        self.conn.commit()

        res_reported = analyze(self.conn, envelope)
        ctx_reported = res_reported["context_occupancy"]
        self.assertEqual(ctx_reported["window_size"], 200000)
        self.assertEqual(ctx_reported["peak_occupancy_tokens"], 12000)
        self.assertEqual(ctx_reported["peak_occupancy_ratio"], 0.06)

        turns_rep = ctx_reported["per_turn_history"]
        # req-1 has explicit context measurement
        self.assertEqual(turns_rep[0]["reported_context_tokens"], 12000)
        self.assertEqual(turns_rep[0]["context_window_size"], 200000)
        self.assertEqual(turns_rep[0]["occupancy_basis"], "explicit_source_context_event")
        self.assertEqual(turns_rep[0]["input_volume_proxy_tokens"], 1000)
        # req-2 still has no explicit context event -> reported is None, proxy is preserved
        self.assertIsNone(turns_rep[1]["reported_context_tokens"])
        self.assertEqual(turns_rep[1]["input_volume_proxy_tokens"], 1500)
    def test_compaction_nullable_semantics(self) -> None:
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "traces",
        }
        res = analyze(self.conn, envelope)

        comp = res["compaction"]
        self.assertEqual(comp["status"], "available")
        self.assertEqual(comp["compaction_events_count"], 2)

        ev1 = next(e for e in comp["events"] if e["event_id"] == "ev-compact-1")
        self.assertEqual(ev1["before_tokens"], 150000)
        self.assertEqual(ev1["after_tokens"], 80000)
        self.assertEqual(ev1["reduction_tokens"], 70000)
        self.assertAlmostEqual(ev1["reduction_ratio"], 0.4667, places=4)
        self.assertEqual(comp["total_reduction_tokens"], 70000)

        ev2 = next(e for e in comp["events"] if e["event_id"] == "ev-compact-null")
        # Semantics check: NULL before/after tokens MUST remain None, never 0!
        self.assertIsNone(ev2["before_tokens"])
        self.assertIsNone(ev2["after_tokens"])
        self.assertIsNone(ev2["reduction_tokens"])
        self.assertIsNone(ev2["reduction_ratio"])

    def test_tool_usage_metadata_never_bodies(self) -> None:
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "traces",
            "group_by": ["tool"],
        }
        res = analyze(self.conn, envelope)

        rows = res["rows"]
        read_file_tool = next(r for r in rows if r["tool_name"] == "read_file")
        self.assertEqual(read_file_tool["invocations_count"], 2)
        self.assertEqual(read_file_tool["total_duration_ms"], 950)  # 500 + 450
        self.assertEqual(read_file_tool["total_output_size_bytes"], 8192)  # 4096 + 4096
        self.assertEqual(read_file_tool["success_count"], 2)
        self.assertEqual(read_file_tool["error_count"], 0)

        # Ensure no tool payload or body is in rows or values.
        dumped = json.dumps(res)
        self.assertNotIn("command_body", dumped)
        self.assertNotIn("pytest -v", dumped)
        read_file_stats = next(t for t in res["tools_breakdown"] if t["tool_name"] == "read_file")
        self.assertEqual(read_file_stats["timed_invocations_count"], 2)
        self.assertEqual(read_file_stats["output_size_observed_count"], 2)

    def test_linked_tool_result_contributes_status_and_output_size(self) -> None:
        # Entire fixture is synthetic; this marker is not a real file body.
        self.conn.execute(
            """
            INSERT INTO events (
                event_id, session_id, canonical_id, parent_event_id,
                event_kind, event_name, started_at, ended_at,
                duration_ms, output_size_bytes, status, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ev-result-synthetic", "sess-root", "req-1", "ev-tool-1",
                "tool_result", "read_file_result", "2026-10-20T10:00:03Z",
                "2026-10-20T10:00:03Z", None, 2048, "error",
                json.dumps({"content": "synthetic marker; never source file data"}),
            ),
        )
        res = analyze(self.conn, {"contract_version": "1", "harness_scope": "combined"})
        linked = next(event for event in res["events"] if event["event_id"] == "ev-result-synthetic")
        self.assertEqual(linked["parent_event_id"], "ev-tool-1")
        self.assertNotIn("content", linked["details"])

        read_file = next(tool for tool in res["tools_breakdown"] if tool["tool_name"] == "read_file")
        self.assertEqual(read_file["total_output_size_bytes"], 6144)
        self.assertEqual(read_file["output_size_observed_count"], 2)
        self.assertEqual(read_file["success_count"], 1)
        self.assertEqual(read_file["error_count"], 1)
        self.assertNotIn("synthetic marker", json.dumps(res))

    def test_trace_response_exposes_linkage_and_aggregate_evidence(self) -> None:
        res = analyze(self.conn, {"contract_version": "1", "harness_scope": "combined"})
        events_by_id = {event["event_id"]: event for event in res["events"]}
        self.assertIn("parent_event_id", events_by_id["ev-tool-1"])
        self.assertEqual(res["event_counts_by_kind"], {"tool_use": 3, "compaction": 2})
        self.assertEqual(res["missingness"]["unresolved_parent_events_count"], 0)

        root = next(node for node in res["session_nodes"] if node["session_id"] == "sess-root")
        self.assertEqual(root["exclusive_tokens"], 3000)
        self.assertEqual(root["inclusive_tokens"], 9900)
        self.assertEqual(res["compaction"]["total_reduction_tokens"], 70000)
        self.assertEqual(res["context_occupancy"]["per_turn_history"][1]["input_proxy_growth_tokens"], 1500)
        self.assertEqual(res["repetition_signals"]["signals"][0]["evidence_state"], "unknown")

    def test_retry_loop_detection_suspected_signals(self) -> None:
        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "traces",
        }
        res = analyze(self.conn, envelope)

        retries = res["retry_loops"]
        self.assertGreater(retries["retry_requests_count"], 0)
        self.assertEqual(retries["retry_tokens"], 1800)  # req-2 had retry_attempt=1 and tokens=1800

        signals = retries["suspected_waste_signals"]
        self.assertTrue(any(s["classification"] == "suspected_signal" for s in signals))
        self.assertTrue(all("proven waste" not in s["classification"] for s in signals))

    def test_repetition_privacy_gate(self) -> None:
        # A repeated path alias alone is not evidence that the source contents were unchanged.
        res_no_opt = analyze(self.conn, {"contract_version": "1", "harness_scope": "combined"})
        rep_no_opt = res_no_opt["repetition_signals"]
        self.assertFalse(rep_no_opt["opt_in_content_fingerprints"])
        self.assertGreater(rep_no_opt["repeated_reads_count"], 0)
        signal = rep_no_opt["signals"][0]
        self.assertIsNone(signal["content_fingerprint"])
        self.assertEqual(signal["content_fingerprints"], [])
        self.assertEqual(signal["evidence_state"], "unknown")
        self.assertEqual(signal["evidence_basis"], "unknown")
        self.assertIn("suspected", signal["classification"])
        self.assertNotIn("content_fingerprint", res_no_opt["events"][0]["details"])

        save_privacy_setting(self.conn, OPTIN_CONTENT_FINGERPRINTS, True)
        res_opt = analyze(self.conn, {"contract_version": "1", "harness_scope": "combined"})
        rep_opt = res_opt["repetition_signals"]
        self.assertTrue(rep_opt["opt_in_content_fingerprints"])
        sig_opt = rep_opt["signals"][0]
        self.assertIsNone(sig_opt["content_fingerprint"])
        self.assertEqual(sig_opt["evidence_state"], "unknown")
        self.assertNotIn("content_fingerprint", res_opt["events"][0]["details"])

    def test_repetition_requires_explicit_source_evidence(self) -> None:
        digest_a = "a" * 64
        digest_b = "b" * 64
        synthetic_reads = [
            {
                "event_id": "synthetic-read-1",
                "event_kind": "tool_use",
                "event_name": "read_file",
                "details": {"path": "loc_0123456789abcdef"},
            },
            {
                "event_id": "synthetic-read-2",
                "event_kind": "tool_use",
                "event_name": "read_file",
                "details": {"path": "loc_0123456789abcdef"},
            },
        ]
        unknown_without_consent = analyze_repetition(self.conn, synthetic_reads, opt_in_fingerprints=False)
        self.assertEqual(unknown_without_consent["signals"][0]["evidence_state"], "unknown")
        self.assertEqual(unknown_without_consent["signals"][0]["content_fingerprints"], [])

        digest_reads = [
            {**synthetic_reads[0], "details": {**synthetic_reads[0]["details"], "source_content_digest": digest_a}},
            {**synthetic_reads[1], "details": {**synthetic_reads[1]["details"], "source_content_digest": digest_b}},
        ]
        unconsented_digest = analyze_repetition(
            self.conn, digest_reads, opt_in_fingerprints=False
        )["signals"][0]
        self.assertEqual(unconsented_digest["evidence_state"], "unknown")
        self.assertEqual(unconsented_digest["evidence_basis"], "unknown")
        self.assertIsNone(unconsented_digest["content_fingerprint"])
        self.assertEqual(unconsented_digest["content_fingerprints"], [])

        content_change = analyze_repetition(self.conn, digest_reads, opt_in_fingerprints=True)["signals"][0]
        self.assertEqual(content_change["evidence_state"], "changed")
        self.assertEqual(content_change["evidence_basis"], "source_content_digest")
        self.assertIsNone(content_change["content_fingerprint"])
        self.assertEqual(content_change["content_fingerprints"], [digest_a, digest_b])

        same_digest_reads = [
            {**synthetic_reads[0], "details": {**synthetic_reads[0]["details"], "content_digest": digest_a}},
            {**synthetic_reads[1], "details": {**synthetic_reads[1]["details"], "content_digest": digest_a}},
        ]
        unchanged = analyze_repetition(self.conn, same_digest_reads, opt_in_fingerprints=True)["signals"][0]
        self.assertEqual(unchanged["evidence_state"], "unchanged")
        self.assertEqual(unchanged["content_fingerprint"], digest_a)

        change_reads = [
            {**synthetic_reads[0], "details": {**synthetic_reads[0]["details"], "source_change_state": "changed"}},
            {**synthetic_reads[1], "details": {**synthetic_reads[1]["details"], "source_change_state": "unchanged"}},
        ]
        explicit_change = analyze_repetition(self.conn, change_reads, opt_in_fingerprints=False)["signals"][0]
        self.assertEqual(explicit_change["evidence_state"], "changed")
        self.assertEqual(explicit_change["evidence_basis"], "source_change_evidence")
        self.assertIsNone(explicit_change["content_fingerprint"])
        self.assertEqual(explicit_change["content_fingerprints"], [])

        unchanged_reads = [
            {**synthetic_reads[0], "details": {**synthetic_reads[0]["details"], "content_changed": False}},
            {**synthetic_reads[1], "details": {**synthetic_reads[1]["details"], "changed_since_previous_read": False}},
        ]
        explicit_unchanged = analyze_repetition(self.conn, unchanged_reads, opt_in_fingerprints=False)["signals"][0]
        self.assertEqual(explicit_unchanged["evidence_state"], "unchanged")
        self.assertEqual(explicit_unchanged["evidence_basis"], "source_change_evidence")
        self.assertIsNone(explicit_unchanged["content_fingerprint"])
        self.assertEqual(explicit_unchanged["content_fingerprints"], [])

    def test_source_coverage_describes_native_ingestion(self) -> None:
        res = analyze(self.conn, {"contract_version": "1", "harness_scope": "combined"})
        cov = res["source_coverage"]

        self.assertEqual(cov["tools"], "supported")
        self.assertEqual(cov["session_hierarchy"], "supported")
        self.assertEqual(cov["mcp_tools"], "unavailable_in_current_adapters")
        self.assertEqual(cov["skills_and_hooks"], "unavailable_in_current_adapters")
        self.assertIn("Native ingestion extracts source-present", cov["missing_ingestion_path"])
        self.assertIn("not synthesized", cov["missing_ingestion_path"])


if __name__ == "__main__":
    unittest.main()
