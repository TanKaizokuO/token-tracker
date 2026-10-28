"""Unit and integration tests for Provider, Quota, Burn-Rate, and Budget Analytics (WP16).

Verifies:
- Provider, account_alias, access_route, and model identity separation.
- Financial separation: actual billed, API equivalent USD/INR, cache savings, unpriced.
- Historical FX selection explicit (date and rate).
- Quota snapshot import and validation (official source provenance).
- Quota unavailable/unsupported status (never infers percentage from local counts).
- Stale quota detection when reset_at has passed.
- Burn rate with declared interval, partial current bucket, and sparse baseline abstention.
- Live active sessions with last_observed_time separate from heuristic active status.
- Budget threshold evaluation (tokens, cost, project/model/session) with local notifications.
- Synthetic database smoke test exercising all 4 families against populated SQLite DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

from token_ledger.query import execute_query

from token_ledger.metrics.providers import (
    analyze,
    delete_budget,
    import_quota_snapshot,
    list_budgets,
    save_budget,
    validate_quota_snapshot,
)
from token_ledger.pricing import record_historical_fx
from token_ledger.store.database import open_database


class TestProviderMetricsUnit(unittest.TestCase):
    """Unit tests for quota validation and budget configuration persistence."""

    def test_validate_quota_snapshot_valid(self):
        snapshot = {
            "provider": "anthropic",
            "account_alias": "prod_team",
            "access_route": "direct_api",
            "official_source": "anthropic-ratelimit-headers",
            "window_type": "minute",
            "limit_value": 100000.0,
            "used_value": 25000.0,
            "remaining_value": 75000.0,
            "reset_at": "2026-10-26T12:01:00Z",
            "units": "tokens",
            "status": "available",
            "percent_used": 25.0,
        }
        val = validate_quota_snapshot(snapshot)
        self.assertEqual(val["provider"], "anthropic")
        self.assertEqual(val["account_alias"], "prod_team")
        self.assertEqual(val["access_route"], "direct_api")
        self.assertEqual(val["window_type"], "minute")
        self.assertEqual(val["status"], "available")
        self.assertEqual(val["provenance"]["official_source"], "anthropic-ratelimit-headers")
        self.assertEqual(val["provenance"]["percent_used"], 25.0)

    def test_validate_quota_snapshot_requires_official_source(self):
        with self.assertRaises(ValueError) as ctx:
            validate_quota_snapshot({
                "provider": "anthropic",
                "limit_value": 1000,
            })
        self.assertIn("official_source", str(ctx.exception))

    def test_validate_quota_snapshot_requires_provider(self):
        with self.assertRaises(ValueError) as ctx:
            validate_quota_snapshot({
                "official_source": "headers",
                "limit_value": 1000,
            })
        self.assertIn("provider", str(ctx.exception))

    def test_validate_quota_snapshot_negative_bounds(self):
        with self.assertRaises(ValueError):
            validate_quota_snapshot({
                "provider": "anthropic",
                "official_source": "headers",
                "limit_value": -100.0,
            })

    def test_budget_persistence_and_deletion(self):
        conn = open_database(":memory:")
        budget_cfg = {
            "name": "Team Daily Limit",
            "period": "daily",
            "target_type": "tokens",
            "threshold": 500000.0,
            "notifications_enabled": True,
            "scope": {"project_id": "proj_omega"},
        }
        saved = save_budget(conn, budget_cfg)
        self.assertIn("id", saved)
        self.assertEqual(saved["name"], "Team Daily Limit")
        self.assertTrue(saved["notifications_enabled"])

        # List budgets
        budgets = list_budgets(conn)
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0]["threshold"], 500000.0)

        # Update budget
        budget_cfg["id"] = saved["id"]
        budget_cfg["threshold"] = 750000.0
        updated = save_budget(conn, budget_cfg)
        self.assertEqual(updated["threshold"], 750000.0)

        # Delete budget
        deleted = delete_budget(conn, saved["id"])
        self.assertTrue(deleted)
        self.assertEqual(len(list_budgets(conn)), 0)

class TestQuotaWallClock(unittest.TestCase):
    def test_quota_expiry_uses_current_clock_with_old_or_empty_requests(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 28, 12, tzinfo=timezone.utc).astimezone(tz)

        conn = open_database(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """INSERT INTO canonical_requests
               (canonical_id, provider, origin_harness, raw_model, normalized_model,
                request_timestamp, created_at)
               VALUES ('old', 'synthetic', 'omp', 'synthetic', 'synthetic',
                       '2026-10-20T10:00:00Z', '2026-10-20T10:00:00Z')"""
        )
        for provider, reset in (
            ("expired", "2026-10-27T12:00:00Z"),
            ("at_reset", "2026-10-28T12:00:00Z"),
            ("new", "2026-10-29T12:00:00Z"),
        ):
            import_quota_snapshot(conn, {
                "provider": provider, "official_source": "synthetic-official",
                "reset_at": reset, "status": "available", "percent_used": 25,
                "measured_at": "2026-10-20T10:00:00Z",
            })
        with patch("token_ledger.metrics.providers.datetime", Clock):
            for filters, count in (({}, 1), ({"provider": ["expired", "at_reset", "new"]}, 0)):
                with self.subTest(filters=filters):
                    result = execute_query(conn, {
                        "contract_version": "1", "metric_id": "providers_breakdown",
                        "filters": filters,
                    })
                    self.assertEqual(result["eligible"]["total_requests"], count)
                    cards = {c["provider"]: c for c in result["values"]["quotas"]}
                    for provider in ("expired", "at_reset"):
                        self.assertEqual(cards[provider]["status"], "stale")
                        self.assertFalse(cards[provider]["is_available"])
                        self.assertIsNone(cards[provider]["percent_used"])
                    self.assertEqual(cards["new"]["status"], "available")
                    self.assertTrue(cards["new"]["is_available"])
                    self.assertEqual(cards["new"]["percent_used"], 25)



class TestProviderMetricsSyntheticDB(unittest.TestCase):
    """Integration and synthetic DB smoke tests for all 4 families."""

    def setUp(self):
        self.conn = open_database(":memory:")
        self._populate_synthetic_data()

    def _populate_synthetic_data(self):
        cur = self.conn.cursor()

        # 1. Historical FX
        record_historical_fx(
            self.conn,
            base_currency="USD",
            target_currency="INR",
            rate_per_usd=85.0,
            effective_date="2026-10-25",
            source_provenance="synthetic_test",
        )

        # 2. Projects & Sessions
        now = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
        cur.execute(
            "INSERT INTO projects (project_id, project_name, account_alias, created_at) VALUES (?, ?, ?, ?)",
            ("proj_alpha", "Project Alpha", "default", now.isoformat()),
        )
        cur.execute(
            "INSERT INTO projects (project_id, project_name, account_alias, created_at) VALUES (?, ?, ?, ?)",
            ("proj_beta", "Project Beta", "enterprise", now.isoformat()),
        )

        # Session 1: Active session (recent)
        cur.execute(
            """
            INSERT INTO sessions (session_id, project_id, account_alias, origin_harness, started_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("sess_live_1", "proj_alpha", "default", "claude-code", (now - timedelta(minutes=10)).isoformat(), now.isoformat()),
        )
        # Session 2: Idle session (2 hours ago)
        cur.execute(
            """
            INSERT INTO sessions (session_id, project_id, account_alias, origin_harness, started_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("sess_idle_2", "proj_beta", "enterprise", "omp", (now - timedelta(hours=2)).isoformat(), now.isoformat()),
        )

        # 3. Canonical Requests, Usage, and Financial Facts
        # Request 1: Direct Anthropic, sess_live_1, 5 minutes ago, priced, with actual billed
        ts1 = (now - timedelta(minutes=5)).isoformat()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, request_timestamp, provider, account_alias, access_route,
                origin_harness, raw_model, normalized_model, session_id, project_id, request_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_1", ts1, "anthropic", "default", "direct_api", "claude-code", "claude-3-5-sonnet-20241022", "claude-3-5-sonnet", "sess_live_1", "proj_alpha", "completed", ts1),
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_1", 1000, 2000, 500, 0, 0, 800, 200, 4300),
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr,
                actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_1", "priced", 0.025, 2.125, 0.024, 0.005, ts1),
        )

        # Request 2: Bedrock route with same model claude-3-5-sonnet, sess_live_1, 2 minutes ago
        ts2 = (now - timedelta(minutes=2)).isoformat()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, request_timestamp, provider, account_alias, access_route,
                origin_harness, raw_model, normalized_model, session_id, project_id, request_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_2", ts2, "bedrock", "default", "bedrock_cross_region", "claude-code", "anthropic.claude-3-5-sonnet", "claude-3-5-sonnet", "sess_live_1", "proj_alpha", "completed", ts2),
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_2", 2000, 0, 0, 0, 0, 500, 0, 2500),
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr,
                actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_2", "priced", 0.015, 1.275, None, 0.0, ts2),
        )

        # Request 3: Unpriced OpenAI request, sess_idle_2, 2 hours ago
        ts3 = (now - timedelta(hours=2)).isoformat()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, request_timestamp, provider, account_alias, access_route,
                origin_harness, raw_model, normalized_model, session_id, project_id, request_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_3", ts3, "openai", "enterprise", "proxy", "omp", "custom-ft-model", "custom-ft-model", "sess_idle_2", "proj_beta", "completed", ts3),
        )
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens,
                reasoning_tokens, reported_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_3", 500, 0, 0, 0, 0, 100, 0, 600),
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr,
                actual_billed_amount, cache_savings_usd, calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_3", "unpriced", None, None, None, None, ts3),
        )

        # 4. Quota snapshots
        # Quota 1: Anthropic direct_api official valid snapshot
        import_quota_snapshot(self.conn, {
            "provider": "anthropic",
            "account_alias": "default",
            "access_route": "direct_api",
            "official_source": "anthropic-ratelimit-headers",
            "window_type": "day",
            "limit_value": 1000000.0,
            "used_value": 250000.0,
            "remaining_value": 750000.0,
            "reset_at": (now + timedelta(hours=12)).isoformat(),
            "units": "tokens",
            "status": "available",
            "percent_used": 25.0,
        })

        # Quota 2: Bedrock unavailable / unsupported quota snapshot
        import_quota_snapshot(self.conn, {
            "provider": "bedrock",
            "account_alias": "default",
            "access_route": "bedrock_cross_region",
            "official_source": "aws-cloudwatch",
            "window_type": "month",
            "limit_value": None,
            "used_value": None,
            "remaining_value": None,
            "reset_at": None,
            "units": "tokens",
            "status": "unavailable",
            "percent_used": None,
        })

        # Quota 3: Stale quota snapshot (reset_at in the past)
        import_quota_snapshot(self.conn, {
            "provider": "openai",
            "account_alias": "enterprise",
            "access_route": "proxy",
            "official_source": "openai-usage-api",
            "window_type": "day",
            "limit_value": 50000.0,
            "used_value": 40000.0,
            "remaining_value": 10000.0,
            "reset_at": (now - timedelta(hours=1)).isoformat(),
            "units": "tokens",
            "status": "available",
            "percent_used": 80.0,
        })

        # 5. Budgets
        save_budget(self.conn, {
            "id": "b_daily_tokens",
            "name": "Daily Token Budget",
            "period": "daily",
            "target_type": "tokens",
            "threshold": 10000.0,
            "notifications_enabled": True,
        })
        save_budget(self.conn, {
            "id": "b_project_cost",
            "name": "Alpha Project Cost Limit",
            "period": "monthly",
            "target_type": "cost_usd",
            "threshold": 0.03,  # 0.04 observed -> breached
            "scope": {"project_id": "proj_alpha"},
            "notifications_enabled": True,
        })

    def test_providers_family_breakdown_and_separation(self):
        """Tests provider/route/model breakdowns and financial separations."""
        env = {
            "contract_version": "1",
            "harness_scope": "combined",
            "family": "providers",
        }
        res = analyze(self.conn, env)
        self.assertEqual(res["contract_version"], "1")
        self.assertEqual(res["family"], "providers")

        rows = res["rows"]
        # Verify provider and route are separate, and model is distinct
        self.assertGreaterEqual(len(rows), 3)

        anthropic_row = next(r for r in rows if r["provider"] == "anthropic" and r["access_route"] == "direct_api")
        bedrock_row = next(r for r in rows if r["provider"] == "bedrock" and r["access_route"] == "bedrock_cross_region")

        # Same model claude-3-5-sonnet present in both routes without merging identities
        self.assertIn("claude-3-5-sonnet", anthropic_row["models"])
        self.assertIn("claude-3-5-sonnet", bedrock_row["models"])
        self.assertEqual(anthropic_row["access_route"], "direct_api")
        self.assertEqual(bedrock_row["access_route"], "bedrock_cross_region")

        # Financial measure separation
        fin = res["values"]["financial_summary"]
        self.assertEqual(fin["actual_api_spend_usd"], 0.024)
        self.assertEqual(fin["api_equivalent_usd"], 0.04)  # 0.025 + 0.015
        self.assertEqual(fin["cache_savings_usd"], 0.005)
        self.assertEqual(fin["unpriced_request_count"], 1)
        self.assertEqual(fin["priced_request_count"], 2)
        self.assertEqual(fin["fx_rate_usd_to_inr"], 85.0)

    def test_quota_cards_and_unavailable_guarantees(self):
        """Tests official quota cards, stale windows, and no local inference."""
        env = {
            "contract_version": "1",
            "family": "providers",
        }
        with patch("token_ledger.metrics.providers.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
            res = analyze(self.conn, env)
        quotas = res["values"]["quotas"]

        anthropic_q = next(q for q in quotas if q["provider"] == "anthropic" and q["access_route"] == "direct_api")
        self.assertTrue(anthropic_q["is_available"])
        self.assertEqual(anthropic_q["percent_used"], 25.0)
        self.assertEqual(anthropic_q["remaining_tokens"], 750000.0)

        # Bedrock quota was unavailable -> must report is_available=False, percent_used=None
        # Never guess from local counts!
        bedrock_q = next(q for q in quotas if q["provider"] == "bedrock")
        self.assertFalse(bedrock_q["is_available"])
        self.assertIsNone(bedrock_q["percent_used"])

        # OpenAI quota was stale (reset_at in the past) -> must report is_available=False, status='stale'
        openai_q = next(q for q in quotas if q["provider"] == "openai")
        self.assertFalse(openai_q["is_available"])
        self.assertEqual(openai_q["status"], "stale")

    def test_burn_rate_family(self):
        """Tests burn rate with declared interval and baseline change."""
        env = {
            "contract_version": "1",
            "family": "burn",
            "interval_minutes": 15,
        }
        res = analyze(self.conn, env)
        self.assertEqual(res["family"], "burn")
        val = res["values"]

        self.assertEqual(val["declared_interval_minutes"], 15)
        self.assertGreater(val["current_burn_rate_tpm"], 0)
        self.assertGreater(val["current_burn_rate_cost_per_hour"], 0)

        # Baseline window was sparse (0 requests in baseline 15m window)
        # Guarantees: sparse baseline abstains, returns None, never 0.0%
        self.assertIsNone(val["typical_burn_rate_tpm"])
        self.assertIsNone(val["baseline_change_pct"])

        # High burn sessions
        self.assertGreater(len(val["high_burn_sessions"]), 0)
        top_sess = val["high_burn_sessions"][0]
        self.assertEqual(top_sess["session_id"], "sess_live_1")

    def test_live_sessions_family(self):
        """Tests live active-session monitoring and heuristic separation."""
        env = {
            "contract_version": "1",
            "family": "live",
            "heuristic_window_minutes": 30,
        }
        res = analyze(self.conn, env)
        self.assertEqual(res["family"], "live")

        sessions = res["values"]["active_sessions"]
        self.assertEqual(len(sessions), 2)

        live_sess = next(s for s in sessions if s["session_id"] == "sess_live_1")
        idle_sess = next(s for s in sessions if s["session_id"] == "sess_idle_2")

        # live_sess last observed was ~2 mins ago -> is_heuristic_active is True
        self.assertTrue(live_sess["is_heuristic_active"])
        self.assertEqual(live_sess["status"], "active")
        self.assertIsNotNone(live_sess["last_observed_time"])

        # idle_sess last observed was ~2 hours ago -> is_heuristic_active is False
        self.assertFalse(idle_sess["is_heuristic_active"])
        self.assertEqual(idle_sess["status"], "idle")

    def test_budgets_evaluation_and_notifications(self):
        """Tests budget threshold evaluations, warning/breached statuses, and local events."""
        env = {
            "contract_version": "1",
            "family": "budgets",
        }
        res = analyze(self.conn, env)
        self.assertEqual(res["family"], "budgets")

        budgets = res["values"]["budgets"]
        self.assertEqual(len(budgets), 2)

        # Token budget: 7400 total tokens against 10000 threshold -> 74.0% consumed -> "ok"
        token_b = next(b for b in budgets if b["id"] == "b_daily_tokens")
        self.assertEqual(token_b["status"], "ok")
        self.assertEqual(token_b["percent_consumed"], 74.0)

        # Cost budget: proj_alpha cost is 0.04 vs 0.03 threshold -> 133.3% consumed -> "breached"
        cost_b = next(b for b in budgets if b["id"] == "b_project_cost")
        self.assertEqual(cost_b["status"], "breached")
        self.assertGreater(cost_b["percent_consumed"], 100.0)

        # Local notifications generated strictly locally without network
        notifs = res["values"]["local_notifications"]
        self.assertGreaterEqual(len(notifs), 1)
        breached_notif = next(n for n in notifs if n["budget_id"] == "b_project_cost")
        self.assertEqual(breached_notif["delivery_mode"], "local_event_only_no_network")
        self.assertEqual(breached_notif["status"], "breached")


if __name__ == "__main__":
    unittest.main()
