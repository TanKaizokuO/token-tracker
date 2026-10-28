"""Substantive regression tests for Token Ledger normalization and identity.

Verifies:
1. Repeat observations linking to a single canonical request without duplication.
2. Provider and account-scoped request identity preventing collision across accounts.
3. Distinct retry requests with new IDs and parent links remaining distinct.
4. Unknown TTL cache write preservation (unknownTTL not 5m).
5. Missing vs. zero token counters preservation (strict None vs 0).
6. Reasoning tokens as a reported non-additive subset of output tokens.
7. Reported total tokens preserved as reported, never synthesized.
8. Raw timing basis, duration_ms, ttft_ms, and generation speed TPS rules.
9. Fallback identity when provider request ID is absent.
10. Cross-source candidate overlap resolution with unknown account/provider safety.
11. Full database insertion round-trip with canonical SQLite schema.
12. Direct native parsing of synthetic baseline fixtures (claude-baseline & omp-baseline).
"""

import json
import os
import sqlite3
import unittest

from token_ledger.ingest.identity import (
    CanonicalIdentity,
    CrossSourceCandidate,
    build_canonical_id,
    build_fallback_identity,
    create_canonical_identity,
    evaluate_cross_source_identity,
    normalize_model_name,
)
from token_ledger.ingest.normalize import (
    NormalizedObservationRecord,
    normalize_claude_record,
    normalize_observation,
    normalize_omp_record,
    parse_timestamp,
)
from token_ledger.store.database import open_database


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
CLAUDE_FIXTURE = os.path.join(FIXTURES_DIR, "claude-baseline.jsonl")
OMP_FIXTURE = os.path.join(FIXTURES_DIR, "omp-baseline.jsonl")


class TestIdentityScoping(unittest.TestCase):
    """Verify canonical identity derivation and scoping rules."""

    def test_canonical_id_provider_account_scoping(self):
        cid = build_canonical_id("anthropic", "acct_dev_1", "req_123")
        self.assertEqual(cid, "anthropic:acct_dev_1:req_123")

        # Case normalization on provider
        cid_upper = build_canonical_id("ANTHROPIC", "acct_dev_1", "req_123")
        self.assertEqual(cid_upper, "anthropic:acct_dev_1:req_123")

    def test_distinct_accounts_same_request_id(self):
        # Two records with same provider and request ID in different accounts must not collide
        cid_1 = build_canonical_id("anthropic", "acct_dev_1", "req_shared_001")
        cid_2 = build_canonical_id("anthropic", "acct_team_2", "req_shared_001")
        self.assertNotEqual(cid_1, cid_2)
        self.assertEqual(cid_1, "anthropic:acct_dev_1:req_shared_001")
        self.assertEqual(cid_2, "anthropic:acct_team_2:req_shared_001")

    def test_fallback_identity_generation(self):
        fallback = build_fallback_identity(
            source_adapter="claude_code",
            file_locator="session_01.jsonl",
            line_index=42,
            event_id="msg_001",
            file_generation="gen1",
        )
        self.assertEqual(fallback, "claude_code:session_01.jsonl:gen1:42:msg_001")

        cid = build_canonical_id("anthropic", "acct_dev_1", None, fallback_identity=fallback)
        self.assertTrue(cid.startswith("fallback:"))
        self.assertIn("claude_code:session_01.jsonl:gen1:42:msg_001", cid)

    def test_model_normalization(self):
        # Date suffix stripping
        self.assertEqual(normalize_model_name("claude-sonnet-5-5-20251001"), "claude-sonnet-5-5")
        self.assertEqual(normalize_model_name("claude-haiku-4-5-20251001"), "claude-haiku-4-5")
        # Effort tier stripping
        self.assertEqual(normalize_model_name("gpt-6.1-sol-low"), "gpt-6.1-sol")
        self.assertEqual(normalize_model_name("gpt-6.1-sol-medium"), "gpt-6.1-sol")
        self.assertEqual(normalize_model_name("gpt-6.1-sol-high"), "gpt-6.1-sol")
        # Unaltered standard models
        self.assertEqual(normalize_model_name("gemini-3.8-flash"), "gemini-3.8-flash")
        # Synthetic / unknown models
        self.assertEqual(normalize_model_name("<synthetic>"), "unknown")
        self.assertEqual(normalize_model_name("unknown"), "unknown")
        self.assertEqual(normalize_model_name(None), "unknown")


class TestCrossSourceCandidateOverlap(unittest.TestCase):
    """Verify cross-source candidate overlap evaluation and safety rules."""

    def test_unknown_provider_cannot_establish_cross_source_identity(self):
        cand_a = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="unknown",
            account_alias="acct_dev_1",
            provider_request_id="req_candidate_1",
        )
        cand_b = CrossSourceCandidate(
            origin_harness="oh-my-pi",
            source_adapter="omp",
            provider="unknown",
            account_alias="acct_dev_1",
            provider_request_id="req_candidate_1",
        )
        is_same, is_overlap, reason = evaluate_cross_source_identity(cand_a, cand_b)
        self.assertFalse(is_same)
        self.assertTrue(is_overlap)
        self.assertEqual(reason, "unknown_provider_cannot_establish_cross_source_identity")

    def test_unknown_account_cannot_establish_cross_source_identity(self):
        # Both share same provider and request ID, but one has default/unknown account
        cand_a = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="anthropic",
            account_alias="default",
            provider_request_id="req_candidate_2",
        )
        cand_b = CrossSourceCandidate(
            origin_harness="oh-my-pi",
            source_adapter="omp",
            provider="anthropic",
            account_alias="acct_dev_1",
            provider_request_id="req_candidate_2",
        )
        is_same, is_overlap, reason = evaluate_cross_source_identity(cand_a, cand_b)
        self.assertFalse(is_same)
        self.assertTrue(is_overlap)
        self.assertEqual(reason, "unknown_account_cannot_establish_cross_source_identity")

    def test_distinct_accounts_are_not_overlap(self):
        cand_a = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="anthropic",
            account_alias="acct_1",
            provider_request_id="req_candidate_3",
        )
        cand_b = CrossSourceCandidate(
            origin_harness="oh-my-pi",
            source_adapter="omp",
            provider="anthropic",
            account_alias="acct_2",
            provider_request_id="req_candidate_3",
        )
        is_same, is_overlap, reason = evaluate_cross_source_identity(cand_a, cand_b)
        self.assertFalse(is_same)
        self.assertFalse(is_overlap)
        self.assertEqual(reason, "distinct_accounts")

    def test_verified_cross_source_identity_match(self):
        cand_a = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="anthropic",
            account_alias="acct_dev_1",
            provider_request_id="req_candidate_4",
        )
        cand_b = CrossSourceCandidate(
            origin_harness="oh-my-pi",
            source_adapter="omp",
            provider="anthropic",
            account_alias="acct_dev_1",
            provider_request_id="req_candidate_4",
        )
        is_same, is_overlap, reason = evaluate_cross_source_identity(cand_a, cand_b)
        self.assertTrue(is_same)
        self.assertFalse(is_overlap)
        self.assertEqual(reason, "verified_cross_source_identity")

    def test_never_dedupe_by_content_or_timestamp(self):
        # Two records with identical timestamp and model but distinct request IDs
        cand_a = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="anthropic",
            account_alias="acct_dev_1",
            provider_request_id="req_alpha",
            observed_at="2026-10-25T10:00:00Z",
        )
        cand_b = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="anthropic",
            account_alias="acct_dev_1",
            provider_request_id="req_beta",
            observed_at="2026-10-25T10:00:00Z",
        )
        is_same, is_overlap, reason = evaluate_cross_source_identity(cand_a, cand_b)
        self.assertFalse(is_same)
        self.assertFalse(is_overlap)

    def test_anonymous_scoped_account_cannot_establish_cross_source_identity(self):
        cand_a = CrossSourceCandidate(
            origin_harness="claude-code",
            source_adapter="claude_code",
            provider="anthropic",
            account_alias="anon_claude_code_abcd1234",
            provider_request_id="req_candidate_5",
        )
        cand_b = CrossSourceCandidate(
            origin_harness="oh-my-pi",
            source_adapter="omp",
            provider="anthropic",
            account_alias="anon_omp_efgh5678",
            provider_request_id="req_candidate_5",
        )
        is_same, is_overlap, reason = evaluate_cross_source_identity(cand_a, cand_b)
        self.assertFalse(is_same)
        self.assertTrue(is_overlap)
        self.assertEqual(reason, "unknown_account_cannot_establish_cross_source_identity")

class TestObservationNormalization(unittest.TestCase):
    """Substantive normalization tests verifying counters, TTLs, retries, and timing."""

    def test_repeat_observations_deduplication(self):
        # Two log lines representing duplicate observations of the same request
        raw_1 = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_claude_001",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_001",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 1500, "output_tokens": 250},
            },
        }
        raw_2 = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:01Z",
            "requestId": "req_claude_001",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_001",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 1500, "output_tokens": 250},
            },
        }

        rec_1 = normalize_claude_record(raw_1, file_locator="run1.jsonl", line_index=1)
        rec_2 = normalize_claude_record(raw_2, file_locator="run1.jsonl", line_index=2)

        # Observations must be distinct
        self.assertNotEqual(rec_1.observation["observation_id"], rec_2.observation["observation_id"])
        # Canonical request ID must be identical
        self.assertEqual(
            rec_1.canonical_request["canonical_id"],
            rec_2.canonical_request["canonical_id"],
        )
        expected_acct = rec_1.canonical_request["account_alias"]
        self.assertEqual(rec_1.canonical_request["canonical_id"], f"anthropic:{expected_acct}:req_claude_001")
        # Observation links must point to the same canonical request
        self.assertEqual(rec_1.link["canonical_id"], rec_2.link["canonical_id"])
        self.assertEqual(rec_1.link["observation_id"], rec_1.observation["observation_id"])
        self.assertEqual(rec_2.link["observation_id"], rec_2.observation["observation_id"])

    def test_unknown_ttl_preservation_claude(self):
        # Flat cache_creation_input_tokens without breakdown must be unknown TTL, NOT 5m!
        raw = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:05:00Z",
            "requestId": "req_claude_002",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_002",
                "model": "claude-haiku-4-5-20251001",
                "usage": {
                    "input_tokens": 3000,
                    "output_tokens": 120,
                    "cache_read_input_tokens": 1500,
                    "cache_creation_input_tokens": 1000,
                },
            },
        }
        rec = normalize_claude_record(raw)
        usage = rec.normalized_usage
        self.assertEqual(usage["fresh_input_tokens"], 3000)
        self.assertEqual(usage["output_tokens"], 120)
        self.assertEqual(usage["cache_read_tokens"], 1500)
        # CRITICAL: Must be unknown TTL, not 5m
        self.assertIsNone(usage["cache_write_5m_tokens"])
        self.assertIsNone(usage["cache_write_1h_tokens"])
        self.assertEqual(usage["cache_write_unknown_ttl_tokens"], 1000)

    def test_unknown_ttl_preservation_omp(self):
        # Flat cacheWrite without cttl breakdown must be unknown TTL, NOT 5m!
        raw = {
            "type": "message",
            "id": "evt_omp_005",
            "timestamp": "2026-10-25T11:05:00Z",
            "message": {
                "role": "assistant",
                "id": "msg_omp_005",
                "responseId": "resp_omp_002",
                "model": "gpt-6.1-sol-low",
                "provider": "openai",
                "duration": 1800,
                "ttft": 200,
                "usage": {
                    "input": 2500,
                    "output": 400,
                    "cacheRead": 1000,
                    "cacheWrite": 500,
                },
            },
        }
        rec = normalize_omp_record(raw)
        usage = rec.normalized_usage
        self.assertEqual(usage["fresh_input_tokens"], 2500)
        self.assertEqual(usage["output_tokens"], 400)
        self.assertEqual(usage["cache_read_tokens"], 1000)
        # CRITICAL: Must be unknown TTL, not 5m
        self.assertIsNone(usage["cache_write_5m_tokens"])
        self.assertIsNone(usage["cache_write_1h_tokens"])
        self.assertEqual(usage["cache_write_unknown_ttl_tokens"], 500)

    def test_detailed_ttl_breakdown(self):
        # When TTL breakdown is provided, 5m and 1h are populated
        raw = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_claude_001",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_001",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 250,
                    "cache_read_input_tokens": 800,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 400,
                        "ephemeral_1h_input_tokens": 100,
                    },
                    "reasoning_tokens": 50,
                },
            },
        }
        rec = normalize_claude_record(raw)
        usage = rec.normalized_usage
        self.assertEqual(usage["cache_write_5m_tokens"], 400)
        self.assertEqual(usage["cache_write_1h_tokens"], 100)
        self.assertIsNone(usage["cache_write_unknown_ttl_tokens"])
        self.assertEqual(usage["reasoning_tokens"], 50)

    def test_missing_vs_zero_token_counters(self):
        # 1. Measured zero: field present and equal to 0
        raw_zero = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_zero",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_zero",
                "model": "claude-sonnet-5-5",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
        rec_zero = normalize_claude_record(raw_zero)
        usage_zero = rec_zero.normalized_usage
        self.assertEqual(usage_zero["fresh_input_tokens"], 1000)
        self.assertEqual(usage_zero["output_tokens"], 0)  # Measured zero!
        self.assertEqual(usage_zero["cache_read_tokens"], 0)  # Measured zero!

        # 2. Missing counters: fields absent from JSON
        raw_missing = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:12:00Z",
            "requestId": "req_claude_005",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_claude_005",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 500},
            },
        }
        rec_missing = normalize_claude_record(raw_missing)
        usage_missing = rec_missing.normalized_usage
        self.assertEqual(usage_missing["fresh_input_tokens"], 500)
        # CRITICAL: Missing counters must be None, NOT 0!
        self.assertIsNone(usage_missing["output_tokens"])
        self.assertIsNone(usage_missing["cache_read_tokens"])
        self.assertIsNone(usage_missing["cache_write_5m_tokens"])
        self.assertEqual(usage_missing["is_partial"], 1)
        missing_list = json.loads(usage_missing["missing_counters_json"])
        self.assertIn("output_tokens", missing_list)
        self.assertIn("cache_read_tokens", missing_list)

    def test_reasoning_tokens_is_reported_subset(self):
        raw = {
            "type": "message",
            "id": "evt_omp_001",
            "timestamp": "2026-10-25T11:00:10Z",
            "message": {
                "role": "assistant",
                "id": "msg_omp_001",
                "responseId": "resp_omp_001",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "duration": 2500,
                "ttft": 400,
                "usage": {
                    "input": 1800,
                    "output": 350,
                    "cacheRead": 1200,
                    "reasoning": 80,
                    "totalTokens": 3950,
                },
            },
        }
        rec = normalize_omp_record(raw)
        usage = rec.normalized_usage
        self.assertEqual(usage["fresh_input_tokens"], 1800)
        self.assertEqual(usage["output_tokens"], 350)
        self.assertEqual(usage["reasoning_tokens"], 80)
        self.assertEqual(usage["reported_total_tokens"], 3950)
        # Reasoning subset must not be added to output
        self.assertEqual(usage["output_tokens"], 350)

    def test_distinct_retries_not_collapsed(self):
        raw_orig = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_claude_001",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_001",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 1500, "output_tokens": 250},
            },
        }
        raw_retry = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:05Z",
            "requestId": "req_claude_001_retry_1",
            "account_id": "acct_dev_1",
            "retry_attempt": 1,
            "parent_request_id": "req_claude_001",
            "message": {
                "id": "msg_001_retry_1",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 1500, "output_tokens": 260},
            },
        }
        rec_orig = normalize_claude_record(raw_orig)
        rec_retry = normalize_claude_record(raw_retry)

        self.assertNotEqual(
            rec_orig.canonical_request["canonical_id"],
            rec_retry.canonical_request["canonical_id"],
        )
        self.assertEqual(rec_retry.canonical_request["retry_attempt"], 1)
        self.assertEqual(rec_retry.canonical_request["parent_request_id"], "req_claude_001")

    def test_omp_speed_and_timing_calculations(self):
        # 1. Normal timed request: (2500 - 400) / 1000 = 2.1s; 350 / 2.1 = 166.6667 TPS
        raw_timed = {
            "type": "message",
            "id": "evt_1",
            "timestamp": "2026-10-25T11:00:10Z",
            "message": {
                "role": "assistant",
                "responseId": "resp_1",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "duration": 2500,
                "ttft": 400,
                "usage": {"input": 1800, "output": 350},
            },
        }
        rec_timed = normalize_omp_record(raw_timed)
        req = rec_timed.canonical_request
        self.assertEqual(req["timing_basis"], "measured")
        self.assertEqual(req["duration_ms"], 2500)
        self.assertEqual(req["ttft_ms"], 400)
        self.assertEqual(req["generation_duration_ms"], 2100)
        self.assertAlmostEqual(req["generation_speed_tps"], 166.6667, places=3)

        # 2. Long measured generation (> 600s): plan requires all measured generation times with NO arbitrary cutoff
        raw_long = {
            "type": "message",
            "id": "evt_2",
            "timestamp": "2026-10-25T11:10:00Z",
            "message": {
                "role": "assistant",
                "responseId": "resp_2",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "duration": 700000,
                "ttft": 1000,
                "usage": {"input": 1000, "output": 200},
            },
        }
        rec_long = normalize_omp_record(raw_long)
        req_long = rec_long.canonical_request
        self.assertEqual(req_long["timing_basis"], "measured")
        self.assertEqual(req_long["duration_ms"], 700000)
        self.assertEqual(req_long["ttft_ms"], 1000)
        self.assertEqual(req_long["generation_duration_ms"], 699000)
        self.assertAlmostEqual(req_long["generation_speed_tps"], round(200 / 699.0, 4), places=4)

        # 3. Measured zero output with duration > 0 -> TPS is 0.0, NOT None
        raw_zero_out = {
            "type": "message",
            "id": "evt_3",
            "timestamp": "2026-10-25T11:12:00Z",
            "message": {
                "role": "assistant",
                "responseId": "resp_3",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "duration": 1500,
                "ttft": 500,
                "usage": {"input": 1000, "output": 0},
            },
        }
        rec_zero_out = normalize_omp_record(raw_zero_out)
        req_zero = rec_zero_out.canonical_request
        self.assertEqual(req_zero["timing_basis"], "measured")
        self.assertEqual(req_zero["duration_ms"], 1500)
        self.assertEqual(req_zero["ttft_ms"], 500)
        self.assertEqual(req_zero["generation_duration_ms"], 1000)
        self.assertEqual(req_zero["generation_speed_tps"], 0.0)

        # 4. Absent TTFT -> total duration 2s preserved, ttft_ms is None (NOT 0!),
        # generation_duration_ms and generation_speed_tps remain unavailable (None)
        raw_no_ttft = {
            "type": "message",
            "id": "evt_no_ttft",
            "timestamp": "2026-10-25T11:14:00Z",
            "message": {
                "role": "assistant",
                "responseId": "resp_no_ttft",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "duration": 2000,
                "ttft": None,
                "usage": {"input": 1000, "output": 100},
            },
        }
        rec_no_ttft = normalize_omp_record(raw_no_ttft)
        req_no_ttft = rec_no_ttft.canonical_request
        self.assertEqual(req_no_ttft["timing_basis"], "measured")
        self.assertEqual(req_no_ttft["duration_ms"], 2000)
        self.assertIsNone(req_no_ttft["ttft_ms"])
        self.assertIsNone(req_no_ttft["generation_duration_ms"])
        self.assertIsNone(req_no_ttft["generation_speed_tps"])

        # 5. Untimed request (duration is None) -> speed must be None, timing_basis none
        raw_untimed = {
            "type": "message",
            "id": "evt_4",
            "timestamp": "2026-10-25T11:15:00Z",
            "message": {
                "role": "assistant",
                "responseId": "resp_4",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "duration": None,
                "ttft": None,
                "usage": {"input": 1200, "output": 180},
            },
        }
        rec_untimed = normalize_omp_record(raw_untimed)
        self.assertEqual(rec_untimed.canonical_request["timing_basis"], "none")
        self.assertIsNone(rec_untimed.canonical_request["generation_speed_tps"])

    def test_tool_call_trace_event_extraction(self):
        raw_claude = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_claude_tools",
            "account_id": "acct_dev_1",
            "message": {
                "id": "msg_tools",
                "model": "claude-sonnet-5-5-20251001",
                "content": [
                    {"type": "text", "text": "Editing file"},
                    {"type": "tool_use", "id": "toolu_01", "name": "edit", "input": {"path": "app.py"}},
                    {"type": "tool_use", "id": "toolu_02", "name": "bash", "input": {"command": "ls"}},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        }
        rec = normalize_claude_record(raw_claude)
        self.assertEqual(len(rec.events), 2)
        self.assertEqual(rec.events[0]["event_id"], "toolu_01")
        self.assertEqual(rec.events[0]["event_name"], "edit")
        self.assertEqual(rec.events[1]["event_id"], "toolu_02")
        self.assertEqual(rec.events[1]["event_name"], "bash")

    def test_absent_account_scoping_claude_and_omp(self):
        raw_claude = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_no_acct_1",
            "message": {
                "id": "msg_no_acct_1",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        }
        rec_claude = normalize_claude_record(raw_claude, file_locator="/path/to/claude.jsonl")
        self.assertTrue(rec_claude.canonical_request["account_alias"].startswith("anon_claude_code_"))
        self.assertEqual(rec_claude.canonical_request["is_unresolved_overlap"], 1)

        raw_omp = {
            "type": "message",
            "id": "evt_no_acct_1",
            "timestamp": "2026-10-25T11:00:10Z",
            "message": {
                "role": "assistant",
                "responseId": "resp_no_acct_1",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "usage": {"input": 200, "output": 40},
            },
        }
        rec_omp = normalize_omp_record(raw_omp, file_locator="/path/to/omp.jsonl")
        self.assertTrue(rec_omp.canonical_request["account_alias"].startswith("anon_omp_"))
        self.assertEqual(rec_omp.canonical_request["is_unresolved_overlap"], 1)

    def test_corrupt_json_lines_safe_provenance_and_error(self):
        rec_c = normalize_claude_record("{CORRUPT_JSON_DATA", file_locator="claude.jsonl", line_index=15)
        self.assertEqual(rec_c.parse_status, "corrupt")
        self.assertIn("Invalid JSON", rec_c.error_message)
        self.assertNotIn("{CORRUPT", rec_c.observation["provenance_json"])
        prov_c = json.loads(rec_c.observation["provenance_json"])
        self.assertIn("error_kind", prov_c)
        self.assertEqual(prov_c["line_index"], 15)

        rec_o = normalize_omp_record("{ANOTHER_CORRUPT_LINE", file_locator="omp.jsonl", line_index=42)
        self.assertEqual(rec_o.parse_status, "corrupt")
        self.assertIn("Invalid JSON", rec_o.error_message)
        self.assertNotIn("{ANOTHER", rec_o.observation["provenance_json"])
        prov_o = json.loads(rec_o.observation["provenance_json"])
        self.assertIn("error_kind", prov_o)
        self.assertEqual(prov_o["line_index"], 42)

    def test_omp_session_header_provenance_has_safe_cwd_alias(self):
        raw_session = {
            "type": "session",
            "id": "sess_001",
            "cwd": "/home/user/secret-repo",
            "timestamp": "2026-10-25T11:00:00Z",
        }
        rec_s = normalize_omp_record(raw_session)
        prov_s = json.loads(rec_s.observation["provenance_json"])
        self.assertEqual(prov_s["cwd_alias"], "secret-repo")
        self.assertNotIn("/home/user", rec_s.observation["provenance_json"])

class TestSQLiteDatabaseIntegration(unittest.TestCase):
    """Verify that normalized entities insert cleanly into canonical SQLite schema."""

    def setUp(self):
        self.conn = open_database(":memory:", run_migrations=True)

    def tearDown(self):
        self.conn.close()

    def test_insert_normalized_record_round_trip(self):
        raw = {
            "type": "assistant",
            "timestamp": "2026-10-25T10:00:00Z",
            "requestId": "req_round_trip_1",
            "account_id": "acct_dev_1",
            "cwd": "/home/user/project_alpha",
            "message": {
                "id": "msg_rt_1",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 250,
                    "cache_read_input_tokens": 800,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 400,
                        "ephemeral_1h_input_tokens": 100,
                    },
                    "reasoning_tokens": 50,
                },
            },
        }

        rec = normalize_claude_record(raw)

        # 1. Insert observation
        obs = rec.observation
        self.conn.execute(
            """
            INSERT INTO source_observations (
                observation_id, source_adapter, origin_harness, account_alias,
                file_locator, file_generation, line_index, event_id,
                observed_at, raw_timestamp, parser_version, raw_counters_json,
                source_reported_total, provenance_json, parse_status,
                error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                obs["observation_id"], obs["source_adapter"], obs["origin_harness"],
                obs["account_alias"], obs["file_locator"], obs["file_generation"],
                obs["line_index"], obs["event_id"], obs["observed_at"],
                obs["raw_timestamp"], obs["parser_version"], obs["raw_counters_json"],
                obs["source_reported_total"], obs["provenance_json"], obs["parse_status"],
                obs["error_message"], obs["created_at"],
            ),
        )
        # 2. Insert canonical request
        req = rec.canonical_request
        # Ensure parent project exists for foreign key constraint
        if req["project_id"]:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO projects (project_id, project_name, account_alias, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (req["project_id"], req["project_id"], req["account_alias"], req["created_at"]),
            )
        self.conn.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, provider_request_id,
                fallback_identity, origin_harness, access_route, raw_model,
                normalized_model, session_id, project_id, prompt_id,
                parent_request_id, retry_attempt, request_status,
                request_timestamp, timing_basis, duration_ms, ttft_ms,
                generation_duration_ms, generation_speed_tps,
                is_unresolved_overlap, pricing_version_id, created_at,
                raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                req["canonical_id"], req["provider"], req["account_alias"],
                req["provider_request_id"], req["fallback_identity"], req["origin_harness"],
                req["access_route"], req["raw_model"], req["normalized_model"],
                req["session_id"], req["project_id"], req["prompt_id"],
                req["parent_request_id"], req["retry_attempt"], req["request_status"],
                req["request_timestamp"], req["timing_basis"], req["duration_ms"],
                req["ttft_ms"], req["generation_duration_ms"], req["generation_speed_tps"],
                req["is_unresolved_overlap"], req["pricing_version_id"], req["created_at"],
                req["raw_payload_json"],
            ),
        )

        # 3. Insert normalized usage
        u = rec.normalized_usage
        self.conn.execute(
            """
            INSERT INTO normalized_usage (
                canonical_id, fresh_input_tokens, cache_read_tokens,
                cache_write_5m_tokens, cache_write_1h_tokens,
                cache_write_unknown_ttl_tokens, output_tokens, reasoning_tokens,
                reported_total_tokens, counter_inclusion_semantics,
                normalization_rule_version, is_partial, missing_counters_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                u["canonical_id"], u["fresh_input_tokens"], u["cache_read_tokens"],
                u["cache_write_5m_tokens"], u["cache_write_1h_tokens"],
                u["cache_write_unknown_ttl_tokens"], u["output_tokens"],
                u["reasoning_tokens"], u["reported_total_tokens"],
                u["counter_inclusion_semantics"], u["normalization_rule_version"],
                u["is_partial"], u["missing_counters_json"],
            ),
        )

        # 4. Insert link
        link = rec.link
        self.conn.execute(
            """
            INSERT INTO request_observation_links (canonical_id, observation_id, linked_at)
            VALUES (?, ?, ?);
            """,
            (link["canonical_id"], link["observation_id"], link["linked_at"]),
        )

        self.conn.commit()

        # Query back and verify
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT cr.canonical_id, cr.provider, cr.account_alias, nu.fresh_input_tokens,
                   nu.output_tokens, nu.cache_write_5m_tokens, nu.cache_write_1h_tokens,
                   nu.reasoning_tokens, COUNT(rol.observation_id) as obs_count
            FROM canonical_requests cr
            JOIN normalized_usage nu ON cr.canonical_id = nu.canonical_id
            JOIN request_observation_links rol ON cr.canonical_id = rol.canonical_id
            WHERE cr.canonical_id = ?
            GROUP BY cr.canonical_id;
            """,
            (req["canonical_id"],),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], req["canonical_id"])
        self.assertEqual(row[1], "anthropic")
        self.assertEqual(row[2], req["account_alias"])
        self.assertEqual(row[3], 1500)
        self.assertEqual(row[4], 250)
        self.assertEqual(row[5], 400)
        self.assertEqual(row[6], 100)
        self.assertEqual(row[7], 50)
        self.assertEqual(row[8], 1)


class TestSyntheticBaselineFixturesParsing(unittest.TestCase):
    """End-to-end normalization against the official synthetic baseline fixtures."""

    def test_parse_claude_baseline_fixture(self):
        self.assertTrue(os.path.exists(CLAUDE_FIXTURE), f"Fixture missing: {CLAUDE_FIXTURE}")

        records = []
        with open(CLAUDE_FIXTURE, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                rec = normalize_observation(
                    raw_record=line,
                    source_adapter="claude_code",
                    file_locator=CLAUDE_FIXTURE,
                    line_index=idx,
                )
                records.append(rec)

        # Must parse all 15 fixture lines
        self.assertEqual(len(records), 15)

        # Corrupt line 15 must be flagged corrupt
        corrupt_rec = records[14]
        self.assertEqual(corrupt_rec.parse_status, "corrupt")

        # User message line 11 must be skipped
        user_rec = records[10]
        self.assertEqual(user_rec.parse_status, "skipped")

        # Assistant records with usage
        usage_records = [r for r in records if r.canonical_request is not None]
        self.assertGreater(len(usage_records), 5)

        # Verify req_claude_002 preserves unknown TTL (1000) rather than 5m
        rec_2 = next(r for r in usage_records if r.canonical_request["provider_request_id"] == "req_claude_002")
        self.assertEqual(rec_2.normalized_usage["cache_write_unknown_ttl_tokens"], 1000)
        self.assertIsNone(rec_2.normalized_usage["cache_write_5m_tokens"])

        # Verify req_claude_005 preserves partial missing counters
        rec_5 = next(r for r in usage_records if r.canonical_request["provider_request_id"] == "req_claude_005")
        self.assertEqual(rec_5.normalized_usage["is_partial"], 1)
        self.assertIsNone(rec_5.normalized_usage["output_tokens"])

    def test_parse_omp_baseline_fixture(self):
        self.assertTrue(os.path.exists(OMP_FIXTURE), f"Fixture missing: {OMP_FIXTURE}")

        records = []
        with open(OMP_FIXTURE, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                rec = normalize_observation(
                    raw_record=line,
                    source_adapter="omp",
                    file_locator=OMP_FIXTURE,
                    line_index=idx,
                )
                records.append(rec)

        # Must parse all 16 fixture lines
        self.assertEqual(len(records), 16)

        # Line 1 is session header
        sess_rec = records[0]
        self.assertEqual(sess_rec.observation["parse_status"], "ok")
        self.assertIsNone(sess_rec.canonical_request)

        # Corrupt line 16 must be flagged corrupt
        corrupt_rec = records[15]
        self.assertEqual(corrupt_rec.parse_status, "corrupt")

        # User message line 14 must be skipped
        user_rec = records[13]
        self.assertEqual(user_rec.parse_status, "skipped")

        # Verify req 1 has generation speed TPS
        rec_1 = next(r for r in records if r.canonical_request and r.canonical_request["provider_request_id"] == "resp_omp_001")
        self.assertIsNotNone(rec_1.canonical_request["generation_speed_tps"])
        self.assertAlmostEqual(rec_1.canonical_request["generation_speed_tps"], 166.6667, places=3)

        # Verify measured long interval (duration 700,000) retains measured speed and duration without arbitrary cutoff
        rec_idle = next(r for r in records if r.canonical_request and r.canonical_request["provider_request_id"] == "resp_omp_004")
        self.assertEqual(rec_idle.canonical_request["timing_basis"], "measured")
        self.assertEqual(rec_idle.canonical_request["duration_ms"], 700000)
        self.assertEqual(rec_idle.canonical_request["generation_duration_ms"], 699000)
        self.assertAlmostEqual(rec_idle.canonical_request["generation_speed_tps"], round(200 / 699.0, 4), places=4)


if __name__ == "__main__":
    unittest.main()
