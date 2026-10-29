"""Comprehensive unit and integration tests for Token Ledger Query Engine.

Verifies:
- validate_query: Version enforcement, field allowlists, timezone/DST parsing, filter normalization, sort/pagination bounds.
- Half-open instant boundaries [start, end) and accurate IANA timezone conversions.
- DST boundary handling and transition warning detection.
- Filter intersection across model, provider, project, session, route, and task (manual outcomes).
- Canonical exclusive session counts (COUNT(DISTINCT session_id)).
- Unresolved overlap detection: combined total flagged as partial, warning emitted, never silently exact sum.
- Null-aware disjoint billable counters (6 counters + non-additive reasoning subset).
- Financial facts and cost coverage: unpriced vs measured zero, missingness tracking.
- Timed eligibility: speed TPS strictly over eligible timed requests, untimed requests never assigned zero speed.
- Mutually exclusive archive combination (is_live_replaced = 0 included, is_live_replaced = 1 excluded).
- Sorting with allowlist, pagination, and parameterized search.
- All metric families: summary, timeline, models, projects, sessions, requests, events, coverage.
- Forecast, anomaly, and comparison aliases: typed options, registered definitions, actual analyzer dispatch, and metadata.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
import unittest
from zoneinfo import ZoneInfo

from token_ledger.query import execute_query, validate_query
from token_ledger.store.database import open_database


class TestQueryValidation(unittest.TestCase):
    """Test validation of request envelopes."""

    def test_valid_minimal_envelope(self):
        env = {"contract_version": "1"}
        norm = validate_query(env)
        self.assertEqual(norm["contract_version"], "1")
        self.assertEqual(norm["harness_scope"], "combined")
        self.assertEqual(norm["metric_id"], "summary")
        self.assertEqual(norm["family"], "summary")
        self.assertEqual(norm["time_range"]["timezone"], "UTC")

    def test_reject_unsupported_version(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({"contract_version": "2"})
        self.assertIn("Unsupported contract_version", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_query({})
        self.assertIn("contract_version", str(ctx.exception))

    def test_reject_unknown_top_level_fields(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({"contract_version": "1", "arbitrary_field": 123})
        self.assertIn("Unknown fields in query envelope", str(ctx.exception))

    def test_reject_invalid_harness_scope(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({"contract_version": "1", "harness_scope": "invalid-scope"})
        self.assertIn("Invalid harness_scope", str(ctx.exception))

    def test_reject_invalid_timezone(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({
                "contract_version": "1",
                "time_range": {"timezone": "Invalid/Timezone_That_Does_Not_Exist"},
            })
        self.assertIn("Invalid timezone name", str(ctx.exception))

    def test_reject_start_greater_or_equal_end(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({
                "contract_version": "1",
                "time_range": {
                    "start": "2026-10-27T00:00:00Z",
                    "end": "2026-10-26T00:00:00Z",
                },
            })
        self.assertIn("must be strictly earlier", str(ctx.exception))

    def test_reject_unknown_filter_keys(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({
                "contract_version": "1",
                "filters": {"unsupported_filter": "val"},
            })
        self.assertIn("Unknown filter keys", str(ctx.exception))

    def test_filter_normalization(self):
        env = {
            "contract_version": "1",
            "filters": {
                "model": "claude-3-5-sonnet",
                "provider": ["anthropic"],
                "project": "proj-1",
                "repository": "repo-one",
                "subscription": "recorded",
                "duration": "30m_plus",
            },
        }
        norm = validate_query(env)
        self.assertEqual(norm["filters"]["model"], ["claude-3-5-sonnet"])
        self.assertEqual(norm["filters"]["provider"], ["anthropic"])
        self.assertEqual(norm["filters"]["project"], ["proj-1"])
        self.assertEqual(norm["filters"]["repository"], ["repo-one"])
        self.assertEqual(norm["filters"]["subscription"], ["recorded"])
        self.assertEqual(norm["filters"]["duration"], ["30m_plus"])

    def test_reject_invalid_subscription_and_duration_filter_values(self):
        for key, value in (("subscription", "paid"), ("duration", "long")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"Invalid {key} filter"):
                validate_query({"contract_version": "1", "filters": {key: value}})

    def test_reject_disallowed_sort_field(self):
        with self.assertRaises(ValueError) as ctx:
            validate_query({
                "contract_version": "1",
                "metric_id": "models",
                "sort": {"field": "disallowed_sql_injection_field", "order": "asc"},
            })
        self.assertIn("is not allowed for family 'models'", str(ctx.exception))

    def test_pagination_validation(self):
        # Valid
        norm = validate_query({
            "contract_version": "1",
            "pagination": {"limit": 50, "offset": 10},
        })
        self.assertEqual(norm["pagination"]["limit"], 50)
        self.assertEqual(norm["pagination"]["offset"], 10)

        # Negative offset
        with self.assertRaises(ValueError):
            validate_query({
                "contract_version": "1",
                "pagination": {"offset": -1},
            })

        # Out of bounds limit
        with self.assertRaises(ValueError):
            validate_query({
                "contract_version": "1",
                "pagination": {"limit": 10000},
            })

    def test_dst_transition_detection(self):
        # In America/New_York, 2026-11-01 has fall-back DST transition
        env = {
            "contract_version": "1",
            "time_range": {
                "start": "2026-10-31",
                "end": "2026-11-02",
                "timezone": "America/New_York",
            },
        }
        norm = validate_query(env)
        self.assertTrue(norm["time_range"]["dst_transition"])



    def test_normalized_envelopes_are_revalidated_without_marker_bypass(self):
        normalized = validate_query({
            "contract_version": "1",
            "metric_id": "forecasts",
            "time_range": {"start": "2026-10-01", "end": "2026-10-02"},
            "history_window": {"start": "2026-10-01", "end": "2026-10-02"},
        })
        self.assertNotIn("_validated", normalized)
        self.assertEqual(validate_query(normalized), normalized)
        normalized["_validated"] = True
        with self.assertRaisesRegex(ValueError, "Unknown fields in query envelope"):
            validate_query(normalized)

    def test_family_options_are_typed_and_scoped(self):
        forecast = validate_query({
            "contract_version": "1",
            "metric_id": "forecast",
            "time_range": {"timezone": "UTC"},
            "history_window": {"start": "2026-10-01", "end": "2026-10-02"},
        })
        self.assertEqual(forecast["family"], "forecasts")
        self.assertEqual(
            forecast["history_window"],
            {
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
                "timezone": "UTC",
            },
        )

        scenario = validate_query({
            "contract_version": "1",
            "metric_id": "efficiency",
            "scenario_inputs": {
                "scenario_type": "model-switch",
                "source_provider": "provider-a",
                "target_provider": "provider-b",
                "source_route": "direct",
                "target_route": "proxy",
            },
        })
        self.assertNotIn("context_threshold_tokens", scenario["scenario_inputs"])
        threshold = validate_query({
            "contract_version": "1",
            "metric_id": "efficiency",
            "scenario_inputs": {"context_threshold_tokens": 0},
        })
        self.assertEqual(threshold["scenario_inputs"]["context_threshold_tokens"], 0)

        burn = validate_query({
            "contract_version": "1",
            "metric_id": "burn",
            "interval_minutes": 15,
        })
        self.assertEqual(burn["interval_minutes"], 15)
        live = validate_query({
            "contract_version": "1",
            "metric_id": "live",
            "heuristic_window_minutes": 30,
        })
        self.assertEqual(live["heuristic_window_minutes"], 30)

        malformed = (
            {"metric_id": "forecasts", "threshold_multiplier": 2},
            {"metric_id": "anomalies", "threshold_multiplier": "2"},
            {"metric_id": "anomalies", "anomaly_settings": {"unknown": True}},
            {"metric_id": "comparisons", "comparison_periods": {}},
            {"metric_id": "efficiency", "scenario_inputs": {"fresh_input_tokens": 500}},
            {
                "metric_id": "efficiency",
                "scenario_inputs": {"context_threshold_tokens": -1},
            },
            {"metric_id": "tokens", "simulator_parameters": {"fresh_rate_per_m": 0}},
            {"metric_id": "burn", "heuristic_window_minutes": 10},
            {"metric_id": "forecasts", "history_window": {"start": 42}},
            {"metric_id": "forecasts", "history_window": {"unknown": "value"}},
            {"metric_id": "anomalies", "history_window": {"start": "2026-10-01"}},
        )
        for fields in malformed:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_query({"contract_version": "1", **fields})

    def test_analytics_aliases_normalize_to_registered_families(self):
        aliases = {
            "forecasts": ("forecasts", "forecast", "usage_forecast"),
            "anomalies": ("anomalies", "usage_anomalies", "speed_anomalies"),
            "comparisons": ("comparisons", "before_after_comparison"),
        }
        from token_ledger.metrics import get_metric_definition

        for family, metric_ids in aliases.items():
            for metric_id in metric_ids:
                with self.subTest(metric_id=metric_id):
                    normalized = validate_query({
                        "contract_version": "1",
                        "metric_id": metric_id,
                    })
                    self.assertEqual(normalized["family"], family)
                    self.assertIsNotNone(get_metric_definition(metric_id))


class TestQueryEngineExecution(unittest.TestCase):
    """End-to-end integration tests using an in-memory SQLite token ledger."""

    def setUp(self):
        self.conn = open_database(":memory:", run_migrations=True)
        self._populate_fixtures()

    def tearDown(self):
        self.conn.close()

    def _populate_fixtures(self):
        cur = self.conn.cursor()

        # 1. Projects
        cur.execute("""
        INSERT INTO projects (project_id, project_name, project_alias, account_alias, is_active, created_at)
        VALUES
            ('proj-alpha', 'Alpha Project', 'alpha', 'default', 1, '2026-10-01T00:00:00Z'),
            ('proj-beta', 'Beta Project', 'beta', 'default', 1, '2026-10-01T00:00:00Z');
        """)

        # 2. Sessions
        cur.execute("""
        INSERT INTO sessions (session_id, origin_harness, project_id, parent_session_id, account_alias, is_subagent, started_at, ended_at, duration_ms, status, created_at)
        VALUES
            ('sess-1', 'claude-code', 'proj-alpha', NULL, 'default', 0, '2026-10-15T10:00:00Z', '2026-10-15T10:30:00Z', 1800000, 'completed', '2026-10-15T10:00:00Z'),
            ('sess-2', 'omp', 'proj-beta', NULL, 'default', 0, '2026-10-15T11:00:00Z', '2026-10-15T11:45:00Z', 2700000, 'completed', '2026-10-15T11:00:00Z'),
            ('sess-3-sub', 'claude-code', 'proj-alpha', 'sess-1', 'default', 1, '2026-10-15T10:10:00Z', '2026-10-15T10:20:00Z', 600000, 'completed', '2026-10-15T10:10:00Z');
        """)

        # 3. Canonical Requests
        # req-1: claude-code, timed, priced
        # req-2: claude-code, untimed, unpriced
        # req-3: omp, timed, measured zero cost
        # req-4: boundary test request at 2026-10-01T00:00:00Z
        # req-5: boundary test request at 2026-10-02T00:00:00Z
        cur.execute("""
        INSERT INTO canonical_requests (
            canonical_id, provider, account_alias, provider_request_id, origin_harness, access_route,
            raw_model, normalized_model, session_id, project_id, request_status, request_timestamp,
            timing_basis, duration_ms, ttft_ms, generation_duration_ms, generation_speed_tps,
            is_unresolved_overlap, created_at
        ) VALUES
            ('req-1', 'anthropic', 'default', 'prov-1', 'claude-code', 'direct',
             'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-1', 'proj-alpha', 'ok',
             '2026-10-15T10:05:00Z', 'measured', 5000, 500, 4500, 44.44, 0, '2026-10-15T10:05:00Z'),
            ('req-2', 'anthropic', 'default', 'prov-2', 'claude-code', 'direct',
             'claude-3-5-haiku-20241022', 'claude-3-5-haiku', 'sess-1', 'proj-alpha', 'ok',
             '2026-10-15T10:15:00Z', 'none', NULL, NULL, NULL, NULL, 0, '2026-10-15T10:15:00Z'),
            ('req-3', 'google', 'default', 'prov-3', 'omp', 'default',
             'gemini-flash', 'gemini-3.8-flash', 'sess-2', 'proj-beta', 'ok',
             '2026-10-15T11:05:00Z', 'measured', 2000, 300, 1700, 58.82, 0, '2026-10-15T11:05:00Z'),
            ('req-4', 'anthropic', 'default', 'prov-4', 'claude-code', 'direct',
             'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-1', 'proj-alpha', 'ok',
             '2026-10-01T00:00:00Z', 'none', NULL, NULL, NULL, NULL, 0, '2026-10-01T00:00:00Z'),
            ('req-5', 'anthropic', 'default', 'prov-5', 'claude-code', 'direct',
             'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet', 'sess-3-sub', 'proj-alpha', 'ok',
             '2026-10-02T00:00:00Z', 'none', NULL, NULL, NULL, NULL, 0, '2026-10-02T00:00:00Z');
        """)

        # 4. Normalized Usage
        # 6 disjoint counters + non-additive reasoning subset
        cur.execute("""
        INSERT INTO normalized_usage (
            canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
            cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
            reasoning_tokens, reported_total_tokens, counter_inclusion_semantics
        ) VALUES
            ('req-1', 1000, 500, 200, 100, 50, 200, 50, 2050, 'disjoint'),
            ('req-2', 500, 0, 0, 0, 0, 100, 0, 600, 'disjoint'),
            ('req-3', 2000, 1000, 0, 0, 0, 100, 0, 3100, 'disjoint'),
            ('req-4', 100, 0, 0, 0, 0, 50, 0, 150, 'disjoint'),
            ('req-5', 100, 0, 0, 0, 0, 50, 0, 150, 'disjoint');
        """)

        # 5. Financial Facts
        cur.execute("""
        INSERT INTO financial_facts (
            canonical_id, actual_billed_amount, api_equivalent_usd, api_equivalent_inr,
            subscription_fee_usd, cache_savings_usd, pricing_status, fx_rate_applied, calculated_at
        ) VALUES
            ('req-1', 0.05, 0.065, 5.52, 9.99, 0.015, 'priced', 85.0, '2026-10-15T10:05:00Z'),
            ('req-2', NULL, NULL, NULL, NULL, NULL, 'unpriced', NULL, '2026-10-15T10:15:00Z'),
            ('req-3', 0.00, 0.00, 0.00, NULL, 0.00, 'priced', 85.0, '2026-10-15T11:05:00Z'),
            ('req-4', 0.01, 0.01, 0.85, NULL, 0.00, 'priced', 85.0, '2026-10-01T00:00:00Z'),
            ('req-5', 0.01, 0.01, 0.85, NULL, 0.00, 'priced', 85.0, '2026-10-02T00:00:00Z');
        """)

        # 6. Manual settings and outcomes (Task labels)
        cur.execute("""
        INSERT INTO manual_settings_and_outcomes (
            outcome_id, entity_type, entity_id, account_alias, task_label,
            completion_rating, manual_rating, is_inferred, created_at, updated_at
        ) VALUES
            ('out-1', 'session', 'sess-1', 'default', 'refactoring', 'success', 5.0, 0, '2026-10-15T10:00:00Z', '2026-10-15T10:00:00Z'),
            ('out-2', 'session', 'sess-2', 'default', 'bug-fixing', 'success', 4.0, 0, '2026-10-15T11:00:00Z', '2026-10-15T11:00:00Z');
        """)

        # 7. Archive partitions (1 active, 1 superseded)
        cur.execute("""
        INSERT INTO archive_partitions (
            partition_id, partition_type, grain, source_name, timezone_name,
            start_time, end_time, total_tokens, total_cost_usd, records_count,
            pricing_status, is_reconciled, is_live_replaced, payload_json, created_at
        ) VALUES
            ('arch-active', 'aggregate', 'daily', 'legacy_logs', 'UTC',
             '2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z', 50000, 1.25, 20,
             'priced', 0, 0, '{}', '2026-09-02T01:00:00Z'),
            ('arch-superseded', 'aggregate', 'daily', 'legacy_logs', 'UTC',
             '2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z', 45000, 1.10, 18,
             'priced', 1, 1, '{}', '2026-09-02T00:00:00Z');
        """)

        # 8. Events
        cur.execute("""
        INSERT INTO events (
            event_id, session_id, canonical_id, parent_event_id, event_kind,
            event_name, started_at, duration_ms, output_size_bytes, status, details_json
        ) VALUES
            ('ev-1', 'sess-1', 'req-1', NULL, 'tool_call', 'read_file', '2026-10-15T10:05:01Z', 45, 1200, 'ok', '{}'),
            ('ev-2', 'sess-1', 'req-1', NULL, 'tool_call', 'edit_file', '2026-10-15T10:05:05Z', 120, 3400, 'ok', '{}');
        """)

        # 9. Historical FX
        cur.execute("""
        INSERT INTO historical_fx (base_currency, target_currency, rate_per_usd, effective_date, source_provenance, created_at)
        VALUES ('USD', 'INR', 85.0, '2026-10-01', 'manual', '2026-10-01T00:00:00Z');
        """)

        self.conn.commit()

    def test_overview_bucket_model_coverage_and_duration(self):
        self.conn.execute("UPDATE sessions SET duration_ms = NULL WHERE session_id = 'sess-2'")
        env = {
            "contract_version": "1", "metric_id": "timeline",
            "time_range": {"start": "2026-10-15", "end": "2026-10-16", "timezone": "UTC"},
        }
        daily = execute_query(self.conn, env)["rows"][0]
        self.assertEqual(daily["sessions_count"], 2)
        self.assertIsNone(daily["active_minutes"])
        self.assertEqual(daily["api_equivalent_usd_status"], "partial")
        model_rows = execute_query(self.conn, {**env, "group_by": ["day", "model"]})["rows"]
        self.assertEqual(len(model_rows), 3)
        zero = next(row for row in model_rows if row["model"] == "gemini-3.8-flash")
        self.assertEqual(zero["api_equivalent_usd"], 0)
        self.assertEqual(zero["bucket_start"], "2026-10-15T00:00:00+00:00")
        self.assertEqual(zero["bucket_end"], "2026-10-16T00:00:00+00:00")
        summary = execute_query(self.conn, {**env, "metric_id": "summary"})
        self.assertEqual(summary["missingness"]["session_duration_observed_count"], 1)
        self.assertEqual(summary["missingness"]["session_duration_sessions_count"], 2)
        models = execute_query(self.conn, {**env, "metric_id": "models"})["rows"]
        self.assertTrue(all(row["proportions"]["api_equivalent_usd"] is None for row in models))
        self.assertAlmostEqual(sum(row["proportions"]["requests_count"] for row in models), 1)

    def test_timeline_interval_union_and_partial_boundaries(self):
        self.conn.executemany(
            """INSERT INTO events
               (event_id, session_id, canonical_id, event_kind, event_name,
                started_at, ended_at, duration_ms, status, details_json)
               VALUES (?, 'sess-1', ?, 'request', 'request', ?, ?, ?, 'ok', '{}')""",
            [
                ('interval-a', 'req-1', '2026-10-15T10:02:00Z', '2026-10-15T10:12:00Z', None),
                ('interval-b', 'req-2', '2026-10-15T10:10:00Z', None, 600000),
            ],
        )
        env = {"contract_version": "1", "metric_id": "timeline",
               "time_range": {"start": "2026-10-15T10:04:00Z", "end": "2026-10-15T10:18:00Z", "timezone": "UTC"}}
        row = execute_query(self.conn, env)["rows"][0]
        self.assertEqual(row["active_minutes"], 14)
        self.assertEqual(row["active_minutes_basis"], "explicit_request_event_interval_union")
        self.assertEqual(row["active_minutes_timed_requests"], 2)
        self.assertEqual(row["bucket_start"], "2026-10-15T10:04:00+00:00")
        self.assertEqual(row["bucket_end"], "2026-10-15T10:18:00+00:00")
        filtered = execute_query(self.conn, {**env, "filters": {"model": "claude-3-5-haiku"}})["rows"][0]
        self.assertEqual(filtered["active_minutes"], 8)
        crossing = execute_query(self.conn, {**env, "time_range": {
            "start": "2026-10-15T10:10:00Z", "end": "2026-10-15T10:14:00Z", "timezone": "UTC"}})["rows"][0]
        self.assertEqual(crossing["requests_count"], 0)
        self.assertEqual(crossing["active_minutes"], 4)
        self.assertEqual(crossing["active_minutes_status"], "partial")

    def test_timeline_dst_repeated_hours_and_day_lengths(self):
        for ident, stamp in [('fold-a', '2026-11-01T05:30:00Z'),
                             ('fold-b', '2026-11-01T06:30:00Z'),
                             ('spring', '2026-03-08T06:30:00Z')]:
            self.conn.execute("""INSERT INTO canonical_requests
                (canonical_id, provider, origin_harness, raw_model, normalized_model, request_timestamp, created_at)
                VALUES (?, 'example', 'omp', 'model', 'model', ?, ?)""", (ident, stamp, stamp))
        self.conn.execute("""INSERT INTO events
            (event_id, session_id, canonical_id, event_kind, event_name, started_at, ended_at, status, details_json)
            VALUES ('fold-interval', 'sess-1', 'fold-a', 'request', 'request',
                    '2026-11-01T05:45:00Z', '2026-11-01T06:15:00Z', 'ok', '{}')""")
        base = {"contract_version": "1", "metric_id": "timeline",
                "time_range": {"start": "2026-11-01", "end": "2026-11-02", "timezone": "America/New_York"}}
        hours = execute_query(self.conn, {**base, "group_by": ["hour"]})["rows"]
        self.assertEqual([row["bucket"] for row in hours],
                         ['2026-11-01T01:00:00-04:00', '2026-11-01T01:00:00-05:00'])
        self.assertEqual([row["active_minutes"] for row in hours], [15, 15])
        day = execute_query(self.conn, base)["rows"][0]
        self.assertEqual(day["bucket_start"], '2026-11-01T04:00:00+00:00')
        self.assertEqual(day["bucket_end"], '2026-11-02T05:00:00+00:00')
        spring = execute_query(self.conn, {**base, "time_range": {
            "start": "2026-03-08", "end": "2026-03-09", "timezone": "America/New_York"}})["rows"][0]
        self.assertEqual(spring["bucket_start"], '2026-03-08T05:00:00+00:00')
        self.assertEqual(spring["bucket_end"], '2026-03-09T04:00:00+00:00')

    def test_half_open_instant_boundaries(self):
        # Range [2026-10-01T00:00:00Z, 2026-10-02T00:00:00Z)
        # Must include req-4 (at 2026-10-01T00:00:00Z)
        # Must EXCLUDE req-5 (at 2026-10-02T00:00:00Z)
        env = {
            "contract_version": "1",
            "time_range": {
                "start": "2026-10-01T00:00:00Z",
                "end": "2026-10-02T00:00:00Z",
                "timezone": "UTC",
            },
            "metric_id": "requests",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        req_ids = [r["canonical_id"] for r in res["rows"]]
        self.assertIn("req-4", req_ids)
        self.assertNotIn("req-5", req_ids)
        self.assertEqual(len(req_ids), 1)

    def test_filter_intersection(self):
        # Filter for model=claude-3-5-sonnet AND task=refactoring AND project=proj-alpha
        env = {
            "contract_version": "1",
            "filters": {
                "model": "claude-3-5-sonnet",
                "task": "refactoring",
                "project": "proj-alpha",
            },
            "metric_id": "requests",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        req_ids = [r["canonical_id"] for r in res["rows"]]
        self.assertIn("req-1", req_ids)
        self.assertNotIn("req-2", req_ids)  # haiku
        self.assertNotIn("req-3", req_ids)  # gemini, task bug-fixing
    def test_null_project_sentinel_and_unknown_duration(self):
        self.conn.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, origin_harness, raw_model, normalized_model,
                project_id, request_timestamp, created_at
            ) VALUES ('req-unattributed', 'anthropic', 'claude-code', 'model-x', 'model-x',
                      NULL, '2026-10-16T10:00:00Z', '2026-10-16T10:00:00Z')
            """
        )
        for metric_id in ("requests", "projects"):
            response = execute_query(self.conn, {
                "contract_version": "1",
                "metric_id": metric_id,
                "filters": {"project": "unattributed", "duration": "unknown"},
                "include_archive": False,
            })
            if metric_id == "requests":
                self.assertEqual(
                    [row["canonical_id"] for row in response["rows"]],
                    ["req-unattributed"],
                )
            else:
                self.assertEqual(
                    [row["project_id"] for row in response["rows"]],
                    ["unattributed"],
                )

    def test_multi_filter_scope_across_query_families(self):
        filters = {
            "application": "claude-code",
            "model": "claude-3-5-sonnet",
            "provider": "anthropic",
            "project": "proj-alpha",
            "repository": "alpha",
            "session": "sess-1",
            "route": "direct",
            "task": "refactoring",
            "subscription": "recorded",
            "duration": "30m_plus",
        }
        base = {
            "contract_version": "1",
            "filters": filters,
            "time_range": {
                "start": "2026-10-01",
                "end": "2026-10-20",
                "timezone": "UTC",
            },
            "include_archive": False,
        }
        families = (
            "summary", "timeline", "models", "projects", "sessions", "requests", "events",
            "coverage", "tokens", "speed", "traces", "providers", "provider_quotas",
            "burn", "live", "budgets", "model_profiles", "outcomes", "efficiency",
            "forecasts", "anomalies", "comparisons",
        )
        for metric_id in families:
            with self.subTest(metric_id=metric_id):
                envelope = {**base, "metric_id": metric_id}
                if metric_id == "forecasts":
                    envelope["history_window"] = {
                        "start": "2026-10-01",
                        "end": "2026-10-20",
                        "timezone": "UTC",
                    }
                elif metric_id == "comparisons":
                    envelope["comparison_periods"] = {
                        "period_a": {
                            "start": "2026-10-01",
                            "end": "2026-10-03",
                            "label": "before",
                        },
                        "period_b": {
                            "start": "2026-10-15",
                            "end": "2026-10-16",
                            "label": "after",
                        },
                    }
                response = execute_query(self.conn, envelope)
                self.assertEqual(response["query"]["filters"], {
                    key: [value] for key, value in filters.items()
                })
                if metric_id == "summary":
                    self.assertEqual(response["values"]["requests_count"], 1)
                elif metric_id == "requests":
                    self.assertEqual(
                        [row["canonical_id"] for row in response["rows"]],
                        ["req-1"],
                    )
                elif metric_id == "events":
                    self.assertEqual(
                        [row["event_id"] for row in response["rows"]],
                        ["ev-2", "ev-1"],
                    )
                elif metric_id == "providers":
                    self.assertEqual(
                        [(row["provider"], row["access_route"], row["request_count"]) for row in response["rows"]],
                        [("anthropic", "direct", 1)],
                    )
                elif metric_id == "provider_quotas":
                    self.assertEqual(response["values"]["quotas"], [])
                    self.assertTrue(any("quota cards were omitted" in warning for warning in response["warnings"]))
                elif metric_id == "coverage":
                    self.assertTrue(any("Coverage is global" in warning for warning in response["warnings"]))

    def test_provider_quotas_apply_provider_route_and_time_filters(self):
        self.conn.executemany(
            """
            INSERT INTO subscription_quotas (
                provider, account_alias, access_route, window_type, limit_value, used_value,
                remaining_value, reset_at, measured_at, status, provenance_json
            ) VALUES (?, 'default', ?, 'daily', 1000, 100, 900, ?, ?, 'available',
                      '{"units":"tokens","official_source":"fixture"}')
            """,
            (
                ("anthropic", "direct", "2999-01-01T00:00:00Z", "2026-10-10T00:00:00Z"),
                ("anthropic", "direct", "2999-01-01T00:00:00Z", "2026-09-10T00:00:00Z"),
                ("anthropic", "bedrock", "2999-01-01T00:00:00Z", "2026-10-10T00:00:00Z"),
                ("openai", "direct", "2999-01-01T00:00:00Z", "2026-10-10T00:00:00Z"),
            ),
        )
        response = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "provider_quotas",
            "filters": {"provider": "anthropic", "route": "direct"},
            "time_range": {
                "start": "2026-10-01",
                "end": "2026-10-20",
                "timezone": "UTC",
            },
            "include_archive": False,
        })
        self.assertEqual(
            [
                (card["provider"], card["access_route"], card["official_timestamp"])
                for card in response["values"]["quotas"]
            ],
            [("anthropic", "direct", "2026-10-10T00:00:00Z")],
        )

    def test_filtered_summary_omits_unscoped_archive(self):
        response = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "summary",
            "filters": {"repository": "alpha"},
            "time_range": {
                "start": "2026-09-01",
                "end": "2026-09-03",
                "timezone": "UTC",
            },
            "include_archive": True,
        })
        self.assertEqual(response["values"]["archive_tokens"], 0)
        self.assertTrue(any("omitted from this filtered summary" in warning for warning in response["warnings"]))

    def test_exclusive_session_counts(self):
        # Querying requests in sess-1 (which has 3 requests: req-1, req-2, req-4)
        env = {
            "contract_version": "1",
            "filters": {"session": "sess-1"},
            "metric_id": "summary",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        vals = res["values"]
        self.assertEqual(vals["live_requests_count"], 3)
        self.assertEqual(vals["sessions_count"], 1)  # Strictly 1 session, never double-counted

    def test_missing_session_ids_report_unknown_not_zero(self):
        # Ingest gap: oh-my-pi requests carry no session id at all.
        self.conn.execute("UPDATE canonical_requests SET session_id = NULL WHERE origin_harness = 'omp'")
        self.conn.commit()

        omp = execute_query(self.conn, {
            "contract_version": "1",
            "harness_scope": "omp",
            "metric_id": "summary",
            "include_archive": False,
        })["values"]
        self.assertEqual(omp["live_requests_count"], 1)
        self.assertIsNone(omp["sessions_count"])  # unknown, not 0

        # Combined still counts the known Claude Code sessions (sess-1 and sess-3-sub).
        combined = execute_query(self.conn, {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "summary",
            "include_archive": False,
        })["values"]
        self.assertEqual(combined["sessions_count"], 2)

        # Model rows follow the same rule.
        model_rows = execute_query(self.conn, {
            "contract_version": "1",
            "harness_scope": "omp",
            "metric_id": "models",
            "include_archive": False,
        })["rows"]
        self.assertTrue(model_rows)
        self.assertTrue(all(row["sessions_count"] is None for row in model_rows))

        # A scope with zero requests is genuinely zero sessions, not unknown.
        empty = execute_query(self.conn, {
            "contract_version": "1",
            "harness_scope": "omp",
            "metric_id": "summary",
            "time_range": {"start": "2020-01-01", "end": "2020-01-02", "timezone": "UTC"},
            "include_archive": False,
        })["values"]
        self.assertEqual(empty["live_requests_count"], 0)
        self.assertEqual(empty["sessions_count"], 0)

    def test_missing_session_ids_unknown_in_projects(self):
        self.conn.execute("UPDATE canonical_requests SET session_id = NULL WHERE origin_harness = 'omp'")
        self.conn.commit()

        # Projects: beta has only the omp request, which records no session id.
        omp_projects = {
            row["project_id"]: row
            for row in execute_query(self.conn, {
                "contract_version": "1",
                "harness_scope": "omp",
                "metric_id": "projects",
                "include_archive": False,
            })["rows"]
        }
        self.assertEqual(omp_projects["proj-beta"]["requests_count"], 1)
        self.assertIsNone(omp_projects["proj-beta"]["sessions_count"])  # unknown, not 0

        # Combined still counts the known Claude Code sessions in alpha.
        combined_projects = {
            row["project_id"]: row
            for row in execute_query(self.conn, {
                "contract_version": "1",
                "harness_scope": "combined",
                "metric_id": "projects",
                "include_archive": False,
            })["rows"]
        }
        self.assertEqual(combined_projects["proj-alpha"]["sessions_count"], 2)
        self.assertIsNone(combined_projects["proj-beta"]["sessions_count"])

    def test_missing_session_ids_unknown_in_timeline(self):
        self.conn.execute("UPDATE canonical_requests SET session_id = NULL WHERE origin_harness = 'omp'")
        self.conn.commit()
        time_range = {"start": "2026-10-15", "end": "2026-10-16", "timezone": "UTC"}

        # The omp day has requests but no session ids, so the bucket is unknown.
        omp_day = execute_query(self.conn, {
            "contract_version": "1",
            "harness_scope": "omp",
            "metric_id": "timeline",
            "time_range": time_range,
            "include_archive": False,
        })["rows"]
        self.assertEqual(len(omp_day), 1)
        self.assertEqual(omp_day[0]["requests_count"], 1)
        self.assertIsNone(omp_day[0]["sessions_count"])  # unknown, not 0

        # Combined day keeps the known sess-1 session from Claude Code.
        combined_day = execute_query(self.conn, {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "timeline",
            "time_range": time_range,
            "include_archive": False,
        })["rows"]
        self.assertEqual(len(combined_day), 1)
        self.assertEqual(combined_day[0]["sessions_count"], 1)

    def test_unresolved_overlap_flagging(self):
        # Mark req-1 as unresolved overlap
        cur = self.conn.cursor()
        cur.execute("UPDATE canonical_requests SET is_unresolved_overlap = 1 WHERE canonical_id = 'req-1'")
        self.conn.commit()

        env = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "summary",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        vals = res["values"]
        self.assertTrue(vals["has_unresolved_overlap"])
        self.assertEqual(vals["total_tokens_status"], "partial")
        self.assertTrue(any("Unresolved overlap detected" in w for w in res["warnings"]))

        # Restore
        cur.execute("UPDATE canonical_requests SET is_unresolved_overlap = 0 WHERE canonical_id = 'req-1'")
        self.conn.commit()

    def test_null_aware_disjoint_tokens_and_reasoning_subset(self):
        # req-1 has fresh: 1000, read: 500, write5m: 200, write1h: 100, write_unk: 50, output: 200, reasoning: 50
        # Disjoint sum = 1000 + 500 + 200 + 100 + 50 + 200 = 2050
        # Reasoning (50) is a reported subset of output (200), must NOT be added again to total_tokens
        env = {
            "contract_version": "1",
            "filters": {"session": "sess-1"},
            "time_range": {
                "start": "2026-10-15T10:00:00Z",
                "end": "2026-10-15T10:10:00Z",
                "timezone": "UTC",
            },
            "metric_id": "summary",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        vals = res["values"]
        self.assertEqual(vals["fresh_input_tokens"], 1000)
        self.assertEqual(vals["cache_read_tokens"], 500)
        self.assertEqual(vals["cache_write_5m_tokens"], 200)
        self.assertEqual(vals["cache_write_1h_tokens"], 100)
        self.assertEqual(vals["cache_write_unknown_ttl_tokens"], 50)
        self.assertEqual(vals["output_tokens"], 200)
        self.assertEqual(vals["reasoning_tokens"], 50)
        self.assertEqual(vals["live_disjoint_tokens"], 2050)
        self.assertEqual(vals["total_tokens"], 2050)

        # Cache hit rate = 500 / (1000 + 500) = 500 / 1500 = 0.3333
        self.assertAlmostEqual(vals["cache_hit_rate"], 0.3333, places=3)

    def test_cost_and_priced_coverage(self):
        # sess-1 has req-1 (priced, $0.065), req-2 (unpriced, NULL), req-4 (priced, $0.01)
        env = {
            "contract_version": "1",
            "filters": {"session": "sess-1"},
            "metric_id": "summary",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        vals = res["values"]
        miss = res["missingness"]
        self.assertAlmostEqual(vals["api_equivalent_usd"], 0.075, places=3)
        self.assertEqual(miss["unpriced_requests_count"], 1)
        self.assertEqual(miss["priced_requests_count"], 2)
        # Measured zero: req-3 in sess-2 has 0.00 cost, status 'priced'
        env2 = {
            "contract_version": "1",
            "filters": {"session": "sess-2"},
            "metric_id": "summary",
            "include_archive": False,
        }
        res2 = execute_query(self.conn, env2)
        self.assertEqual(res2["values"]["api_equivalent_usd"], 0.0)
        self.assertEqual(res2["missingness"]["unpriced_requests_count"], 0)
        self.assertEqual(res2["missingness"]["priced_requests_count"], 1)

    def test_aggregates_preserve_zero_missing_and_partial_values(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO projects (
                project_id, project_name, project_alias, account_alias, is_active, created_at
            ) VALUES ('missingness-project', 'Missingness Project', 'missingness', 'default', 1,
                '2026-11-01T00:00:00Z')
            """
        )
        cur.execute(
            """
            INSERT INTO sessions (
                session_id, origin_harness, project_id, account_alias, is_subagent,
                started_at, created_at
            ) VALUES ('missingness-session', 'claude-code', 'missingness-project', 'default', 0,
                '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')
            """
        )
        request_ids = ("miss-zero", "miss-unknown", "miss-partial")
        cur.executemany(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, is_unresolved_overlap, created_at
            ) VALUES (?, 'missingness-provider', 'default', ?, 'claude-code', 'direct',
                'fixture-model', 'fixture-model', 'missingness-session', 'missingness-project',
                'ok', ?, 'none', 0, ?)
            """,
            [
                (request_id, f"{request_id}-provider-id", "2026-11-01T00:00:00Z",
                    "2026-11-01T00:00:00Z")
                for request_id in request_ids
            ],
        )
        cur.executemany(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("miss-zero", 0, 0, 0, 0, 0, 0, 0, 0),
                ("miss-unknown", None, None, None, None, None, None, None, None),
                ("miss-partial", 7, 1, None, None, None, 2, None, None),
            ],
        )
        cur.executemany(
            """
            INSERT INTO financial_facts (
                canonical_id, actual_billed_amount, api_equivalent_usd, api_equivalent_inr,
                subscription_fee_usd, cache_savings_usd, pricing_status, fx_rate_applied,
                calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("miss-zero", 0.0, 0.0, None, 0.0, 0.0, "priced", None,
                    "2026-11-01T00:00:00Z"),
                ("miss-unknown", None, None, None, None, None, "unpriced", None,
                    "2026-11-01T00:00:00Z"),
                ("miss-partial", 0.10, 0.25, None, 0.50, None, "partial", 85.0,
                    "2026-11-01T00:00:00Z"),
            ],
        )
        self.conn.commit()

        def summary_for(search):
            return execute_query(self.conn, {
                "contract_version": "1",
                "metric_id": "summary",
                "search": search,
                "include_archive": False,
            })["values"]

        zero = summary_for("miss-zero")
        self.assertEqual(zero["total_tokens"], 0)
        self.assertEqual(zero["total_tokens_status"], "exact")
        self.assertEqual(zero["api_equivalent_usd"], 0.0)
        self.assertEqual(zero["api_equivalent_usd_status"], "exact")

        unknown = summary_for("miss-unknown")
        self.assertIsNone(unknown["total_tokens"])
        self.assertEqual(unknown["total_tokens_status"], "unavailable")
        self.assertIsNone(unknown["api_equivalent_usd"])
        self.assertEqual(unknown["api_equivalent_usd_status"], "unavailable")

        partial = summary_for("miss-partial")
        self.assertEqual(partial["total_tokens"], 10)
        self.assertEqual(partial["total_tokens_status"], "partial")
        self.assertEqual(partial["api_equivalent_usd"], 0.25)
        self.assertEqual(partial["api_equivalent_usd_status"], "exact")

        summary = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "summary",
            "filters": {"project": "missingness-project"},
            "include_archive": False,
        })["values"]
        self.assertEqual(summary["total_tokens"], 10)
        self.assertEqual(summary["total_tokens_status"], "partial")
        self.assertEqual(summary["api_equivalent_usd"], 0.25)
        self.assertEqual(summary["api_equivalent_usd_status"], "partial")

        project = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "projects",
            "filters": {"project": "missingness-project"},
            "include_archive": False,
        })["rows"][0]
        self.assertEqual(project["total_tokens"], 10)
        self.assertEqual(project["total_tokens_status"], "partial")
        self.assertEqual(project["fresh_input_tokens"], 7)
        self.assertEqual(project["cache_read_tokens"], 1)
        self.assertEqual(project["output_tokens"], 2)
        self.assertEqual(project["cache_write_tokens"], 0)
        self.assertEqual(project["token_fields_status"]["cache_write_tokens"], "partial")
        self.assertEqual(project["api_equivalent_usd"], 0.25)
        self.assertEqual(project["api_equivalent_usd_status"], "partial")

        session = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "sessions",
            "filters": {"session": "missingness-session"},
            "include_archive": False,
        })["rows"][0]
        self.assertEqual(session["total_tokens"], 10)
        self.assertEqual(session["total_tokens_status"], "partial")
        self.assertEqual(session["api_equivalent_usd"], 0.25)
        self.assertEqual(session["api_equivalent_usd_status"], "partial")

        tokens = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "tokens",
            "group_by": ["project"],
            "filters": {"project": "missingness-project"},
            "include_archive": False,
        })["rows"][0]
        self.assertEqual(tokens["total_tokens"], 10)
        self.assertEqual(tokens["total_tokens_status"], "partial")
        self.assertEqual(tokens["fresh_input_tokens"], 7)
        self.assertEqual(tokens["token_fields_status"]["fresh_input_tokens"], "partial")
        self.assertEqual(tokens["api_equivalent_usd"], 0.25)
        self.assertEqual(tokens["api_equivalent_usd_status"], "partial")
        self.assertEqual(tokens["cache_savings_usd"], 0.0)
        self.assertEqual(tokens["cache_savings_usd_status"], "partial")

        providers = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "providers",
            "filters": {"provider": "missingness-provider"},
            "include_archive": False,
        })
        financials = providers["values"]["financial_summary"]
        self.assertEqual(financials["api_equivalent_usd"], 0.25)
        self.assertEqual(financials["api_equivalent_usd_status"], "partial")
        self.assertIsNone(financials["api_equivalent_inr"])
        self.assertEqual(financials["api_equivalent_inr_status"], "unavailable")
        self.assertEqual(financials["subscription_fees_usd"], 0.5)
        self.assertEqual(financials["subscription_fees_usd_status"], "partial")

        no_fee = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "providers",
            "filters": {"provider": "google"},
            "include_archive": False,
        })["values"]["financial_summary"]
        self.assertIsNone(no_fee["subscription_fees_usd"])
        self.assertEqual(no_fee["subscription_fees_usd_status"], "unavailable")

        cur.executemany(
            "UPDATE canonical_requests SET normalized_model = ? WHERE canonical_id = ?",
            [
                ("zero-model", "miss-zero"),
                ("unknown-model", "miss-unknown"),
                ("partial-model", "miss-partial"),
            ],
        )
        self.conn.commit()
        models = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "models",
            "filters": {"provider": "missingness-provider"},
            "include_archive": False,
        })["rows"]
        models_by_name = {row["model"]: row for row in models}

        zero_model = models_by_name["zero-model"]
        self.assertEqual(zero_model["total_tokens"], 0)
        self.assertEqual(zero_model["total_tokens_status"], "exact")
        self.assertEqual(zero_model["fresh_input_tokens"], 0)
        self.assertEqual(zero_model["api_equivalent_usd"], 0.0)
        self.assertEqual(zero_model["api_equivalent_usd_status"], "exact")

        unknown_model = models_by_name["unknown-model"]
        self.assertIsNone(unknown_model["total_tokens"])
        self.assertEqual(unknown_model["total_tokens_status"], "unavailable")
        self.assertIsNone(unknown_model["fresh_input_tokens"])
        self.assertEqual(
            unknown_model["token_fields_status"]["fresh_input_tokens"],
            "unavailable",
        )
        self.assertIsNone(unknown_model["api_equivalent_usd"])
        self.assertEqual(unknown_model["api_equivalent_usd_status"], "unavailable")

        partial_model = models_by_name["partial-model"]
        self.assertEqual(partial_model["total_tokens"], 10)
        self.assertEqual(partial_model["total_tokens_status"], "partial")
        self.assertEqual(partial_model["fresh_input_tokens"], 7)
        self.assertEqual(partial_model["cache_read_tokens"], 1)
        self.assertIsNone(partial_model["cache_write_tokens"])
        self.assertEqual(
            partial_model["token_fields_status"]["cache_write_tokens"],
            "unavailable",
        )
        self.assertEqual(partial_model["output_tokens"], 2)
        self.assertEqual(partial_model["api_equivalent_usd"], 0.25)
        self.assertEqual(partial_model["api_equivalent_usd_status"], "exact")
        self.assertIsNone(partial_model["generation_speed_tps"])

    def test_one_cache_write_bucket_per_request_is_exact(self):
        """Cache-write buckets are alternatives: 1h-only or unknown-TTL-only rows are complete."""
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO projects (
                project_id, project_name, project_alias, account_alias, is_active, created_at
            ) VALUES ('bucket-project', 'Bucket Project', 'bucket', 'default', 1,
                '2026-11-02T00:00:00Z')
            """
        )
        cur.execute(
            """
            INSERT INTO sessions (
                session_id, origin_harness, project_id, account_alias, is_subagent,
                started_at, created_at
            ) VALUES ('bucket-session', 'claude-code', 'bucket-project', 'default', 0,
                '2026-11-02T00:00:00Z', '2026-11-02T00:00:00Z')
            """
        )
        cur.executemany(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, is_unresolved_overlap, created_at
            ) VALUES (?, 'bucket-provider', 'default', ?, 'claude-code', 'direct',
                'fixture-model', 'fixture-model', 'bucket-session', 'bucket-project',
                'ok', '2026-11-02T00:00:00Z', 'none', 0, '2026-11-02T00:00:00Z')
            """,
            [("bucket-1h", "bucket-1h-id"), ("bucket-unk", "bucket-unk-id")],
        )
        cur.executemany(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("bucket-1h", 5, 1, None, 3, None, 2, None, None),
                ("bucket-unk", 5, 1, None, None, 4, 2, None, None),
            ],
        )
        self.conn.commit()
        flt = {"project": "bucket-project"}

        def run(metric_id, **extra):
            return execute_query(self.conn, {
                "contract_version": "1", "metric_id": metric_id,
                "filters": flt, "include_archive": False, **extra,
            })

        summary = run("summary")["values"]
        self.assertEqual(summary["total_tokens"], 23)
        self.assertEqual(summary["total_tokens_status"], "exact")
        self.assertEqual(summary["live_disjoint_tokens_status"], "exact")
        self.assertEqual(summary["token_fields_status"]["cache_write_tokens"], "exact")

        project = run("projects")["rows"][0]
        self.assertEqual(project["total_tokens_status"], "exact")
        self.assertEqual(project["token_fields_status"]["cache_write_tokens"], "exact")

        session = run("sessions")["rows"][0]
        self.assertEqual(session["total_tokens_status"], "exact")

        day = run("timeline", time_range={
            "start": "2026-11-02", "end": "2026-11-03", "timezone": "UTC",
        })["rows"][0]
        self.assertEqual(day["total_tokens_status"], "exact")

    def test_timed_eligibility_separated(self):
        # req-1 has generation_duration_ms = 4500, output = 200 => speed TPS = 200 / 4.5 = 44.44
        # req-2 is untimed (timing_basis='none')
        # Total requests in sess-1 = 3 (req-1, req-2, req-4 where req-4 is also untimed)
        # Speed TPS must strictly exclude req-2 and req-4, and NOT assign them 0 speed
        env = {
            "contract_version": "1",
            "filters": {"session": "sess-1"},
            "metric_id": "summary",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        vals = res["values"]
        miss = res["missingness"]
        self.assertEqual(vals["generation_speed_tps"], 44.44)
        self.assertEqual(miss["eligible_timing_requests_count"], 1)
        self.assertEqual(miss["missing_timing_requests_count"], 2)

    def test_archive_combination_mutually_exclusive(self):
        # Querying with include_archive=True in range covering Sept 2026
        # arch-active has 50000 tokens, $1.25, is_live_replaced = 0
        # arch-superseded has is_live_replaced = 1 (must be excluded!)
        env = {
            "contract_version": "1",
            "time_range": {
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-03T00:00:00Z",
                "timezone": "UTC",
            },
            "metric_id": "summary",
            "include_archive": True,
        }
        res = execute_query(self.conn, env)
        vals = res["values"]
        self.assertEqual(vals["archive_tokens"], 50000)
        self.assertEqual(vals["live_disjoint_tokens"], 0)
        self.assertEqual(vals["total_tokens"], 50000)
        self.assertEqual(vals["api_equivalent_usd"], 1.25)
        self.assertEqual(vals["archive_records_count"], 20)

    def test_all_metric_families(self):
        families = ["summary", "timeline", "models", "projects", "sessions", "requests", "events", "coverage"]
        for fam in families:
            env = {"contract_version": "1", "metric_id": fam, "include_archive": True}
            res = execute_query(self.conn, env)
            self.assertEqual(res["contract_version"], "1")
            self.assertEqual(res["family"], fam)
            self.assertIn("metric_definition", res)
            self.assertIn("units", res)
            self.assertIn("warnings", res)
            if fam == "summary":
                self.assertTrue(len(res["values"]) > 0)
            elif fam in ("timeline", "models", "projects", "sessions", "requests", "events"):
                self.assertIsInstance(res["rows"], list)
            elif fam == "coverage":
                self.assertIn("checkpoints", res)


    def test_forecast_anomaly_and_comparison_aliases_dispatch(self):
        forecast_ids = ("forecasts", "forecast", "usage_forecast")
        for metric_id in forecast_ids:
            with self.subTest(metric_id=metric_id):
                response = execute_query(self.conn, {
                    "contract_version": "1",
                    "metric_id": metric_id,
                    "harness_scope": "claude-code",
                    "filters": {"provider": "anthropic"},
                    "time_range": {
                        "start": "2026-10-15",
                        "end": "2026-10-16",
                        "timezone": "UTC",
                    },
                    "history_window": {
                        "start": "2026-10-01",
                        "end": "2026-10-16",
                    },
                    "include_archive": False,
                })
                self.assertEqual(response["family"], "forecasts")
                self.assertEqual(response["metric_definition"]["metric_id"], metric_id)
                self.assertIn("status", response)
                self.assertEqual(response["query"]["filters"]["provider"], ["anthropic"])
                self.assertEqual(response["query"]["harness_scope"], "claude-code")
                self.assertEqual(
                    response["query"]["history_window"]["start"],
                    "2026-10-01T00:00:00Z",
                )

        for metric_id in ("anomalies", "usage_anomalies", "speed_anomalies"):
            with self.subTest(metric_id=metric_id):
                response = execute_query(self.conn, {
                    "contract_version": "1",
                    "metric_id": metric_id,
                    "harness_scope": "claude-code",
                    "filters": {"provider": "anthropic"},
                    "threshold_multiplier": 3.0,
                    "anomaly_settings": {"expected_anomalies": []},
                    "include_archive": False,
                })
                self.assertEqual(response["family"], "anomalies")
                self.assertEqual(response["metric_definition"]["metric_id"], metric_id)
                self.assertIn("baselines", response)
                self.assertIsInstance(response["rows"], list)

        comparison_periods = {
            "period_a": {
                "start": "2026-10-01",
                "end": "2026-10-03",
                "label": "Baseline",
            },
            "period_b": {
                "start": "2026-10-15",
                "end": "2026-10-16",
                "label": "Comparison",
            },
        }
        for metric_id, period_fields in (
            ("comparisons", {"comparison_periods": comparison_periods}),
            ("before_after_comparison", comparison_periods),
        ):
            with self.subTest(metric_id=metric_id):
                response = execute_query(self.conn, {
                    "contract_version": "1",
                    "metric_id": metric_id,
                    "harness_scope": "combined",
                    "filters": {"provider": "anthropic"},
                    **period_fields,
                    "include_archive": False,
                })
                self.assertEqual(response["family"], "comparisons")
                self.assertEqual(response["metric_definition"]["metric_id"], metric_id)
                self.assertIn("period_a", response)
                self.assertIn("period_b", response)
                self.assertIn("values", response)

    def test_sorting_and_pagination(self):
        env = {
            "contract_version": "1",
            "metric_id": "requests",
            "sort": {"field": "request_timestamp", "order": "asc"},
            "pagination": {"limit": 2, "offset": 0},
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        self.assertEqual(len(res["rows"]), 2)
        # Oldest first: req-4 (2026-10-01), then req-5 (2026-10-02)
        self.assertEqual(res["rows"][0]["canonical_id"], "req-4")
        self.assertEqual(res["rows"][1]["canonical_id"], "req-5")

        # Page 2
        env["pagination"]["offset"] = 2
        res2 = execute_query(self.conn, env)
        self.assertEqual(len(res2["rows"]), 2)
        self.assertEqual(res2["rows"][0]["canonical_id"], "req-1")

    def test_parameterized_search(self):
        env = {
            "contract_version": "1",
            "metric_id": "requests",
            "search": "gemini",
            "include_archive": False,
        }
        res = execute_query(self.conn, env)
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["rows"][0]["canonical_id"], "req-3")

    def test_engine_projection_fields(self):
        # 1. Sessions projection
        res_sess = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "sessions",
            "include_archive": False,
        })
        self.assertTrue(len(res_sess["rows"]) >= 2)
        sess_map = {r["session_id"]: r for r in res_sess["rows"]}

        # Single model session: sess-2 uses gemini-flash
        s2 = sess_map["sess-2"]
        self.assertEqual(s2["application_basis"], "omp")
        self.assertEqual(s2["application_basis_status"], "exact")
        self.assertEqual(s2["primary_model"], "gemini-3.8-flash")
        self.assertEqual(s2["primary_model_status"], "exact")
        self.assertEqual(s2["session_title"]["status"], "unavailable")
        self.assertIn("reason", s2["session_title"])
        self.assertIn("cache_read_tokens", s2)
        self.assertIn("cache_write_tokens", s2)
        self.assertIn("cache_read_tokens", s2["token_fields_status"])
        self.assertIn("cache_write_tokens", s2["token_fields_status"])

        # Multi-model session: sess-1 uses haiku and sonnet
        s1 = sess_map["sess-1"]
        self.assertEqual(s1["application_basis"], "claude-code")
        self.assertEqual(s1["primary_model_status"], "multi-model")
        self.assertTrue(isinstance(s1["primary_model"], list))

        # 2. Requests projection and filtered_request_rows
        res_req = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "requests",
            "pagination": {"limit": 5, "offset": 0},
            "include_archive": False,
        })
        req_0 = res_req["rows"][0]
        self.assertIn("reasoning_settings", req_0)
        self.assertEqual(req_0["reasoning_settings"]["status"], "unavailable")
        self.assertIn("reason", req_0["reasoning_settings"])
        self.assertEqual(req_0["reasoning_settings_status"], "unavailable")

        # 3. Models projection fields for Harness
        res_models = execute_query(self.conn, {
            "contract_version": "1",
            "metric_id": "models",
            "include_archive": False,
        })
        m_0 = res_models["rows"][0]
        self.assertIn("reported_total_tokens", m_0)
        self.assertIn("cache_write_5m_tokens", m_0)
        self.assertIn("cache_write_1h_tokens", m_0)
        self.assertIn("cache_write_unknown_ttl_tokens", m_0)
        self.assertIn("eligible_prompt_tokens", m_0)
        self.assertIn("priced_requests_count", m_0)
        self.assertIn("cache_savings_usd", m_0)
        self.assertIn("cache_savings_usd_status", m_0)
        self.assertIn("cache_hit_rate", m_0)

class TestQueryPerformanceSmoke(unittest.TestCase):
    """Temporary DB synthetic ingest query smoke and timings."""

    def test_synthetic_scale_smoke(self):
        conn = open_database(":memory:", run_migrations=True)
        cur = conn.cursor()

        # Ingest 1,000 synthetic canonical requests with usage and financial facts
        cur.execute("BEGIN TRANSACTION;")
        cur.execute("INSERT INTO projects (project_id, project_name, created_at) VALUES ('p-smoke', 'Smoke Project', '2026-10-01T00:00:00Z');")
        cur.execute("INSERT INTO sessions (session_id, origin_harness, project_id, created_at) VALUES ('s-smoke', 'claude-code', 'p-smoke', '2026-10-01T00:00:00Z');")

        req_params = []
        usage_params = []
        fin_params = []
        base_time = datetime.datetime(2026, 10, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)

        for i in range(1000):
            req_id = f"smoke_req_{i:04d}"
            t = (base_time + datetime.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            req_params.append((
                req_id, "anthropic", "default", f"prov_{i}", "claude-code", "direct",
                "claude-3-5-sonnet", "claude-3-5-sonnet", "s-smoke", "p-smoke", "ok",
                t, "measured", 1000, 200, 800, 50.0, 0, t
            ))
            usage_params.append((req_id, 500, 200, 50, 20, 0, 100, 10, 870, "disjoint"))
            fin_params.append((req_id, 0.01, 0.012, 1.02, 0.003, "priced", 85.0, t))

        cur.executemany("""
        INSERT INTO canonical_requests (
            canonical_id, provider, account_alias, provider_request_id, origin_harness, access_route,
            raw_model, normalized_model, session_id, project_id, request_status, request_timestamp,
            timing_basis, duration_ms, ttft_ms, generation_duration_ms, generation_speed_tps,
            is_unresolved_overlap, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, req_params)

        cur.executemany("""
        INSERT INTO normalized_usage (
            canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
            cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
            reasoning_tokens, reported_total_tokens, counter_inclusion_semantics
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, usage_params)

        cur.executemany("""
        INSERT INTO financial_facts (
            canonical_id, actual_billed_amount, api_equivalent_usd, api_equivalent_inr,
            cache_savings_usd, pricing_status, fx_rate_applied, calculated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, fin_params)

        conn.commit()

        # Run benchmarks across query families
        families = ["summary", "timeline", "models", "projects", "sessions", "requests"]
        timings = {}
        for fam in families:
            t0 = time.perf_counter()
            res = execute_query(conn, {"contract_version": "1", "metric_id": fam, "include_archive": False})
            elapsed = time.perf_counter() - t0
            timings[fam] = elapsed
            self.assertEqual(res["contract_version"], "1")
            self.assertTrue(res["observed"] >= 1000 or res["eligible"] >= 1)

        conn.close()

        # Check performance: every query on 1k records must complete in under 100ms
        for fam, t_el in timings.items():
            self.assertLess(t_el, 0.10, f"Query family '{fam}' took {t_el:.4f}s, expected < 0.10s")


if __name__ == "__main__":
    unittest.main()
