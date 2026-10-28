"""Tests for incremental ingestion runner, checkpoints, rotation/truncation, and sync health."""

import json
import os
import shutil
import sqlite3
import tempfile
import subprocess
import sys
from pathlib import Path
import unittest
from datetime import datetime, timezone

from token_ledger.ingest.runner import (
    IngestResult,
    SyncHealth,
    get_sync_health,
    ingest_file,
)
from token_ledger.store import open_database


class TestIncrementalIngestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="token_ledger_test_ingest_")
        self.db_path = os.path.join(self.tmp_dir, "test_ledger.db")
        self.conn = open_database(self.db_path, run_migrations=True)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sample_claude_record(self, req_id: str = "req_1", tokens_out: int = 200) -> str:
        data = {
            "type": "assistant",
            "timestamp": "2026-10-25T12:00:00Z",
            "requestId": req_id,
            "cwd": "/home/user/project_test",
            "sessionId": "sess_claude_100",
            "message": {
                "id": f"msg_{req_id}",
                "model": "claude-sonnet-5-5-20251001",
                "content": [
                    {"type": "text", "text": "secret api key sk-ant-1234567890abcdefghijklmnopqrstuvwxyz"},
                    {"type": "tool_use", "id": f"tool_{req_id}", "name": "edit", "input": {"file": "a.txt"}},
                ],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": tokens_out,
                    "cache_read_input_tokens": 500,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 100,
                        "ephemeral_1h_input_tokens": 50,
                    },
                    "reasoning_tokens": 40,
                },
            },
        }
        return json.dumps(data)

    def _sample_omp_record(self, call_id: str = "omp_req_1", dur: int = 2500, ttft: int = 500) -> str:
        data = {
            "type": "message",
            "timestamp": 1792929600000,
            "session": "omp_sess_200",
            "provider": "google",
            "model": "gemini-2.5-pro",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": f"tc_{call_id}", "name": "bash", "arguments": {"cmd": "ls"}},
                ],
                "usage": {
                    "input": 2000,
                    "output": 400,
                    "cacheRead": 600,
                    "cacheWrite": 150,
                    "reasoning": 50,
                    "totalTokens": 3200,
                },
                "duration": dur,
                "ttft": ttft,
            },
        }
        return json.dumps(data)

    def _cli_ingest(self, path, expected_exit=0):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-m", "token_ledger", "ingest", "--db", self.db_path,
             "--file", path, "--adapter", "claude-code", "--account", "synthetic"],
            cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, expected_exit, completed.stderr)
        return json.loads(completed.stdout)

    def test_cli_file_generation_lifecycle(self):
        """Replacement, append, partial completion and truncation preserve every source row."""
        path = os.path.join(self.tmp_dir, "generation.jsonl")
        def write_records(target, *ids, mode="w", newline=True):
            with open(target, mode) as stream:
                stream.write("\n".join(self._sample_claude_record(i) for i in ids))
                if newline:
                    stream.write("\n")
        def checkpoint():
            return self.conn.execute(
                "SELECT file_generation, last_byte_offset, last_line_index, file_device, file_inode "
                "FROM source_checkpoints"
            ).fetchone()
        def assert_totals(count):
            for table in ("canonical_requests", "source_observations", "events"):
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)
        write_records(path, "req_a")
        first = self._cli_ingest(path)
        generation = first["file_generation"]
        for ids in (("req_b",), ("req_c", "req_d")):
            old_size = os.path.getsize(path)
            write_records(path + ".new", *ids)
            self.assertGreaterEqual(os.path.getsize(path + ".new"), old_size)
            os.replace(path + ".new", path)
            result = self._cli_ingest(path)
            self.assertTrue(result["rotation_or_truncation_detected"])
            self.assertNotEqual(result["file_generation"], generation)
            generation = result["file_generation"]
            self.assertEqual(result["lines_read"], len(ids))
            row = checkpoint()
            self.assertEqual(tuple(row[1:]), (
                os.path.getsize(path), len(ids), os.stat(path).st_dev, os.stat(path).st_ino,
            ))
        assert_totals(4)
        write_records(path, "req_e", mode="a")
        appended = self._cli_ingest(path)
        self.assertEqual(appended["file_generation"], generation)
        self.assertEqual(appended["lines_read"], 1)
        complete_offset = checkpoint()[1]
        write_records(path, "req_f", mode="a", newline=False)
        self.assertEqual(self._cli_ingest(path)["lines_read"], 0)
        self.assertEqual(checkpoint()[1], complete_offset)
        with open(path, "a") as stream:
            stream.write("\n")
        self.assertEqual(self._cli_ingest(path)["lines_read"], 1)
        self.assertEqual(checkpoint()[2], 4)
        assert_totals(6)
        write_records(path, "req_g")
        truncated = self._cli_ingest(path)
        self.assertTrue(truncated["rotation_or_truncation_detected"])
        self.assertNotEqual(truncated["file_generation"], generation)
        self.assertEqual(checkpoint()[2], 1)
        assert_totals(7)
        stable = self._cli_ingest(path)
        self.assertEqual((stable["bytes_processed"], stable["events_inserted"]), (0, 0))
        assert_totals(7)
        # Empty and incomplete replacement generations must persist even without a complete line.
        for body in ("", self._sample_claude_record("req_h")):
            old_generation = checkpoint()[0]
            with open(path + ".new", "w") as stream:
                stream.write(body)
            os.replace(path + ".new", path)
            rotated = self._cli_ingest(path)
            self.assertTrue(rotated["rotation_or_truncation_detected"])
            self.assertNotEqual(checkpoint()[0], old_generation)
            self.assertEqual(tuple(checkpoint()[1:3]), (0, 0))
            self.assertEqual(self._cli_ingest(path)["file_generation"], rotated["file_generation"])
        with open(path, "a") as stream:
            stream.write("\n")
        self.assertEqual(self._cli_ingest(path)["lines_read"], 1)
        assert_totals(8)

    def test_cli_legacy_checkpoint_migration_holds_unverified_identity(self):
        path = os.path.join(self.tmp_dir, "legacy.jsonl")
        with open(path, "w") as stream:
            stream.write(self._sample_claude_record("legacy") + "\n")
        self._cli_ingest(path)
        before = tuple(self.conn.execute(
            "SELECT file_generation, last_byte_offset, last_line_index FROM source_checkpoints"
        ).fetchone())
        # Reconstruct the exact pre-006 checkpoint schema without editing old migration files.
        with self.conn:
            self.conn.execute("ALTER TABLE source_checkpoints DROP COLUMN file_device")
            self.conn.execute("ALTER TABLE source_checkpoints DROP COLUMN file_inode")
            self.conn.execute("DELETE FROM schema_migrations WHERE version = 6")
        held = self._cli_ingest(path, expected_exit=1)
        self.assertEqual((held["status"], held["bytes_processed"]), ("error", 0))
        row = self.conn.execute(
            "SELECT file_generation, last_byte_offset, last_line_index, file_device, file_inode, status "
            "FROM source_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(row[:3]), before)
        self.assertEqual(tuple(row[3:]), (None, None, "identity_unverified"))
        self.assertEqual(self._cli_ingest(path, expected_exit=1)["file_generation"], before[0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM canonical_requests").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 6)

    def test_basic_ingest_and_checkpoint_advancement(self):
        log_file = os.path.join(self.tmp_dir, "claude.jsonl")
        rec1 = self._sample_claude_record("req_base_1", 100)
        rec2 = self._sample_claude_record("req_base_2", 200)
        with open(log_file, "w") as f:
            f.write(rec1 + "\n" + rec2 + "\n")

        res = ingest_file(
            conn=self.conn,
            path=log_file,
            source_adapter="claude_code",
            account_alias="work",
        )
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.lines_read, 2)
        self.assertEqual(res.observations_inserted, 2)
        self.assertEqual(res.canonical_requests_inserted, 2)
        self.assertEqual(res.events_inserted, 2)

        # Verify checkpoint in database
        cur = self.conn.cursor()
        cur.execute("SELECT last_byte_offset, last_line_index, status FROM source_checkpoints")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row[0], 0)
        self.assertEqual(row[1], 2)
        self.assertEqual(row[2], "active")

        # Verify no raw absolute paths stored in source_checkpoints or source_observations
        cur.execute("SELECT file_locator FROM source_checkpoints")
        loc_row = cur.fetchone()
        self.assertNotIn("/home/user", loc_row[0])

    def test_idempotent_replay_no_duplicates(self):
        log_file = os.path.join(self.tmp_dir, "omp.jsonl")
        rec = self._sample_omp_record("req_replay_1")
        with open(log_file, "w") as f:
            f.write(rec + "\n")

        # Ingest first time
        res1 = ingest_file(self.conn, log_file, "omp", "personal")
        self.assertEqual(res1.observations_inserted, 1)
        self.assertEqual(res1.canonical_requests_inserted, 1)

        # Ingest second time without file changes (should no-op)
        res2 = ingest_file(self.conn, log_file, "omp", "personal")
        self.assertEqual(res2.lines_read, 0)
        self.assertEqual(res2.observations_inserted, 0)

        # Verify exactly 1 canonical request in database
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests")
        self.assertEqual(cur.fetchone()[0], 1)

    def test_append_incremental_reading(self):
        log_file = os.path.join(self.tmp_dir, "append.jsonl")
        rec1 = self._sample_claude_record("req_app_1")
        with open(log_file, "w") as f:
            f.write(rec1 + "\n")

        res1 = ingest_file(self.conn, log_file, "claude_code")
        self.assertEqual(res1.lines_read, 1)

        # Append second line
        rec2 = self._sample_claude_record("req_app_2")
        with open(log_file, "a") as f:
            f.write(rec2 + "\n")

        res2 = ingest_file(self.conn, log_file, "claude_code")
        self.assertEqual(res2.lines_read, 1)
        self.assertEqual(res2.canonical_requests_inserted, 1)

        # Check total canonical requests is 2
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests")
        self.assertEqual(cur.fetchone()[0], 2)

    def test_incomplete_line_reread(self):
        log_file = os.path.join(self.tmp_dir, "partial.jsonl")
        rec1 = self._sample_claude_record("req_part_1")
        rec2_full = self._sample_claude_record("req_part_2")
        rec2_incomplete = rec2_full[: len(rec2_full) // 2]  # cut in half without newline

        with open(log_file, "w") as f:
            f.write(rec1 + "\n" + rec2_incomplete)

        # Ingest partial file: should only read line 1 and not consume line 2
        res1 = ingest_file(self.conn, log_file, "claude_code")
        self.assertEqual(res1.lines_read, 1)
        self.assertEqual(res1.canonical_requests_inserted, 1)

        # Complete line 2 now
        with open(log_file, "w") as f:
            f.write(rec1 + "\n" + rec2_full + "\n")

        # Ingest again: should read line 2 cleanly
        res2 = ingest_file(self.conn, log_file, "claude_code")
        self.assertEqual(res2.lines_read, 1)
        self.assertEqual(res2.canonical_requests_inserted, 1)

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests")
        self.assertEqual(cur.fetchone()[0], 2)

    def test_truncation_and_rotation_detection(self):
        log_file = os.path.join(self.tmp_dir, "rotating.jsonl")
        rec1 = self._sample_claude_record("req_rot_1")
        rec2 = self._sample_claude_record("req_rot_2")
        with open(log_file, "w") as f:
            f.write(rec1 + "\n" + rec2 + "\n")

        res1 = ingest_file(self.conn, log_file, "claude_code")
        self.assertEqual(res1.lines_read, 2)
        initial_generation = res1.file_generation

        # Truncate / rewrite file with shorter content (simulating logrotate)
        rec3 = self._sample_claude_record("req_rot_3")
        with open(log_file, "w") as f:
            f.write(rec3 + "\n")

        res2 = ingest_file(self.conn, log_file, "claude_code")
        self.assertTrue(res2.rotation_or_truncation_detected)
        self.assertNotEqual(res2.file_generation, initial_generation)
        self.assertEqual(res2.lines_read, 1)
        self.assertEqual(res2.canonical_requests_inserted, 1)

    def test_corrupt_lines_not_silently_dropped(self):
        log_file = os.path.join(self.tmp_dir, "corrupt.jsonl")
        valid1 = self._sample_claude_record("req_valid_1")
        corrupt = "NOT_A_VALID_JSON_LINE {{{ bad syntax"
        valid2 = self._sample_claude_record("req_valid_2")

        with open(log_file, "w") as f:
            f.write(valid1 + "\n" + corrupt + "\n" + valid2 + "\n")

        res = ingest_file(self.conn, log_file, "claude_code")
        self.assertEqual(res.status, "partial")
        self.assertEqual(res.parse_errors, 1)
        self.assertEqual(res.observations_inserted, 3)  # Corrupt line is preserved as observation!
        self.assertEqual(res.canonical_requests_inserted, 2)

        cur = self.conn.cursor()
        cur.execute("SELECT parse_status, error_message FROM source_observations WHERE parse_status = 'corrupt'")
        err_row = cur.fetchone()
        self.assertIsNotNone(err_row)
        self.assertEqual(err_row[0], "corrupt")

    def test_preserve_timing_and_privacy_redaction(self):
        log_file = os.path.join(self.tmp_dir, "timing_priv.jsonl")
        omp_rec = self._sample_omp_record("req_omp_time", dur=2500, ttft=500)
        with open(log_file, "w") as f:
            f.write(omp_rec + "\n")

        res = ingest_file(self.conn, log_file, "omp", "privacy_acct")
        self.assertEqual(res.status, "ok")

        cur = self.conn.cursor()
        # Verify duration and ttft preserved in milliseconds
        cur.execute("SELECT duration_ms, ttft_ms, generation_duration_ms, timing_basis FROM canonical_requests")
        row = cur.fetchone()
        self.assertEqual(row[0], 2500)
        self.assertEqual(row[1], 500)
        self.assertEqual(row[2], 2000)
        self.assertEqual(row[3], "measured")

        # Verify secret redaction in metadata
        cur.execute("SELECT raw_payload_json FROM canonical_requests")
        raw_pl = cur.fetchone()[0]
        self.assertNotIn("sk-ant-", raw_pl)

    def test_sync_health_diagnostics(self):
        log_file = os.path.join(self.tmp_dir, "health.jsonl")
        rec = self._sample_claude_record("req_health_1")
        with open(log_file, "w") as f:
            f.write(rec + "\n")

        ingest_file(self.conn, log_file, "claude_code", "diag_user")

        health_list = get_sync_health(self.conn)
        self.assertEqual(len(health_list), 1)
        h = health_list[0]
        self.assertEqual(h.source_adapter, "claude_code")
        self.assertEqual(h.status, "active")
        self.assertEqual(h.error_count, 0)
        self.assertEqual(h.total_observations, 1)
        self.assertIsNotNone(h.newest_observation_time)

    def test_child_before_parent_unresolved_and_later_link_resolved(self):
        """Verifies no placeholder canonical requests are fabricated when child arrives before parent,
        and link resolves cleanly once real parent observation arrives."""
        file_child = os.path.join(self.tmp_dir, "child_first.jsonl")
        child_rec = json.dumps({
            "type": "message",
            "id": "evt_child_001",
            "timestamp": "2026-10-25T12:00:00Z",
            "cwd": "/home/user/project",
            "account": "acct_test",
            "retryAttempt": 1,
            "retryOf": "req_parent_alpha",
            "message": {
                "role": "assistant",
                "id": "msg_child_001",
                "responseId": "req_child_001",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "usage": {"input": 500, "output": 100},
            },
        })
        with open(file_child, "w") as f:
            f.write(child_rec + "\n")

        # Step 1: Ingest child before parent
        res1 = ingest_file(self.conn, file_child, "omp", "acct_test")
        self.assertEqual(res1.canonical_requests_inserted, 1)

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests")
        self.assertEqual(cur.fetchone()[0], 1)  # EXACTLY 1! No fabricated placeholder parent!

        cur.execute("SELECT canonical_id, parent_request_id, is_unresolved_overlap, raw_payload_json FROM canonical_requests")
        row = cur.fetchone()
        self.assertIsNone(row[1])  # FK is NULL
        self.assertEqual(row[2], 1)  # Marked unresolved
        self.assertIn("req_parent_alpha", row[3])  # Unresolved parent ID in safe metadata

        cur.execute("SELECT COUNT(*) FROM staging_unresolved_parent_links")
        self.assertEqual(cur.fetchone()[0], 1)

        # Step 2: Ingest real parent
        file_parent = os.path.join(self.tmp_dir, "parent_second.jsonl")
        parent_rec = json.dumps({
            "type": "message",
            "id": "evt_parent_001",
            "timestamp": "2026-10-25T11:59:00Z",
            "cwd": "/home/user/project",
            "account": "acct_test",
            "message": {
                "role": "assistant",
                "id": "msg_parent_001",
                "responseId": "req_parent_alpha",
                "model": "gemini-3.8-flash",
                "provider": "google",
                "usage": {"input": 1000, "output": 200},
            },
        })
        with open(file_parent, "w") as f:
            f.write(parent_rec + "\n")

        res2 = ingest_file(self.conn, file_parent, "omp", "acct_test")
        self.assertEqual(res2.canonical_requests_inserted, 1)

        cur.execute("SELECT COUNT(*) FROM canonical_requests")
        self.assertEqual(cur.fetchone()[0], 2)  # EXACTLY 2 requests (child + parent)

        # Child's parent_request_id is now resolved to the parent's canonical_id!
        cur.execute("SELECT canonical_id FROM canonical_requests WHERE provider_request_id = 'req_parent_alpha'")
        parent_canon_id = cur.fetchone()[0]

        cur.execute("SELECT parent_request_id FROM canonical_requests WHERE provider_request_id = 'req_child_001'")
        child_parent_fk = cur.fetchone()[0]
        self.assertEqual(child_parent_fk, parent_canon_id)

        # Staging table is cleared
        cur.execute("SELECT COUNT(*) FROM staging_unresolved_parent_links")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_unknown_pricing_version_remains_unpriced_without_fabrication(self):
        """Verifies unknown pricing version references remain unpriced without fabricating pricing cards."""
        file_unpriced = os.path.join(self.tmp_dir, "unpriced.jsonl")
        rec = json.dumps({
            "type": "assistant",
            "timestamp": "2026-10-25T12:00:00Z",
            "requestId": "req_unpriced_001",
            "cwd": "/home/user/project",
            "account_id": "acct_test",
            "pricing_version_id": "pv_unvalidated_unknown_999",
            "message": {
                "id": "msg_unpriced_001",
                "model": "claude-sonnet-5-5-20251001",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        })
        with open(file_unpriced, "w") as f:
            f.write(rec + "\n")

        ingest_file(self.conn, file_unpriced, "claude_code", "acct_test")

        cur = self.conn.cursor()
        # Verify pricing FK remains NULL
        cur.execute("SELECT pricing_version_id, raw_payload_json FROM canonical_requests WHERE provider_request_id = 'req_unpriced_001'")
        row = cur.fetchone()
        self.assertIsNone(row[0])  # Unpriced: FK is NULL
        self.assertIn("pv_unvalidated_unknown_999", row[1])  # Retained in metadata

        # Verify NO pricing versions or model rates were fabricated
        cur.execute("SELECT COUNT(*) FROM pricing_versions WHERE version_id = 'pv_unvalidated_unknown_999'")
        self.assertEqual(cur.fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
