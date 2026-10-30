"""Unit and integration regression tests for WP15 native trace and event capture.

Tests:
1. Secrecy: Strips tool arguments/payloads, preserving safe metadata (status, duration, output size).
2. Hierarchy: Verifies source-observed session parent links without synthetic parent inference.
3. Compaction: Preserves nullable before/after context measurements (never defaulting to 0).
4. Namespace Classification: Identifies MCP, skill, and hook events strictly from verified source prefixes.
5. Prompt Groups: Captures promptgroup references without prompt body text.
6. Privacy & Content Fingerprints: Opt-in only, no hashing by default.
7. End-to-End Ingestion & traces.analyze: Ingests trace-native.jsonl via runner and validates trace analysis.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from token_ledger.ingest.events import (
    classify_event_kind,
    extract_native_events,
    extract_prompt_group,
    extract_session_hierarchy,
)
from token_ledger.ingest.runner import ingest_file
from token_ledger.ingest.normalize import normalize_observation
from token_ledger.metrics.traces import analyze
from token_ledger.privacy import (
    OPTIN_CONTENT_FINGERPRINTS,
    save_privacy_setting,
)
from token_ledger.store import connect, migrate, open_database


class TestNativeTraceEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="token_ledger_test_native_events_")
        self.db_path = os.path.join(self.tmp_dir, "test_events.db")
        self.conn = open_database(self.db_path, run_migrations=True)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_secrecy_strips_tool_payloads_preserves_metadata(self) -> None:
        """Verifies tool arguments and output bodies are stripped, preserving safe metadata only."""
        raw_invoc = {
            "type": "assistant",
            "timestamp": "2026-10-25T12:00:00Z",
            "sessionId": "sess_secret_01",
            "message": {
                "id": "msg_001",
                "duration": 450,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_private_001",
                        "name": "read",
                        "input": {
                            "path": "/home/private/user_keys/secret_token.txt",
                            "secret_payload": "SUPER_SECRET_VALUE_DO_NOT_STORE",
                        },
                    }
                ],
            },
        }

        events = extract_native_events(raw_invoc, adapter="claude_code", canonical_id="can_001")
        self.assertEqual(len(events), 1)
        ev = events[0]

        # Metadata preserved
        self.assertEqual(ev["event_id"], "toolu_private_001")
        self.assertEqual(ev["canonical_id"], "can_001")
        self.assertEqual(ev["session_id"], "sess_secret_01")
        self.assertEqual(ev["event_name"], "read")
        self.assertEqual(ev["event_kind"], "tool_call")
        self.assertEqual(ev["duration_ms"], 450)
        self.assertIsNone(ev["output_size_bytes"])

        # Details must NOT contain secret payload or raw un-sanitized root paths
        details = json.loads(ev["details_json"])
        self.assertNotIn("secret_payload", details)
        self.assertNotIn("SUPER_SECRET_VALUE_DO_NOT_STORE", ev["details_json"])
        # Safe path alias is an opaque locator hash.
        self.assertTrue(details["path"].startswith("loc_"))

        # Test tool_result stripping
        raw_res = {
            "type": "tool_result",
            "timestamp": "2026-10-25T12:00:01Z",
            "sessionId": "sess_secret_01",
            "tool_use_id": "toolu_private_001",
            "is_error": False,
            "duration_ms": 110,
            "content": "CONFIDENTIAL_OUTPUT_BUFFER_12345",
        }
        res_events = extract_native_events(raw_res, adapter="claude_code", canonical_id="can_001")
        self.assertEqual(len(res_events), 1)
        rev = res_events[0]
        self.assertEqual(rev["parent_event_id"], "toolu_private_001")
        self.assertEqual(rev["status"], "ok")
        self.assertEqual(rev["duration_ms"], 110)
        self.assertEqual(rev["output_size_bytes"], len("CONFIDENTIAL_OUTPUT_BUFFER_12345".encode("utf-8")))

        # Output payload body strictly excluded
        res_details = json.loads(rev["details_json"])
        self.assertNotIn("CONFIDENTIAL_OUTPUT_BUFFER_12345", rev["details_json"])
        self.assertNotIn("content", res_details)

        # Overlapping nested and top-level result records share one source event ID.
        duplicate_res = {
            "type": "tool_result",
            "sessionId": "sess_secret_01",
            "tool_use_id": "toolu_private_001",
            "content": "synthetic output",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_private_001", "content": "synthetic output"}
                ]
            },
        }
        self.assertEqual(len(extract_native_events(duplicate_res, adapter="claude_code")), 1)

    def test_hierarchy_source_observed_links(self) -> None:
        """Session parents are source fields; Claude parentUuid is an event parent."""
        root_rec = {"type": "session", "id": "sess_root", "parentSession": None}
        h_root = extract_session_hierarchy(root_rec, adapter="omp")
        self.assertEqual(h_root["session_id"], "sess_root")
        self.assertIsNone(h_root["parent_session_id"])
        self.assertEqual(h_root["is_subagent"], 0)

        child_omp = {"type": "session", "id": "sess_child", "parentSessionId": "sess_root"}
        h_child = extract_session_hierarchy(child_omp, adapter="omp")
        self.assertEqual(h_child["session_id"], "sess_child")
        self.assertEqual(h_child["parent_session_id"], "sess_root")
        self.assertEqual(h_child["is_subagent"], 1)

        child_claude = {"sessionId": "sess_claude_sub", "parentUuid": "evt_parent"}
        h_claude = extract_session_hierarchy(child_claude, adapter="claude_code")
        self.assertEqual(h_claude["session_id"], "sess_claude_sub")
        self.assertIsNone(h_claude["parent_session_id"])
        self.assertEqual(h_claude["is_subagent"], 0)

        parent_event = extract_native_events(
            {
                "type": "assistant",
                "sessionId": "sess_claude_sub",
                "parentUuid": "evt_parent",
                "message": {
                    "content": [{"type": "tool_use", "id": "evt_child", "name": "read", "input": {}}]
                },
            },
            adapter="claude_code",
        )[0]
        self.assertEqual(parent_event["parent_event_id"], "evt_parent")

        standalone = {"type": "assistant", "sessionId": "sess_lone"}
        h_lone = extract_session_hierarchy(standalone, adapter="claude_code")
        self.assertIsNone(h_lone["parent_session_id"])
        self.assertEqual(h_lone["is_subagent"], 0)

    def test_explicit_compaction_with_nullable_measurements(self) -> None:
        """Verifies compaction preserves nullable before/after measurements and avoids default 0."""
        # Measured compaction
        rec_measured = {
            "type": "compaction",
            "id": "compact_01",
            "sessionId": "sess_comp",
            "timestamp": "2026-10-25T13:00:00Z",
            "before_tokens": 80000,
            "after_tokens": 60000,
            "duration_ms": 500,
        }
        ev_meas = extract_native_events(rec_measured, adapter="claude_code")[0]
        self.assertEqual(ev_meas["event_kind"], "compaction")
        d_meas = json.loads(ev_meas["details_json"])
        self.assertEqual(d_meas["before_tokens"], 80000)
        self.assertEqual(d_meas["after_tokens"], 60000)
        self.assertEqual(d_meas["reduction_tokens"], 20000)
        self.assertEqual(d_meas["reduction_ratio"], 0.25)

        # Unmeasured compaction (e.g. source records /compact without counter deltas)
        rec_unmeasured = {
            "type": "compaction",
            "id": "compact_02",
            "sessionId": "sess_comp",
            "timestamp": "2026-10-25T13:05:00Z",
        }
        ev_unm = extract_native_events(rec_unmeasured, adapter="claude_code")[0]
        d_unm = json.loads(ev_unm["details_json"])
        self.assertIsNone(d_unm["before_tokens"])
        self.assertIsNone(d_unm["after_tokens"])
        self.assertIsNone(d_unm["reduction_tokens"])
        self.assertIsNone(d_unm["reduction_ratio"])
        # Crucial: never default to 0
        self.assertNotEqual(d_unm["before_tokens"], 0)

    def test_compaction_preserves_measured_zero(self) -> None:
        event = extract_native_events(
            {
                "type": "compaction",
                "id": "compact_zero",
                "sessionId": "sess_comp",
                "before_tokens": 0,
                "after_tokens": 0,
            },
            adapter="claude_code",
        )[0]
        details = json.loads(event["details_json"])
        self.assertEqual(details["before_tokens"], 0)
        self.assertEqual(details["after_tokens"], 0)
        self.assertEqual(details["reduction_tokens"], 0)

    def test_compaction_and_tool_result_coexist_on_non_assistant_record(self) -> None:
        events = extract_native_events(
            {
                "type": "tool_result",
                "id": "result_with_compaction",
                "sessionId": "sess_comp_result",
                "tool_use_id": "tool_call_parent",
                "content": "synthetic output",
                "compaction": {
                    "id": "compact_with_result",
                    "before_tokens": 0,
                    "after_tokens": 0,
                },
            },
            adapter="claude_code",
        )
        self.assertEqual(
            {event["event_kind"] for event in events},
            {"compaction", "tool_result"},
        )
        self.assertEqual(
            {event["event_id"] for event in events},
            {"compact_with_result", "result_with_compaction"},
        )
        self.assertNotIn("synthetic output", json.dumps(events))

    def test_non_assistant_events_survive_normalizer_skips(self) -> None:
        records = (
            ("claude_code", {"type": "compaction", "id": "c_claude", "sessionId": "s_claude"}),
            ("claude_code", {"type": "tool_result", "tool_use_id": "t_claude", "sessionId": "s_claude"}),
            ("omp", {"type": "compaction", "id": "c_omp", "sessionId": "s_omp"}),
            ("omp", {"type": "tool_result", "tool_use_id": "t_omp", "sessionId": "s_omp"}),
        )
        for adapter, raw_record in records:
            with self.subTest(adapter=adapter, kind=raw_record["type"]):
                normalized = normalize_observation(
                    raw_record,
                    source_adapter=adapter,
                    file_locator="synthetic-native.jsonl",
                )
                self.assertEqual(normalized.parse_status, "skipped")
                self.assertIsNone(normalized.canonical_request)
                self.assertEqual(len(normalized.events), 1)
                self.assertIsNone(normalized.events[0]["canonical_id"])

    def test_out_of_order_event_parent_is_resolved_on_later_ingest(self) -> None:
        result_path = os.path.join(self.tmp_dir, "result-first.jsonl")
        parent_path = os.path.join(self.tmp_dir, "parent-later.jsonl")
        result_record = {
            "type": "tool_result",
            "timestamp": "2026-10-25T12:00:01Z",
            "sessionId": "sess_ordered",
            "tool_use_id": "toolu_arrives_later",
            "content": "synthetic output",
        }
        with open(result_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(result_record) + "\n")

        first = ingest_file(
            self.conn,
            result_path,
            source_adapter="claude_code",
            origin_harness="claude_code",
            account_alias="acct_native_order",
        )
        self.assertEqual(first.events_inserted, 1)
        result_event_id = "res_toolu_arrives_later"
        unresolved = self.conn.execute(
            "SELECT parent_event_id, details_json FROM events WHERE event_id = ?;",
            (result_event_id,),
        ).fetchone()
        self.assertIsNone(unresolved[0])
        self.assertEqual(
            json.loads(unresolved[1])["unresolved_parent_event_id"],
            "toolu_arrives_later",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM canonical_requests;").fetchone()[0],
            0,
        )

        parent_record = {
            "type": "assistant",
            "timestamp": "2026-10-25T12:00:02Z",
            "requestId": "req_native_order",
            "sessionId": "sess_ordered",
            "account_id": "acct_native_order",
            "message": {
                "id": "msg_native_order",
                "model": "claude-sonnet-5-5-20251001",
                "content": [
                    {"type": "tool_use", "id": "toolu_arrives_later", "name": "read", "input": {}}
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        with open(parent_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(parent_record) + "\n")
        ingest_file(
            self.conn,
            parent_path,
            source_adapter="claude_code",
            origin_harness="claude_code",
            account_alias="acct_native_order",
        )

        resolved = self.conn.execute(
            "SELECT parent_event_id, details_json FROM events WHERE event_id = ?;",
            (result_event_id,),
        ).fetchone()
        self.assertEqual(resolved[0], "toolu_arrives_later")
        self.assertNotIn("unresolved_parent_event_id", json.loads(resolved[1]))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM canonical_requests;").fetchone()[0],
            1,
        )

    def test_namespace_classification_mcp_skills_hooks(self) -> None:
        """Identifies MCP, skill, and hook events strictly from verified prefixes, no inferring."""
        self.assertEqual(classify_event_kind("mcp__brave__search"), "mcp")
        self.assertEqual(classify_event_kind("mcp:postgres:query"), "mcp")
        self.assertEqual(classify_event_kind("skill_build_and_review"), "skill")
        self.assertEqual(classify_event_kind("hook_pre_commit"), "hook")
        self.assertEqual(classify_event_kind("read"), "tool_call")
        self.assertEqual(classify_event_kind("bash"), "tool_call")

    def test_prompt_group_links_preserved_without_bodies(self) -> None:
        """Verifies prompt groups are captured optionally without prompt body contents."""
        rec = {
            "type": "user",
            "timestamp": "2026-10-25T14:00:00Z",
            "sessionId": "sess_prompt_01",
            "promptId": "p_user_101",
            "promptGroup": "auth_refactor",
            "message": {
                "role": "user",
                "content": "Please rewrite the entire user authentication subsystem with OAuth2.",
            },
        }
        prompt_data = extract_prompt_group(rec, adapter="omp")
        self.assertIsNotNone(prompt_data)
        self.assertEqual(prompt_data["prompt_id"], "p_user_101")
        self.assertEqual(prompt_data["prompt_group"], "auth_refactor")
        self.assertEqual(prompt_data["session_id"], "sess_prompt_01")
        # Ensure message content body is NOT in prompt_data
        self.assertNotIn("Please rewrite", json.dumps(prompt_data))

    def test_opt_in_content_fingerprints_gate(self) -> None:
        """Verifies no hashing of content occurs by default; strictly opt-in."""
        rec = {
            "type": "assistant",
            "timestamp": "2026-10-25T14:10:00Z",
            "sessionId": "sess_fp",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_fp_01",
                        "name": "edit",
                        "input": {"path": "main.py", "content": "import sys\nprint('hello')"},
                    }
                ]
            },
        }
        # Default: opt-in disabled -> no content fingerprint
        ev_default = extract_native_events(rec, adapter="claude_code", opt_in_fingerprints=False)[0]
        details_default = json.loads(ev_default["details_json"])
        self.assertNotIn("content_fingerprint", details_default)

        # Opt-in enabled -> fingerprint computed
        ev_optin = extract_native_events(rec, adapter="claude_code", opt_in_fingerprints=True)[0]
        details_optin = json.loads(ev_optin["details_json"])
        self.assertIn("content_fingerprint", details_optin)
        self.assertRegex(details_optin["content_fingerprint"], r"^[a-f0-9]{64}$")

    def test_end_to_end_synthetic_fixture_and_traces_analyze(self) -> None:
        """Ingests native events without request fabrication and queries them through traces.analyze."""
        fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "trace-native.jsonl"
        )
        self.assertTrue(os.path.exists(fixture_path), f"Fixture not found: {fixture_path}")

        result = ingest_file(
            conn=self.conn,
            path=fixture_path,
            source_adapter="claude_code",
            origin_harness="claude_code",
            account_alias="acct_native_root",
        )
        self.assertEqual(result.lines_read, 9)
        self.assertEqual(result.events_inserted, 6)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM canonical_requests;").fetchone()[0],
            3,
        )
        events = self.conn.execute(
            "SELECT event_id, event_kind, canonical_id, parent_event_id, details_json FROM events;"
        ).fetchall()
        self.assertEqual(len(events), 6)
        events_by_id = {row[0]: row for row in events}
        self.assertEqual(events_by_id["res_toolu_native_001"][3], "toolu_native_001")
        self.assertIsNone(events_by_id["res_toolu_native_001"][2])
        self.assertIsNone(events_by_id["compact_native_001"][2])
        self.assertIsNone(events_by_id["compact_native_unmeasured_002"][2])
        persisted_details = "\n".join(row[4] for row in events)
        self.assertNotIn("class AuthMiddleware", persisted_details)
        self.assertNotIn('"issue": 42', persisted_details)

        analysis = analyze(
            self.conn,
            {
                "contract_version": "1",
                "harness_scope": "combined",
                "metric_id": "traces",
                "group_by": ["session"],
            },
        )
        values = analysis["values"]
        self.assertEqual(values["total_requests"], 3)
        self.assertEqual(values["total_sessions"], 2)
        self.assertEqual(values["total_events"], 3)
        self.assertEqual(values["compaction_events_count"], 0)
        self.assertEqual(analysis["source_coverage"]["context_compaction"], "unavailable")
        self.assertEqual(analysis["event_scope"], "filtered_requests")
        self.assertEqual(len(analysis["session_level_events"]), 3)
        self.assertEqual(
            len({event["event_id"] for event in analysis["events"] + analysis["session_level_events"]}),
            6,
        )
        session_analysis = analysis["session_level_analysis"]
        self.assertEqual(session_analysis["compaction"]["compaction_events_count"], 2)
        self.assertEqual(session_analysis["compaction"]["status"], "available")


if __name__ == "__main__":
    unittest.main()
