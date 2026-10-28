"""Baseline synthetic fixture tests for Token Ledger adapters.

Exercises tokentrack.py parsing, normalization, deduplication, timing,
and pricing against synthetic fixtures:
  - tests/fixtures/claude-baseline.jsonl
  - tests/fixtures/omp-baseline.jsonl

No user data. Tests verify legacy CLI behavior and capture expected missing states.
"""

import json
import os
import unittest

import tokentrack


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
CLAUDE_FIXTURE = os.path.join(FIXTURES_DIR, "claude-baseline.jsonl")
OMP_FIXTURE = os.path.join(FIXTURES_DIR, "omp-baseline.jsonl")


def load_pricing():
    """Load default pricing.json from project root."""
    pricing_path = os.path.join(os.path.dirname(__file__), "..", "pricing.json")
    with open(pricing_path, encoding="utf-8") as f:
        return json.load(f).get("models", {})


class TestClaudeBaselineParsing(unittest.TestCase):
    """Verify parsing behavior and field extraction for Claude Code synthetic fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.rates = load_pricing()
        cls.parsed = tokentrack._parse_claude_file(CLAUDE_FIXTURE)

    def test_parsed_file_structure(self):
        self.assertEqual(self.parsed["source"], "claude-code")
        self.assertIn("sid", self.parsed)
        self.assertIn("rows", self.parsed)
        self.assertIn("tools", self.parsed)
        # Sidechain message in fixture marks subagent true
        self.assertTrue(self.parsed["subagent"])

    def test_tool_extraction(self):
        tool_ids = [t[1] for t in self.parsed["tools"]]
        tool_names = [t[2] for t in self.parsed["tools"]]
        self.assertIn("toolu_001", tool_ids)
        self.assertIn("edit", tool_names)
        self.assertIn("toolu_002", tool_ids)
        self.assertIn("bash", tool_names)
        # Tool call extracted even when assistant message has no usage
        self.assertIn("toolu_003", tool_ids)
        self.assertIn("read", tool_names)
        self.assertIn("toolu_004", tool_ids)
        self.assertIn("write", tool_names)

    def test_standard_row_and_model_normalization(self):
        # Model date suffix stripped
        r1 = next(r for r in self.parsed["rows"] if r["_key"][0] == "req_claude_001")
        self.assertEqual(r1["model"], "claude-sonnet-5-5")
        self.assertEqual(r1["provider"], "anthropic")
        self.assertEqual(r1["in"], 1500)
        self.assertEqual(r1["out"], 250)
        self.assertEqual(r1["cr"], 800)
        self.assertEqual(r1["cw5m"], 400)
        self.assertEqual(r1["cw1h"], 100)
        # Claude logs have no timing
        self.assertIsNone(r1["secs"])

    def test_legacy_flat_ttl_fallback(self):
        rows_by_req = {r["_key"][0]: r for r in self.parsed["rows"] if r["_key"][0]}
        # Flat cache_creation_input_tokens assumed 5m in legacy parser
        r2 = rows_by_req["req_claude_002"]
        self.assertEqual(r2["cw5m"], 1000)
        self.assertEqual(r2["cw1h"], 0)

    def test_ignored_entries(self):
        models = [r["model"] for r in self.parsed["rows"]]
        # <synthetic> and unknown models dropped by legacy parser
        self.assertNotIn("<synthetic>", models)
        # Non-assistant messages (e.g. user prompt) not parsed into rows
        self.assertNotIn("Please review this pull request.", [str(r) for r in self.parsed["rows"]])

    def test_unpriced_model_retained_with_zero_cost(self):
        rows_by_req = {r["_key"][0]: r for r in self.parsed["rows"] if r["_key"][0]}
        r_unpriced = rows_by_req["req_claude_004"]
        self.assertEqual(r_unpriced["model"], "claude-experimental-future-v1")
        self.assertFalse(tokentrack.model_status(r_unpriced["model"], self.rates))
        self.assertEqual(tokentrack.price(r_unpriced, self.rates), 0.0)

    def test_haiku_long_tier_repricing(self):
        rows_by_req = {r["_key"][0]: r for r in self.parsed["rows"] if r["_key"][0]}
        r_haiku = rows_by_req["req_claude_009"]
        self.assertEqual(r_haiku["model"], "claude-haiku-5-5")
        prompt_tokens = r_haiku["in"] + r_haiku["cr"] + r_haiku["cw5m"] + r_haiku["cw1h"]
        # Prompt 135,000 exceeds long tier threshold 100,000
        self.assertGreater(prompt_tokens, 100000)
        rate = tokentrack.rate_for("claude-haiku-5-5", prompt_tokens, self.rates)
        self.assertEqual(rate["in"], 0.5)
        self.assertEqual(rate["out"], 2.5)

    def test_legacy_deduplication_key_behavior(self):
        # In legacy tokentrack, dedupe key is (source, (requestId, msg.id))
        keys = [tuple(r["_key"]) for r in self.parsed["rows"]]
        # Multiple rows with key ('req_claude_001', 'msg_claude_001') exist in raw parsed list
        dupe_count = keys.count(("req_claude_001", "msg_claude_001"))
        self.assertGreater(dupe_count, 1)
        # Distinct retry ID is separate key
        self.assertIn(("req_claude_001_retry_1", "msg_claude_001_retry_1"), keys)


class TestOMPBaselineParsing(unittest.TestCase):
    """Verify parsing behavior and field extraction for Oh-My-Pi synthetic fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.rates = load_pricing()
        cls.parsed = tokentrack._parse_omp_file(OMP_FIXTURE)

    def test_parsed_file_structure(self):
        self.assertEqual(self.parsed["source"], "omp")
        self.assertIn("sid", self.parsed)
        self.assertIn("rows", self.parsed)
        self.assertIn("tools", self.parsed)
        # Session header has parentSession: null
        self.assertFalse(self.parsed["subagent"])
        # Session cwd establishes project for all rows
        for r in self.parsed["rows"]:
            self.assertEqual(r["project"], "project-beta")

    def test_tool_extraction(self):
        tool_ids = [t[1] for t in self.parsed["tools"]]
        tool_names = [t[2] for t in self.parsed["tools"]]
        self.assertIn("call_omp_001", tool_ids)
        self.assertIn("read", tool_names)
        self.assertIn("call_omp_002", tool_ids)
        self.assertIn("edit", tool_names)
        # Tool call extracted even without usage
        self.assertIn("call_omp_003", tool_ids)
        self.assertIn("write", tool_names)

    def test_generation_speed_and_timing_rules(self):
        # Valid generation timing: (2500 - 400) / 1000 = 2.1s
        r1 = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_001")
        self.assertAlmostEqual(r1["secs"], 2.1)

        # Retry timing: (2200 - 350) / 1000 = 1.85s
        r_retry = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_001_retry_1")
        self.assertAlmostEqual(r_retry["secs"], 1.85)

        # Duration > SPEED_MAX_SECS (700s > 600s) -> secs is None
        r_toolong = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_004")
        self.assertIsNone(r_toolong["secs"])

        # Zero output tokens -> secs is None
        r_zero_out = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_005")
        self.assertIsNone(r_zero_out["secs"])

        # Missing duration -> secs is None
        r_untimed = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_006")
        self.assertIsNone(r_untimed["secs"])

    def test_ttl_handling(self):
        # Split TTL from cttl
        r1 = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_001")
        self.assertEqual(r1["cw5m"], 600)
        self.assertEqual(r1["cw1h"], 200)

        # Flat TTL fallback from cacheWrite
        r2 = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_002")
        self.assertEqual(r2["cw5m"], 500)
        self.assertEqual(r2["cw1h"], 0)

    def test_model_normalization(self):
        # In legacy tokentrack, model name retains effort suffix but rate_key resolves base model
        r2 = next(r for r in self.parsed["rows"] if r["_key"] == "resp_omp_002")
        self.assertEqual(r2["model"], "gpt-6.1-sol-low")
        self.assertEqual(tokentrack.rate_key(r2["model"], self.rates), "gpt-6.1-sol")
        self.assertEqual(r2["provider"], "openai")
    def test_fallback_key_when_no_response_id(self):
        rows = self.parsed["rows"]
        # Entry without responseId uses entry id 'evt_omp_fallback_014' as _key
        matching = [r for r in rows if r["_key"] == "evt_omp_fallback_014"]
        self.assertEqual(len(matching), 1)

    def test_epoch_timestamp_parsed(self):
        rows_by_key = {r["_key"]: r for r in self.parsed["rows"]}
        r_epoch = rows_by_key["resp_omp_007"]
        # Epoch timestamp 1792927200000 converted to ISO string
        self.assertIsNotNone(r_epoch["ts"])
        self.assertTrue(r_epoch["ts"].startswith("2026-"))


class TestContractGapsAndTargetSemantics(unittest.TestCase):
    """Exercise and document behavioral gaps between legacy parser and target canonical contract."""

    def test_account_scoping_gap_in_legacy_dedupe(self):
        """Legacy dedupe collapses records with identical IDs across different accounts."""
        parsed = tokentrack._parse_claude_file(CLAUDE_FIXTURE)
        keys = [tuple(r["_key"]) for r in parsed["rows"]]
        # In legacy parser, record 1 (acct_dev_1) and record 3 (acct_team_2) share key
        # and would be deduplicated by collect() set tracking
        shared_key = ("req_claude_001", "msg_claude_001")
        matching_rows = [r for r in parsed["rows"] if tuple(r["_key"]) == shared_key]
        self.assertGreaterEqual(len(matching_rows), 2)
        # Target contract requires (provider, account, id) scoping to avoid cross-account loss

    def test_unknown_ttl_loss_in_legacy_parser(self):
        """Legacy parser assigns flat cache writes to cw5m, losing unknown TTL fact."""
        parsed = tokentrack._parse_claude_file(CLAUDE_FIXTURE)
        rows_by_req = {r["_key"][0]: r for r in parsed["rows"] if r["_key"][0]}
        r_unknown = rows_by_req["req_claude_003"]
        # Legacy forced cw5m = 750, cw1h = 0
        self.assertEqual(r_unknown["cw5m"], 750)
        self.assertEqual(r_unknown["cw1h"], 0)
        # Target contract WP03 preserves unknown TTL as distinct counter

    def test_timing_availability_difference(self):
        """Claude Code has unavailable timing (secs=None); OMP provides measured duration and TTFT."""
        claude_parsed = tokentrack._parse_claude_file(CLAUDE_FIXTURE)
        omp_parsed = tokentrack._parse_omp_file(OMP_FIXTURE)

        # Claude: all secs must be None
        for r in claude_parsed["rows"]:
            self.assertIsNone(r["secs"], "Claude requests must not have synthetic timing")

        # OMP: timed request has numeric secs
        timed_omp = [r for r in omp_parsed["rows"] if r["secs"] is not None]
        self.assertGreater(len(timed_omp), 0, "OMP must retain valid generation timings")


if __name__ == "__main__":
    unittest.main()
