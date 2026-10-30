"""Overlap flags are re-derived from cross-row evidence; same-call duplicates are resolved, not flagged."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from token_ledger.ingest.overlap import recompute_overlap_flags
from token_ledger.query.engine import _build_where_clause, _check_unresolved_overlap
from token_ledger.store import open_database

TS = "2026-10-25T10:00:00Z"


class OverlapRecomputeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="token_ledger_test_overlap_")
        self.db_path = os.path.join(self.tmp_dir, "ledger.db")
        self.conn = open_database(self.db_path, run_migrations=True)
        self.root = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _insert(self, cid, harness, account, provider="anthropic", request_id=None, flag=0, output_tokens=None):
        self.conn.execute(
            """
            INSERT INTO canonical_requests (
                canonical_id, provider, account_alias, origin_harness, provider_request_id,
                access_route, raw_model, normalized_model, request_status, request_timestamp,
                is_unresolved_overlap, created_at
            ) VALUES (?, ?, ?, ?, ?, 'default', 'm', 'm', 'ok', ?, ?, ?);
            """,
            (cid, provider, account, harness, request_id, TS, flag, TS),
        )
        self.conn.execute(
            "INSERT INTO normalized_usage (canonical_id, output_tokens) VALUES (?, ?);",
            (cid, output_tokens),
        )
        self.conn.commit()

    def _flags(self):
        return dict(self.conn.execute("SELECT canonical_id, is_unresolved_overlap FROM canonical_requests").fetchall())

    def _duplicates(self):
        return dict(
            self.conn.execute(
                "SELECT canonical_id, duplicate_of FROM canonical_requests WHERE duplicate_of IS NOT NULL"
            ).fetchall()
        )

    def _notice_fires(self):
        where_sql, params = _build_where_clause({}, table_prefix="c", param_prefix="p_")
        return _check_unresolved_overlap(self.conn, where_sql, params)[0]

    def test_missing_account_alone_is_not_flagged(self):
        self._insert("c1", "claude-code", "anon_claude_code_x", request_id="req_1", flag=1)
        self._insert("c2", "claude-code", "anon_claude_code_y", request_id=None, flag=1)
        summary = recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"c1": 0, "c2": 0})
        self.assertEqual(summary["rows_flagged_before"], 2)
        self.assertEqual(summary["rows_flagged_after"], 0)

    def test_same_request_id_under_two_unverified_accounts_is_deduplicated(self):
        self._insert("o1", "oh-my-pi", "anon_omp_a", request_id="msg_dup", output_tokens=10)
        self._insert("o2", "oh-my-pi", "anon_omp_b", request_id="msg_dup", output_tokens=20)
        self._insert("o3", "oh-my-pi", "anon_omp_c", request_id="msg_other", output_tokens=5)
        summary = recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"o1": 0, "o2": 0, "o3": 0})
        self.assertEqual(self._duplicates(), {"o1": "o2"})
        self.assertEqual(summary["rows_flagged_after"], 0)
        self.assertEqual(summary["same_call_duplicate_rows"], 1)
        self.assertFalse(self._notice_fires())

    def test_pair_is_counted_once_in_summary(self):
        self._insert("o1", "oh-my-pi", "anon_omp_a", request_id="msg_dup", output_tokens=10)
        self._insert("o2", "oh-my-pi", "anon_omp_b", request_id="msg_dup", output_tokens=7)
        recompute_overlap_flags(self.conn)
        where_sql, params = _build_where_clause({}, table_prefix="c", param_prefix="p_")
        count, output = self.conn.execute(
            f"""
            SELECT COUNT(c.canonical_id), SUM(u.output_tokens)
            FROM canonical_requests c LEFT JOIN normalized_usage u ON u.canonical_id = c.canonical_id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(output, 10)

    def test_six_row_group_keeps_max_output_row(self):
        # Streaming snapshots of one call: same harness, provider and request id, different aliases.
        rows = [
            ("g1", "anon_claude_code_1", 5),
            ("g2", "anon_claude_code_2", 40),
            ("g3", "anon_claude_code_3", 12),
            ("g4", "anon_claude_code_4", 40),
            ("g5", "anon_claude_code_5", 3),
            ("g6", "anon_claude_code_6", None),
        ]
        for cid, alias, out in rows:
            self._insert(cid, "claude-code", alias, request_id="req_011Cf6pwaraVq7AovtgQg5F2", output_tokens=out)
        summary = recompute_overlap_flags(self.conn)
        self.assertEqual(set(self._flags().values()), {0})
        # Tie at 40 tokens goes to the smallest canonical_id (g2).
        self.assertEqual(self._duplicates(), {"g1": "g2", "g3": "g2", "g4": "g2", "g5": "g2", "g6": "g2"})
        self.assertEqual(summary["same_call_duplicate_rows"], 5)
        self.assertEqual(summary["rows_flagged_after"], 0)

    def test_cross_harness_same_id_with_distinct_verified_accounts_is_not_flagged(self):
        self._insert("c1", "claude-code", "acct_a", request_id="req_x", flag=1)
        self._insert("o1", "oh-my-pi", "acct_b", request_id="req_x", flag=1)
        recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"c1": 0, "o1": 0})

    def test_cross_harness_same_id_with_one_unverified_account_is_flagged(self):
        self._insert("c1", "claude-code", "acct_a", request_id="req_y")
        self._insert("o1", "oh-my-pi", "anon_omp_z", request_id="req_y")
        recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"c1": 1, "o1": 1})

    def test_cross_provider_same_id_is_flagged(self):
        self._insert("c1", "claude-code", "acct_a", provider="anthropic", request_id="req_cp")
        self._insert("o1", "claude-code", "acct_b", provider="openai", request_id="req_cp")
        recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"c1": 1, "o1": 1})
        self.assertEqual(self._duplicates(), {})
        self.assertTrue(self._notice_fires())

    def test_unknown_provider_is_flagged(self):
        self._insert("u1", "oh-my-pi", "acct_a", provider="unknown", request_id="req_u")
        recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"u1": 1})

    def test_staged_unresolved_parent_is_flagged_until_parent_arrives(self):
        self._insert("child", "claude-code", "acct_a", request_id="req_child", flag=1)
        # Same DDL ingest uses (runner creates this table lazily).
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS staging_unresolved_parent_links (
                child_canonical_id TEXT PRIMARY KEY,
                parent_external_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                account_alias TEXT NOT NULL,
                origin_harness TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.execute(
            """
            INSERT INTO staging_unresolved_parent_links (
                child_canonical_id, parent_external_id, provider, account_alias, origin_harness, created_at
            ) VALUES ('child', 'req_parent', 'anthropic', 'acct_a', 'claude-code', ?);
            """,
            (TS,),
        )
        self.conn.commit()
        recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"child": 1})

        self.conn.execute("DELETE FROM staging_unresolved_parent_links WHERE child_canonical_id = 'child';")
        self.conn.commit()
        recompute_overlap_flags(self.conn)
        self.assertEqual(self._flags(), {"child": 0})

    def test_recompute_is_idempotent(self):
        self._insert("o1", "oh-my-pi", "anon_omp_a", request_id="msg_dup", output_tokens=10)
        self._insert("o2", "oh-my-pi", "anon_omp_b", request_id="msg_dup", output_tokens=20)
        first = recompute_overlap_flags(self.conn)
        second = recompute_overlap_flags(self.conn)
        self.assertEqual(first["rows_flagged_after"], 0)
        self.assertEqual(second["rows_flagged_before"], 0)
        self.assertEqual(second["rows_flagged_after"], 0)
        self.assertEqual(second["same_call_duplicate_rows"], 1)
        self.assertEqual(self._flags(), {"o1": 0, "o2": 0})
        self.assertEqual(self._duplicates(), {"o1": "o2"})

    def test_recompute_cli_runs_against_db_file(self):
        self._insert("o1", "oh-my-pi", "anon_omp_a", request_id="msg_dup", flag=1, output_tokens=5)
        self._insert("o2", "oh-my-pi", "anon_omp_b", request_id="msg_dup", flag=1, output_tokens=9)
        self._insert("c1", "claude-code", "anon_claude_code_q", request_id="req_q", flag=1)
        self.conn.close()
        self.conn = open_database(self.db_path, run_migrations=True)

        env = {**os.environ, "PYTHONPATH": str(self.root / "src")}
        cmd = [sys.executable, "-m", "token_ledger", "recompute-overlap-flags", "--db", self.db_path]
        result = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["rows_flagged_before"], 3)
        self.assertEqual(summary["rows_flagged_after"], 0)
        self.assertEqual(self._flags(), {"o1": 0, "o2": 0, "c1": 0})
        self.assertEqual(self._duplicates(), {"o1": "o2"})

    def test_notice_false_when_no_flags(self):
        self._insert("a1", "claude-code", "acct_a", request_id="req_a", output_tokens=1)
        self._insert("b1", "oh-my-pi", "acct_b", request_id="req_b", output_tokens=1)
        recompute_overlap_flags(self.conn)
        self.assertFalse(self._notice_fires())


if __name__ == "__main__":
    unittest.main()
