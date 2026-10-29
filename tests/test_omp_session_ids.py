"""oh-my-pi session id attribution: ingest from the header, resume, and backfill idempotence."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from token_ledger.ingest.runner import ingest_file
from token_ledger.ingest.session_backfill import backfill_omp_session_ids
from token_ledger.store import open_database

HEADER_ID = "01a12c4b-9440-71c7-9789-771d8d18c633"
CLAUDE_SESSION = "sess_claude_100"


def omp_header(session_id: str = HEADER_ID) -> str:
    return json.dumps({
        "type": "session",
        "id": session_id,
        "version": 3,
        "timestamp": "2026-10-25T12:00:00Z",
        "cwd": "/home/user/omp_project",
    })


def omp_message(call_id: str) -> str:
    return json.dumps({
        "type": "message",
        "timestamp": 1792929600000,
        "provider": "google",
        "model": "gemini-2.5-pro",
        "message": {
            "role": "assistant",
            "responseId": f"resp_{call_id}",
            "content": [{"type": "text", "text": "done"}],
            "usage": {
                "input": 2000, "output": 400, "cacheRead": 600,
                "cacheWrite": 150, "reasoning": 50, "totalTokens": 3200,
            },
            "duration": 2500,
            "ttft": 500,
        },
    })


def claude_record(req_id: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-10-25T12:00:00Z",
        "requestId": req_id,
        "cwd": "/home/user/project_test",
        "sessionId": CLAUDE_SESSION,
        "message": {
            "id": f"msg_{req_id}",
            "model": "claude-sonnet-5-5-20251001",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        },
    })


class TestOmpSessionIds(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="token_ledger_test_omp_sid_")
        self.db_path = os.path.join(self.tmp_dir, "ledger.db")
        self.conn = open_database(self.db_path, run_migrations=True)
        self.root = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _request_sessions(self):
        rows = self.conn.execute(
            "SELECT provider_request_id, session_id FROM canonical_requests "
            "WHERE origin_harness = 'oh-my-pi' ORDER BY provider_request_id"
        ).fetchall()
        return {rid: sid for rid, sid in rows}

    def _request_snapshot(self, blank_omp_session: bool):
        """All canonical_requests columns; optionally blank session_id on oh-my-pi rows."""
        cur = self.conn.execute("SELECT * FROM canonical_requests ORDER BY canonical_id")
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            if blank_omp_session and rec["origin_harness"] == "oh-my-pi":
                rec["session_id"] = None
            out.append(rec)
        return out

    def test_resume_from_checkpoint_keeps_header_session_id(self):
        path = os.path.join(self.tmp_dir, "omp.jsonl")
        with open(path, "w") as f:
            f.write(omp_header() + "\n" + omp_message("a") + "\n")

        first = ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(first.status, "ok")
        offset = self.conn.execute("SELECT last_byte_offset FROM source_checkpoints").fetchone()[0]
        self.assertGreater(offset, 0)

        # Append after the checkpoint: the header line is not read on this scan from a fresh start.
        with open(path, "a") as f:
            f.write(omp_message("b") + "\n")
        second = ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(second.status, "ok")

        self.assertEqual(self._request_sessions(), {"resp_a": HEADER_ID, "resp_b": HEADER_ID})
        session_row = self.conn.execute(
            "SELECT origin_harness, status FROM sessions WHERE session_id = ?", (HEADER_ID,)
        ).fetchone()
        self.assertEqual(tuple(session_row), ("oh-my-pi", "completed"))

    def test_backfill_fills_only_null_omp_session_ids_and_is_idempotent(self):
        omp_path = os.path.join(self.tmp_dir, "omp.jsonl")
        with open(omp_path, "w") as f:
            f.write(omp_header() + "\n" + omp_message("a") + "\n" + omp_message("b") + "\n")
        claude_path = os.path.join(self.tmp_dir, "claude.jsonl")
        with open(claude_path, "w") as f:
            f.write(claude_record("req_c1") + "\n")
        ingest_file(self.conn, omp_path, "omp", "personal")
        ingest_file(self.conn, claude_path, "claude_code", "work")
        self.assertEqual(self._request_sessions(), {"resp_a": HEADER_ID, "resp_b": HEADER_ID})

        # Simulate legacy ingest: omp requests without session ids and no omp session rows.
        self.conn.execute("UPDATE canonical_requests SET session_id = NULL WHERE origin_harness = 'oh-my-pi'")
        self.conn.execute("DELETE FROM sessions WHERE origin_harness = 'oh-my-pi'")
        self.conn.commit()
        before = self._request_snapshot(blank_omp_session=True)
        claude_session_before = self.conn.execute(
            "SELECT session_id FROM canonical_requests WHERE origin_harness = 'claude-code'"
        ).fetchall()

        summary = backfill_omp_session_ids(self.conn)
        self.assertEqual(summary["requests_considered"], 2)
        self.assertEqual(summary["requests_updated"], 2)
        self.assertEqual(summary["requests_unresolved"], 0)
        self.assertEqual(summary["sessions_inserted"], 1)
        self.assertEqual(summary["omp_requests_still_null"], 0)
        self.assertEqual(self._request_sessions(), {"resp_a": HEADER_ID, "resp_b": HEADER_ID})
        self.assertEqual(
            tuple(self.conn.execute("SELECT origin_harness FROM sessions WHERE session_id = ?", (HEADER_ID,)).fetchone()),
            ("oh-my-pi",),
        )

        # Only session_id may change: every other column is identical before and after.
        self.assertEqual(self._request_snapshot(blank_omp_session=True), before)
        self.assertEqual(
            self.conn.execute("SELECT session_id FROM canonical_requests WHERE origin_harness = 'claude-code'").fetchall(),
            claude_session_before,
        )

        # Second run is a no-op.
        again = backfill_omp_session_ids(self.conn)
        self.assertEqual(again["requests_considered"], 0)
        self.assertEqual(again["requests_updated"], 0)
        self.assertEqual(again["sessions_inserted"], 0)
        self.assertEqual(self._request_sessions(), {"resp_a": HEADER_ID, "resp_b": HEADER_ID})

    def test_backfill_cli_runs_against_db_file(self):
        omp_path = os.path.join(self.tmp_dir, "omp.jsonl")
        with open(omp_path, "w") as f:
            f.write(omp_header() + "\n" + omp_message("a") + "\n")
        ingest_file(self.conn, omp_path, "omp", "personal")
        self.conn.execute("UPDATE canonical_requests SET session_id = NULL WHERE origin_harness = 'oh-my-pi'")
        self.conn.execute("DELETE FROM sessions WHERE origin_harness = 'oh-my-pi'")
        self.conn.commit()

        env = {**os.environ, "PYTHONPATH": str(self.root / "src")}
        cmd = [sys.executable, "-m", "token_ledger", "backfill-session-ids", "--db", self.db_path]
        first = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["requests_updated"], 1)
        second = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["requests_updated"], 0)
        self.assertEqual(self._request_sessions(), {"resp_a": HEADER_ID})


if __name__ == "__main__":
    unittest.main()
