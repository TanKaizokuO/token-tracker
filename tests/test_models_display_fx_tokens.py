"""Regression tests for the Models page: INR cost, display-identity mapping, and tokens/request.

a. A priced row stored with status "verified" (the status the pricing engine writes) must count as
   priced, so the model profile carries API-equivalent USD and the API-computed INR.
b. Raw ids seen in the real ledger (gpt-6.1-sol and gpt-6-luna via openai-codex, gemini-3.8-flash via
   google-antigravity) must map to Sol 6.1, Luna 6 and Gemini 3.8 Flash.
c. The cohort "tokens_per_request" metric must fall back to the provider-reported total when the
   complete-counter total is unavailable (cache-write buckets missing), not report n=0.
"""

from __future__ import annotations

import unittest
from typing import Optional

from token_ledger.metrics.models import PRICED_STATUSES, analyze, observed_total_tokens
from token_ledger.pricing import (
    GEMINI_3_8_FLASH,
    LUNA_MAX,
    OPUS_5_5,
    SOL_6_1,
    resolve_model_identity,
)
from token_ledger.store.database import open_database

ENVELOPE = {"contract_version": "1", "harness_scope": "combined", "metric_id": "model_profiles"}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = open_database(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def add_request(
        self,
        canonical_id: str,
        provider: str,
        raw_model: str,
        normalized_model: str,
        *,
        status: str = "verified",
        usd: Optional[float] = None,
        inr: Optional[float] = None,
        counters: Optional[dict] = None,
        reported_total: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id, origin_harness,
                access_route, raw_model, normalized_model, session_id, project_id,
                request_status, request_timestamp, timing_basis, created_at
            ) VALUES (?, ?, 'default', ?, 'omp', 'default', ?, ?, ?, NULL,
                      'ok', '2026-10-10T10:00:00Z', 'none', '2026-10-10T10:00:00Z')
            """,
            (canonical_id, provider, canonical_id, raw_model, normalized_model, session_id),
        )
        values = {
            "fresh_input_tokens": 0, "cache_read_tokens": 0, "cache_write_5m_tokens": None,
            "cache_write_1h_tokens": None, "cache_write_unknown_ttl_tokens": None,
            "output_tokens": 0, "reasoning_tokens": None, "reported_total_tokens": reported_total,
        }
        values.update(counters or {})
        cur.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
                cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens, reasoning_tokens,
                reported_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canonical_id, values["fresh_input_tokens"], values["cache_read_tokens"],
             values["cache_write_5m_tokens"], values["cache_write_1h_tokens"],
             values["cache_write_unknown_ttl_tokens"], values["output_tokens"],
             values["reasoning_tokens"], values["reported_total_tokens"]),
        )
        cur.execute(
            """
            INSERT INTO financial_facts (
                canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, calculated_at
            ) VALUES (?, ?, ?, ?, '2026-10-10T10:00:00Z')
            """,
            (canonical_id, status, usd, inr),
        )
        self.conn.commit()


class TestCostAndInrStatus(_Base):
    """a. Cost and INR are counted for every status the engine treats as priced."""

    def test_verified_status_is_priced_and_carries_api_inr(self) -> None:
        self.assertIn("verified", PRICED_STATUSES)
        self.add_request("req-v1", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         status="verified", usd=0.5, inr=47.78, counters={"output_tokens": 10})
        self.add_request("req-v2", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         status="verified", usd=0.25, inr=23.89, counters={"output_tokens": 10})
        opus = analyze(self.conn, ENVELOPE)["profiles"][OPUS_5_5]

        self.assertAlmostEqual(opus["cost"]["api_equivalent_usd"], 0.75)
        self.assertAlmostEqual(opus["cost"]["api_equivalent_inr"], 71.67, places=2)
        self.assertEqual(opus["cost"]["inr_requests_count"], 2)

    def test_unpriced_status_stays_out_of_cost(self) -> None:
        self.add_request("req-u1", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         status="unpriced", usd=None, inr=None, counters={"output_tokens": 10})
        opus = analyze(self.conn, ENVELOPE)["profiles"][OPUS_5_5]

        self.assertIsNone(opus["cost"]["api_equivalent_usd"])
        self.assertIsNone(opus["cost"]["api_equivalent_inr"])

    def test_priced_status_still_counts(self) -> None:
        self.add_request("req-p1", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         status="priced", usd=1.0, inr=95.56, counters={"output_tokens": 10})
        opus = analyze(self.conn, ENVELOPE)["profiles"][OPUS_5_5]

        self.assertAlmostEqual(opus["cost"]["api_equivalent_usd"], 1.0)
        self.assertAlmostEqual(opus["cost"]["api_equivalent_inr"], 95.56)


class TestDisplayIdentityMapping(_Base):
    """b. Raw ids as stored in the ledger map to the six canonical identities."""

    def test_observed_provider_routes_map_to_canonical_identities(self) -> None:
        cases = [
            ("openai-codex", "gpt-6.1-sol", SOL_6_1),
            ("openai-codex", "gpt-6-luna", LUNA_MAX),
            ("google-antigravity", "gemini-3.8-flash", GEMINI_3_8_FLASH),
            ("google-antigravity", "gemini-3.8-flash-high", GEMINI_3_8_FLASH),
            ("google-antigravity", "gemini-3.8-flash-medium", GEMINI_3_8_FLASH),
            ("openai", "gpt-6.1-sol", SOL_6_1),
            ("google", "gemini-3.8-flash", GEMINI_3_8_FLASH),
            ("openai-codex", "luna-max", LUNA_MAX),
        ]
        for provider, raw, expected in cases:
            with self.subTest(provider=provider, raw=raw):
                self.assertEqual(resolve_model_identity(raw, provider).display_name, expected)

    def test_unrelated_models_stay_unmapped(self) -> None:
        # gpt-6.1-terra is a different OpenAI model; openrouter routes are not mapped by provider family.
        self.assertIsNone(resolve_model_identity("gpt-6.1-terra", "openai-codex").display_name)
        self.assertIsNone(resolve_model_identity("gemini-3.1-pro", "google-antigravity").display_name)
        self.assertIsNone(resolve_model_identity("gpt-6-luna", "openrouter").display_name)

    def test_profiles_count_requests_under_observed_provider_routes(self) -> None:
        self.add_request("req-s1", "openai-codex", "gpt-6.1-sol", "gpt-6.1-sol", counters={"output_tokens": 5})
        self.add_request("req-l1", "openai-codex", "gpt-6-luna", "gpt-6-luna", counters={"output_tokens": 5})
        self.add_request("req-g1", "google-antigravity", "gemini-3.8-flash-high", "gemini-3.8-flash",
                         counters={"output_tokens": 5})
        profiles = analyze(self.conn, ENVELOPE)["profiles"]

        self.assertEqual(profiles[SOL_6_1]["requests_count"], 1)
        self.assertEqual(profiles[LUNA_MAX]["requests_count"], 1)
        self.assertEqual(profiles[GEMINI_3_8_FLASH]["requests_count"], 1)


class TestTokensPerRequest(_Base):
    """c. The per-request token average uses the same fallback order as the providers metric."""

    def test_reported_total_used_when_complete_counter_total_missing(self) -> None:
        # Cache-write buckets are NULL, so the complete-counter total is unavailable; the reported total is observed.
        self.add_request("req-r1", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         counters={"fresh_input_tokens": 100, "cache_read_tokens": 50, "output_tokens": 10,
                                   "reported_total_tokens": 160}, reported_total=160)
        res = analyze(self.conn, ENVELOPE)
        opus = next(m for m in res["comparison"]["models_compared"] if m["model_identity"] == OPUS_5_5)

        self.assertEqual(opus["metrics"]["tokens_per_request"]["sample_count"], 1)
        self.assertEqual(opus["metrics"]["tokens_per_request"]["missing_count"], 0)
        self.assertEqual(opus["tokens_per_request"], 160.0)

    def test_observed_counter_sum_used_without_reported_total(self) -> None:
        self.add_request("req-c1", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         counters={"fresh_input_tokens": 30, "output_tokens": 20})
        res = analyze(self.conn, ENVELOPE)
        opus = next(m for m in res["comparison"]["models_compared"] if m["model_identity"] == OPUS_5_5)

        self.assertEqual(opus["metrics"]["tokens_per_request"]["value"], 50.0)

    def test_complete_counters_still_take_precedence(self) -> None:
        self.add_request("req-f1", "anthropic", "claude-opus-5-5", "claude-opus-5-5",
                         counters={"fresh_input_tokens": 100, "cache_read_tokens": 0,
                                   "cache_write_5m_tokens": 5, "cache_write_1h_tokens": 0,
                                   "cache_write_unknown_ttl_tokens": 0, "output_tokens": 10,
                                   "reported_total_tokens": 999}, reported_total=999)
        opus = analyze(self.conn, ENVELOPE)["profiles"][OPUS_5_5]

        self.assertEqual(opus["tokens"]["total_tokens"], 115)

    def test_observed_total_tokens_helper_returns_none_without_any_counter(self) -> None:
        self.assertIsNone(observed_total_tokens({"total_tokens": None, "reported_total_tokens": None,
                                                 "fresh_input_tokens": None, "output_tokens": None}))


if __name__ == "__main__":
    unittest.main()
