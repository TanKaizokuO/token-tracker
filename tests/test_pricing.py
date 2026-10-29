"""Unit and integration tests for Token Ledger pricing engine and valuation.

Tests cover:
- Exact six display identities: Opus 5.5, Sonnet 5.5, Haiku 5.5, Sol 6.1, Luna Max, Gemini 3.8 Flash
- Explicit configurable provider/model-ID/route mapping with original IDs preserved; unknown remain unknown
- Effective dated rate import and query with date boundaries and no retroactive fallback
- Context thresholds and tiered pricing
- per-TTL cache pricing; unknown TTL priced at the 5m rate (status 'verified', savings computed); 'partial' only without a 5m rate
- Historical FX date/source/rate, with unknown FX returning None (not 0.0)
- Actual billed spend vs. API-equivalent value vs. subscription fee vs. cache savings separation
- Legacy pricing.json import as undated unverified data
- Full SQLite financial_facts persistence integration
"""

import json
import os
import sqlite3
import tempfile
import unittest

from token_ledger.pricing import (
    CANONICAL_DISPLAY_IDENTITIES,
    DEFAULT_ACCESS_ROUTE,
    DEFAULT_CURRENCY,
    GEMINI_3_8_FLASH,
    HAIKU_5_5,
    LUNA_MAX,
    OPUS_5_5,
    SOL_6_1,
    SONNET_5_5,
    STATUS_ESTIMATED,
    STATUS_PARTIAL,
    STATUS_UNPRICED,
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    HistoricalFx,
    ModelIdentityResult,
    ModelMappingRule,
    ModelRate,
    PricingVersion,
    ValuationResult,
    calculate_request_valuation,
    compute_pricing_hash,
    create_pricing_version,
    evaluate_usage_cost,
    find_effective_pricing_version,
    get_historical_fx,
    get_model_rate,
    get_pricing_version,
    import_legacy_pricing,
    persist_financial_facts,
    record_historical_fx,
    resolve_model_identity,
    resolve_rates_for_request,
)
from token_ledger.store.database import open_database


class TestModelIdentityAndMapping(unittest.TestCase):
    """Test model mapping rules, display names, and unknown preservation."""

    def test_six_exact_canonical_display_identities(self):
        """Verify the exact six display identities and spacing specified in plan all76 line 12."""
        expected = (
            "Opus 5.5",
            "Sonnet 5.5",
            "Haiku 5.5",
            "Sol 6.1",
            "Luna Max",
            "Gemini 3.8 Flash",
        )
        self.assertEqual(CANONICAL_DISPLAY_IDENTITIES, expected)
        self.assertEqual(OPUS_5_5, "Opus 5.5")
        self.assertEqual(SONNET_5_5, "Sonnet 5.5")
        self.assertEqual(HAIKU_5_5, "Haiku 5.5")
        self.assertEqual(SOL_6_1, "Sol 6.1")
        self.assertEqual(LUNA_MAX, "Luna Max")
        self.assertEqual(GEMINI_3_8_FLASH, "Gemini 3.8 Flash")

    def test_default_resolution_six_models(self):
        """Test default mapping resolves all 6 models from typical raw identifiers."""
        cases = [
            ("claude-opus-5-5", "anthropic", OPUS_5_5, "claude-opus-5-5"),
            ("claude-opus-5-5-20251001", "anthropic", OPUS_5_5, "claude-opus-5-5"),
            ("claude-sonnet-5-5-20251001", "anthropic", SONNET_5_5, "claude-sonnet-5-5"),
            ("claude-haiku-5-5-20251001", "anthropic", HAIKU_5_5, "claude-haiku-5-5"),
            ("gpt-6.1-sol-low", "openai", SOL_6_1, "gpt-6.1-sol"),
            ("gpt-6.1-sol", "openai", SOL_6_1, "gpt-6.1-sol"),
            ("gpt-6-luna", "openai", LUNA_MAX, "gpt-6-luna"),
            ("luna-max", "openai", LUNA_MAX, "gpt-6-luna"),
            ("gemini-3.8-flash", "google", GEMINI_3_8_FLASH, "gemini-3.8-flash"),
        ]
        for raw, provider, expected_display, expected_canonical in cases:
            res = resolve_model_identity(raw, provider=provider)
            self.assertTrue(res.is_known, f"Failed for {raw}")
            self.assertEqual(res.display_name, expected_display)
            self.assertEqual(res.raw_model, raw)
            self.assertEqual(res.canonical_model_id, expected_canonical)

    def test_unknown_models_remain_strictly_unknown(self):
        """Unknown or unconfigured models must preserve original ID and return display_name=None."""
        unknown_cases = [
            ("claude-haiku-4-5-20251001", "anthropic"),
            ("claude-opus-4-6", "anthropic"),
            ("gpt-4o", "openai"),
            ("custom-local-model", "local"),
            ("gemini-3.6-flash", "google"),
            ("glm-5.3", "openrouter"),
            ("<synthetic>", "unknown"),
            ("", "unknown"),
        ]
        for raw, prov in unknown_cases:
            res = resolve_model_identity(raw, provider=prov)
            self.assertFalse(res.is_known)
            self.assertIsNone(res.display_name)
            self.assertEqual(res.raw_model, raw.strip())
            self.assertIsNone(res.canonical_model_id)

    def test_configurable_custom_mapping_rules(self):
        """Users can supply custom mapping rules with route awareness."""
        custom_rules = [
            ModelMappingRule(
                pattern=r"^my-custom-opus$",
                display_name=OPUS_5_5,
                provider="anthropic",
                access_route="bedrock",
                canonical_model_id="claude-opus-5-5",
            ),
        ]
        # Route mismatch should not match
        res_default = resolve_model_identity("my-custom-opus", provider="anthropic", access_route="default", custom_rules=custom_rules)
        self.assertFalse(res_default.is_known)

        # Route match should resolve
        res_bedrock = resolve_model_identity("my-custom-opus", provider="anthropic", access_route="bedrock", custom_rules=custom_rules)
        self.assertTrue(res_bedrock.is_known)
        self.assertEqual(res_bedrock.display_name, OPUS_5_5)
        self.assertEqual(res_bedrock.access_route, "bedrock")


class TestEffectiveDatedRates(unittest.TestCase):
    """Test dated rate card schedules, boundaries, hashing, and retroactive fallback prevention."""

    def setUp(self):
        self.conn = open_database(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_pricing_hash_deterministic(self):
        payload_a = [{"provider": "anthropic", "in": 2.0, "out": 10.0}]
        payload_b = [{"in": 2.0, "out": 10.0, "provider": "anthropic"}]
        # JSON key sorting should produce identical digests
        self.assertEqual(compute_pricing_hash(payload_a), compute_pricing_hash(payload_b))

    def test_dated_rate_import_and_lookup(self):
        v1_rates = [
            ModelRate(
                version_id="sched_2026_q1",
                provider="anthropic",
                model_identity="claude-sonnet-5-5",
                input_rate_per_m=2.0,
                output_rate_per_m=10.0,
                cache_read_rate_per_m=0.2,
                cache_write_5m_rate_per_m=2.5,
                cache_write_1h_rate_per_m=4.0,
            )
        ]
        create_pricing_version(
            self.conn,
            version_id="sched_2026_q1",
            rates=v1_rates,
            effective_start="2026-01-01T00:00:00Z",
            effective_end="2026-07-01T00:00:00Z",
            source_provenance={"source": "Anthropic Official Q1 2026"},
        )

        v2_rates = [
            ModelRate(
                version_id="sched_2026_q3",
                provider="anthropic",
                model_identity="claude-sonnet-5-5",
                input_rate_per_m=1.8,
                output_rate_per_m=9.0,
                cache_read_rate_per_m=0.18,
                cache_write_5m_rate_per_m=2.25,
                cache_write_1h_rate_per_m=3.6,
            )
        ]
        create_pricing_version(
            self.conn,
            version_id="sched_2026_q3",
            rates=v2_rates,
            effective_start="2026-07-01T00:00:00Z",
            effective_end=None,  # ongoing
            source_provenance={"source": "Anthropic Official Q3 2026"},
        )

        # Date in Q1/Q2 resolves to sched_2026_q1
        pv1 = find_effective_pricing_version(self.conn, "2026-03-15T12:00:00Z")
        self.assertIsNotNone(pv1)
        self.assertEqual(pv1.version_id, "sched_2026_q1")
        rate1 = get_model_rate(self.conn, pv1.version_id, "anthropic", "claude-sonnet-5-5")
        self.assertEqual(rate1.input_rate_per_m, 2.0)

        # Date in Q3 resolves to sched_2026_q3
        pv2 = find_effective_pricing_version(self.conn, "2026-08-01T00:00:00Z")
        self.assertIsNotNone(pv2)
        self.assertEqual(pv2.version_id, "sched_2026_q3")
        rate2 = get_model_rate(self.conn, pv2.version_id, "anthropic", "claude-sonnet-5-5")
        self.assertEqual(rate2.input_rate_per_m, 1.8)

    def test_no_latest_retroactive_fallback(self):
        """CRITICAL: Queries prior to earliest effective_start or outside defined intervals must return None."""
        rates = [
            ModelRate(
                version_id="v_future",
                provider="anthropic",
                model_identity="claude-sonnet-5-5",
                input_rate_per_m=2.0,
                output_rate_per_m=10.0,
            )
        ]
        create_pricing_version(
            self.conn,
            version_id="v_future",
            rates=rates,
            effective_start="2026-06-01T00:00:00Z",
            effective_end="2026-12-31T23:59:59Z",
        )

        # Historical request from 2025 must NOT retroactively adopt v_future
        pv_past = find_effective_pricing_version(self.conn, "2025-10-01T00:00:00Z")
        self.assertIsNone(pv_past, "Must not retroactively apply rate to past before effective_start")

        # Request after effective_end must NOT adopt expired rate
        pv_expired = find_effective_pricing_version(self.conn, "2027-01-01T00:00:00Z")
        self.assertIsNone(pv_expired, "Must not apply expired rate after effective_end")

    def test_end_boundary_conditions(self):
        """Test exact boundary: effective_start is inclusive, effective_end is exclusive."""
        rates = [
            ModelRate(
                version_id="v_boundary",
                provider="google",
                model_identity="gemini-3.8-flash",
                input_rate_per_m=0.75,
                output_rate_per_m=3.75,
            )
        ]
        create_pricing_version(
            self.conn,
            version_id="v_boundary",
            rates=rates,
            effective_start="2026-01-01T00:00:00Z",
            effective_end="2026-02-01T00:00:00Z",
        )

        # Exact start is effective
        self.assertIsNotNone(find_effective_pricing_version(self.conn, "2026-01-01T00:00:00Z"))
        # Just before start is None
        self.assertIsNone(find_effective_pricing_version(self.conn, "2025-12-31T23:59:59Z"))
        # Just before end is effective
        self.assertIsNotNone(find_effective_pricing_version(self.conn, "2026-01-31T23:59:59Z"))
        # Exact end is exclusive -> None
        self.assertIsNone(find_effective_pricing_version(self.conn, "2026-02-01T00:00:00Z"))


class TestThresholdsAndPerTTL(unittest.TestCase):
    """Test context length thresholds and per-TTL cache pricing."""

    def test_tiered_rates_threshold(self):
        """Haiku 5.5 reprices when prompt tokens exceed prompt_threshold."""
        haiku_rate = ModelRate(
            version_id="v1",
            provider="anthropic",
            model_identity="claude-haiku-5-5",
            input_rate_per_m=0.1,
            output_rate_per_m=0.5,
            cache_read_rate_per_m=0.01,
            cache_write_5m_rate_per_m=0.125,
            cache_write_1h_rate_per_m=0.2,
            is_tiered=True,
            prompt_threshold=100_000,
            tiered_rates_json=json.dumps({
                "in": 0.5,
                "out": 2.5,
                "cr": 0.05,
                "cw5m": 0.625,
                "cw1h": 1.0,
            }),
        )

        # Standard context (80k prompt tokens)
        std_rates = resolve_rates_for_request(haiku_rate, prompt_tokens=80_000)
        self.assertEqual(std_rates["input"], 0.1)
        self.assertEqual(std_rates["output"], 0.5)
        self.assertEqual(std_rates["cache_read"], 0.01)

        # Long context (120k prompt tokens)
        long_rates = resolve_rates_for_request(haiku_rate, prompt_tokens=120_000)
        self.assertEqual(long_rates["input"], 0.5)
        self.assertEqual(long_rates["output"], 2.5)
        self.assertEqual(long_rates["cache_read"], 0.05)
        self.assertEqual(long_rates["cache_write_5m"], 0.625)
        self.assertEqual(long_rates["cache_write_1h"], 1.0)

    def test_per_ttl_cache_pricing_and_unknown_ttl(self):
        """Disjoint cache writes (5m and 1h) are priced; unknown TTL is priced at the 5m rate."""
        rate = ModelRate(
            version_id="v1",
            provider="anthropic",
            model_identity="claude-sonnet-5-5",
            input_rate_per_m=2.0,
            output_rate_per_m=10.0,
            cache_read_rate_per_m=0.2,
            cache_write_5m_rate_per_m=2.5,
            cache_write_1h_rate_per_m=4.0,
            status=STATUS_VERIFIED,
        )

        # Case A: Known TTLs only -> fully verified
        usage_known = {
            "fresh_input_tokens": 1000,
            "cache_read_tokens": 500,
            "cache_write_5m_tokens": 400,
            "cache_write_1h_tokens": 100,
            "cache_write_unknown_ttl_tokens": 0,
            "output_tokens": 200,
        }
        res_known = evaluate_usage_cost(usage_known, rate)
        self.assertEqual(res_known.pricing_status, STATUS_VERIFIED)
        # fresh: 1000 * 2.0 / 1M = 0.002
        # output: 200 * 10.0 / 1M = 0.002
        # cr: 500 * 0.2 / 1M = 0.0001
        # cw5m: 400 * 2.5 / 1M = 0.001
        # cw1h: 100 * 4.0 / 1M = 0.0004
        # total: 0.0055
        self.assertAlmostEqual(res_known.api_equivalent_usd, 0.0055, places=6)
        # Cache savings:
        # Hypothetical without cache for cached tokens (500 + 400 + 100 = 1000 tokens):
        # 1000 * 2.0 / 1M = 0.002
        # Actual cache cost: 0.0001 + 0.001 + 0.0004 = 0.0015
        # Savings: 0.002 - 0.0015 = 0.0005
        self.assertIsNotNone(res_known.cache_savings_usd)
        self.assertAlmostEqual(res_known.cache_savings_usd, 0.0005, places=6)

        # Case B: Unknown TTL tokens present -> priced at the 5m rate, fully verified, savings computed
        usage_unk = {
            "fresh_input_tokens": 1000,
            "cache_read_tokens": 500,
            "cache_write_unknown_ttl_tokens": 500,
            "output_tokens": 200,
        }
        res_unk = evaluate_usage_cost(usage_unk, rate)
        self.assertEqual(res_unk.pricing_status, STATUS_VERIFIED)
        self.assertNotIn("cache_write_unknown_ttl", res_unk.unpriced_counters)
        self.assertIsNotNone(res_unk.cache_savings_usd)

    def test_unknown_ttl_writes_priced_as_5m(self):
        """Unknown-TTL writes cost the same as the 5m bucket, with savings and non-partial status."""
        rate = ModelRate(
            version_id="v1",
            provider="anthropic",
            model_identity="claude-sonnet-5-5",
            input_rate_per_m=2.0,
            output_rate_per_m=10.0,
            cache_read_rate_per_m=0.2,
            cache_write_5m_rate_per_m=2.5,
            cache_write_1h_rate_per_m=4.0,
            status=STATUS_VERIFIED,
        )
        base = {"fresh_input_tokens": 1000, "cache_read_tokens": 500, "output_tokens": 200,
                "cache_write_1h_tokens": 0}
        as_unknown = evaluate_usage_cost(
            {**base, "cache_write_5m_tokens": 0, "cache_write_unknown_ttl_tokens": 400}, rate)
        as_5m = evaluate_usage_cost(
            {**base, "cache_write_5m_tokens": 400, "cache_write_unknown_ttl_tokens": 0}, rate)

        self.assertEqual(as_unknown.api_equivalent_usd, as_5m.api_equivalent_usd)
        self.assertAlmostEqual(as_unknown.cache_write_unknown_ttl_cost_usd, 0.001, places=9)
        self.assertEqual(as_unknown.pricing_status, STATUS_VERIFIED)
        self.assertEqual(as_unknown.unpriced_counters, [])
        self.assertIsNotNone(as_unknown.cache_savings_usd)
        self.assertEqual(as_unknown.cache_savings_usd, as_5m.cache_savings_usd)

        # A model with no 5m write rate keeps the old behavior: partial, savings None.
        no_5m = ModelRate(
            version_id="v1", provider="anthropic", model_identity="claude-sonnet-5-5",
            input_rate_per_m=2.0, output_rate_per_m=10.0, cache_read_rate_per_m=0.2,
            cache_write_5m_rate_per_m=None, status=STATUS_VERIFIED,
        )
        res_no_5m = evaluate_usage_cost(
            {**base, "cache_write_5m_tokens": 0, "cache_write_unknown_ttl_tokens": 400}, no_5m)
        self.assertEqual(res_no_5m.pricing_status, STATUS_PARTIAL)
        self.assertIn("cache_write_unknown_ttl", res_no_5m.unpriced_counters)
        self.assertIsNone(res_no_5m.cache_savings_usd)


class TestHistoricalFx(unittest.TestCase):
    """Test dated foreign exchange recording, resolution, and unknown handling."""

    def setUp(self):
        self.conn = open_database(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_record_and_query_fx(self):
        record_historical_fx(self.conn, "USD", "INR", 94.50, "2026-09-01", "RBI official")
        record_historical_fx(self.conn, "USD", "INR", 95.56, "2026-10-15", "RBI official")

        # Query on exact date
        self.assertEqual(get_historical_fx(self.conn, "2026-10-15"), 95.56)

        # Query on date between dates adopts closest prior effective date
        self.assertEqual(get_historical_fx(self.conn, "2026-10-20"), 95.56)
        self.assertEqual(get_historical_fx(self.conn, "2026-09-10"), 94.50)

        # USD to USD is always 1.0
        self.assertEqual(get_historical_fx(self.conn, "2026-10-15", "USD", "USD"), 1.0)

    def test_unknown_fx_returns_none_never_zero(self):
        """CRITICAL: Missing FX rate returns None, never 0.0."""
        # Querying date before any record
        self.assertIsNone(get_historical_fx(self.conn, "2025-01-01", "USD", "INR"))
        # Querying unsupported currency pair
        self.assertIsNone(get_historical_fx(self.conn, "2026-10-15", "USD", "JPY"))


class TestActualVsEquivalentSeparation(unittest.TestCase):
    """Test actual billed spend vs. API-equivalent value vs. subscription fees."""

    def test_separation_of_measures(self):
        rate = ModelRate(
            version_id="v1",
            provider="anthropic",
            model_identity="claude-sonnet-5-5",
            input_rate_per_m=2.0,
            output_rate_per_m=10.0,
            status=STATUS_VERIFIED,
        )
        usage = {
            "fresh_input_tokens": 1000,
            "output_tokens": 500,
        }
        # Given actual billed fact from OpenRouter/external invoice
        actual_billed = 0.0075
        actual_currency = "USD"
        sub_fee = 0.05

        val = evaluate_usage_cost(
            usage=usage,
            rate=rate,
            fx_rate=95.56,
            actual_billed_amount=actual_billed,
            actual_billed_currency=actual_currency,
            subscription_fee_usd=sub_fee,
            canonical_id="req_001",
        )

        # Actual billed fact is strictly distinct from API equivalent
        self.assertEqual(val.actual_billed_amount, 0.0075)
        self.assertEqual(val.actual_billed_currency, "USD")
        self.assertEqual(val.subscription_fee_usd, 0.05)
        # API equivalent = (1000 * 2.0 / 1M) + (500 * 10.0 / 1M) = 0.002 + 0.005 = 0.007
        self.assertAlmostEqual(val.api_equivalent_usd, 0.007, places=6)
        # API equivalent INR = 0.007 * 95.56 = 0.66892
        self.assertAlmostEqual(val.api_equivalent_inr, 0.66892, places=5)
        self.assertEqual(val.fx_rate_applied, 95.56)

    def test_unpriced_model_preserves_none_never_zero(self):
        """Unpriced model must result in api_equivalent_usd = None, never 0.0."""
        usage = {"fresh_input_tokens": 1000, "output_tokens": 200}
        val = evaluate_usage_cost(usage, rate=None, fx_rate=95.56)
        self.assertEqual(val.pricing_status, STATUS_UNPRICED)
        self.assertIsNone(val.api_equivalent_usd)
        self.assertIsNone(val.api_equivalent_inr)
        self.assertIsNone(val.fx_rate_applied)


class TestLegacyPricingImport(unittest.TestCase):
    """Test importing legacy pricing.json as undated, unverified baseline data."""

    def setUp(self):
        self.conn = open_database(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_import_legacy_pricing_doc(self):
        sample_doc = {
            "models": {
                "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cw5m": 5.0, "cw1h": 8.0, "cr": 0.2},
                "gemini-3.8-flash": {"in": 0.75, "out": 3.75, "cw5m": 0.9375, "cw1h": 1.5, "cr": 0.075},
            },
            "currency": {
                "code": "INR",
                "per_usd": 95.56,
            },
        }

        vid = import_legacy_pricing(self.conn, sample_doc, version_id="legacy_test")
        self.assertEqual(vid, "legacy_test")

        pv = get_pricing_version(self.conn, "legacy_test")
        self.assertIsNotNone(pv)
        prov = json.loads(pv.source_provenance_json)
        self.assertFalse(prov.get("verified"))

        # Model rate is imported as unverified
        rate = get_model_rate(self.conn, "legacy_test", "anthropic", "claude-opus-5-5")
        self.assertIsNotNone(rate)
        self.assertEqual(rate.status, STATUS_UNVERIFIED)
        self.assertEqual(rate.input_rate_per_m, 4.0)

        # Undated baseline FX was recorded
        fx = get_historical_fx(self.conn, "1970-01-01", "USD", "INR")
        self.assertEqual(fx, 95.56)


class TestFinancialFactsDatabaseIntegration(unittest.TestCase):
    """Test full database lifecycle with canonical requests, usage, and financial_facts table."""

    def setUp(self):
        self.conn = open_database(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_calculate_and_persist_financial_facts(self):
        # 1. Setup dated pricing version
        create_pricing_version(
            self.conn,
            version_id="pv_oct_2026",
            rates=[
                ModelRate(
                    version_id="pv_oct_2026",
                    provider="anthropic",
                    model_identity="claude-sonnet-5-5",
                    input_rate_per_m=2.0,
                    output_rate_per_m=10.0,
                    cache_read_rate_per_m=0.2,
                    cache_write_5m_rate_per_m=2.5,
                    cache_write_1h_rate_per_m=4.0,
                    status=STATUS_VERIFIED,
                )
            ],
            effective_start="2026-10-01T00:00:00Z",
            effective_end=None,
        )

        # 2. Setup historical FX
        record_historical_fx(self.conn, "USD", "INR", 95.50, "2026-10-25", "manual")

        # 3. Insert canonical request and normalized usage
        self.conn.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, access_route,
                raw_model, normalized_model, request_timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "req_integration_001",
                "anthropic",
                "dev_team",
                "claude-code",
                "default",
                "claude-sonnet-5-5-20251001",
                "claude-sonnet-5-5",
                "2026-10-25T14:30:00Z",
                "2026-10-25T14:30:00Z",
            ),
        )
        self.conn.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens,
                cache_write_5m_tokens, cache_write_1h_tokens,
                cache_write_unknown_ttl_tokens, output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("req_integration_001", 5000, 2000, 1000, 0, 0, 800),
        )

        # 4. Calculate request valuation
        val = calculate_request_valuation(self.conn, "req_integration_001")
        self.assertEqual(val.pricing_version_id, "pv_oct_2026")
        self.assertEqual(val.pricing_status, STATUS_VERIFIED)
        # fresh: 5000 * 2.0 / 1M = 0.010
        # cr: 2000 * 0.2 / 1M = 0.0004
        # cw5m: 1000 * 2.5 / 1M = 0.0025
        # out: 800 * 10.0 / 1M = 0.0080
        # total: 0.0209 USD
        self.assertAlmostEqual(val.api_equivalent_usd, 0.0209, places=6)
        # inr: 0.0209 * 95.50 = 1.99595 INR
        self.assertAlmostEqual(val.api_equivalent_inr, 1.99595, places=5)
        self.assertEqual(val.fx_rate_applied, 95.50)

        # 5. Persist to financial_facts
        persist_financial_facts(self.conn, val)

        # 6. Verify database record in financial_facts
        row = self.conn.execute(
            """
            SELECT canonical_id, pricing_version_id, api_equivalent_usd, api_equivalent_inr,
                   pricing_status, fx_rate_applied
            FROM financial_facts
            WHERE canonical_id = ?
            """,
            ("req_integration_001",),
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "req_integration_001")
        self.assertEqual(row[1], "pv_oct_2026")
        self.assertAlmostEqual(row[2], 0.0209, places=6)
        self.assertAlmostEqual(row[3], 1.99595, places=5)
        self.assertEqual(row[4], STATUS_VERIFIED)
        self.assertEqual(row[5], 95.50)


if __name__ == "__main__":
    unittest.main()
