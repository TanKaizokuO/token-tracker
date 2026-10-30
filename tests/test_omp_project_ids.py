"""oh-my-pi project attribution: derived from the header cwd like Claude Code, and backfilled."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from token_ledger.ingest.runner import ingest_file
from token_ledger.ingest.session_backfill import backfill_omp_project_ids
from token_ledger.store import open_database

HEADER_ID = "01a12c4b-9440-71c7-9789-771d8d18c633"
CWD = "/home/user/omp_project_alpha"
EXPECTED_PROJECT = "proj_" + hashlib.sha256(b"omp_project_alpha").hexdigest()[:12]


def omp_title() -> str:
    # Real oh-my-pi files start with a title record, and the session header follows it.
    return json.dumps({"type": "title", "v": 1, "title": "Synthetic", "source": "auto",
                       "updatedAt": "2026-10-25T12:00:00.000Z"})


def omp_header(cwd=CWD, session_id: str = HEADER_ID) -> str:
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-10-25T12:00:00Z",
    }
    if cwd is not None:
        header["cwd"] = cwd
    return json.dumps(header)


def omp_message(call_id: str) -> str:
    # Message records carry no cwd of their own; the project comes from the header.
    return json.dumps({
        "type": "message",
        "id": f"evt_{call_id}",
        "timestamp": "2026-10-25T12:00:10Z",
        "message": {
            "role": "assistant",
            "responseId": f"resp_{call_id}",
            "provider": "google",
            "model": "gemini-2.5-pro",
            "content": [{"type": "text", "text": "done"}],
            "usage": {
                "input": 2000, "output": 400, "cacheRead": 600,
                "cacheWrite": 150, "reasoning": 50, "totalTokens": 3200,
            },
            "duration": 2500,
            "ttft": 500,
        },
    })


def claude_record(req_id: str, cwd: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-10-25T12:00:00Z",
        "requestId": req_id,
        "cwd": cwd,
        "sessionId": "sess_claude_200",
        "message": {
            "id": f"msg_{req_id}",
            "model": "claude-sonnet-5-5-20251001",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        },
    })


class TestOmpProjectIds(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="token_ledger_test_omp_proj_")
        self.db_path = os.path.join(self.tmp_dir, "ledger.db")
        self.conn = open_database(self.db_path, run_migrations=True)
        self.root = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write(self, name, lines):
        path = os.path.join(self.tmp_dir, name)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _omp_projects(self):
        rows = self.conn.execute(
            "SELECT provider_request_id, project_id FROM canonical_requests "
            "WHERE origin_harness = 'oh-my-pi' ORDER BY provider_request_id"
        ).fetchall()
        return {rid: pid for rid, pid in rows}

    def _snapshot(self):
        cur = self.conn.execute("SELECT * FROM canonical_requests ORDER BY canonical_id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def test_project_comes_from_header_cwd_after_title_line(self):
        path = self._write("omp.jsonl", [omp_title(), omp_header(), omp_message("a"), omp_message("b")])
        result = ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(result.status, "ok")

        self.assertEqual(self._omp_projects(), {"resp_a": EXPECTED_PROJECT, "resp_b": EXPECTED_PROJECT})
        name = self.conn.execute(
            "SELECT project_name FROM projects WHERE project_id = ?", (EXPECTED_PROJECT,)
        ).fetchone()
        self.assertEqual(name[0], EXPECTED_PROJECT)

    def test_omp_and_claude_use_the_same_project_id_for_the_same_cwd(self):
        omp_path = self._write("omp.jsonl", [omp_title(), omp_header(), omp_message("a")])
        claude_path = self._write("claude.jsonl", [claude_record("req_c1", CWD)])
        ingest_file(self.conn, omp_path, "omp", "personal")
        ingest_file(self.conn, claude_path, "claude_code", "work")

        projects = dict(self.conn.execute(
            "SELECT origin_harness, project_id FROM canonical_requests"
        ).fetchall())
        self.assertEqual(projects, {"oh-my-pi": EXPECTED_PROJECT, "claude-code": EXPECTED_PROJECT})

    def test_header_on_first_line_also_sets_project(self):
        path = self._write("omp.jsonl", [omp_header(), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(self._omp_projects(), {"resp_a": EXPECTED_PROJECT})

    def test_header_without_cwd_leaves_project_null(self):
        path = self._write("omp.jsonl", [omp_title(), omp_header(cwd=None), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(self._omp_projects(), {"resp_a": None})

    def test_resumed_checkpoint_keeps_header_project(self):
        path = self._write("omp.jsonl", [omp_title(), omp_header(), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        with open(path, "a") as f:
            f.write(omp_message("b") + "\n")
        second = ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(second.status, "ok")
        self.assertEqual(self._omp_projects(), {"resp_a": EXPECTED_PROJECT, "resp_b": EXPECTED_PROJECT})

    def _make_legacy_omp_rows(self):
        """Simulate rows ingested before project derivation: NULL project on omp requests and sessions."""
        self.conn.execute("UPDATE canonical_requests SET project_id = NULL WHERE origin_harness = 'oh-my-pi'")
        self.conn.execute("UPDATE sessions SET project_id = NULL WHERE origin_harness = 'oh-my-pi'")
        self.conn.execute("DELETE FROM projects WHERE project_id = ?", (EXPECTED_PROJECT,))
        self.conn.commit()

    def test_backfill_fills_only_null_omp_projects_and_is_idempotent(self):
        omp_path = self._write("omp.jsonl", [omp_title(), omp_header(), omp_message("a"), omp_message("b")])
        claude_path = self._write("claude.jsonl", [claude_record("req_c1", CWD)])
        ingest_file(self.conn, omp_path, "omp", "personal")
        ingest_file(self.conn, claude_path, "claude_code", "work")
        self._make_legacy_omp_rows()
        before = self._snapshot()
        claude_before = [r for r in before if r["origin_harness"] == "claude-code"]

        summary = backfill_omp_project_ids(self.conn)
        self.assertEqual(summary["requests_considered"], 2)
        self.assertEqual(summary["requests_updated"], 2)
        self.assertEqual(summary["requests_unresolved"], 0)
        self.assertEqual(summary["projects_inserted"], 1)
        self.assertEqual(summary["sessions_project_set"], 1)
        self.assertEqual(summary["omp_requests_project_still_null"], 0)
        self.assertEqual(self._omp_projects(), {"resp_a": EXPECTED_PROJECT, "resp_b": EXPECTED_PROJECT})

        # Only project_id may change on omp rows; claude rows are untouched.
        after = self._snapshot()
        expected_after = []
        for row in before:
            if row["origin_harness"] == "oh-my-pi":
                row = dict(row, project_id=EXPECTED_PROJECT)
            expected_after.append(row)
        self.assertEqual(after, expected_after)
        self.assertEqual([r for r in after if r["origin_harness"] == "claude-code"], claude_before)

        again = backfill_omp_project_ids(self.conn)
        self.assertEqual(again["requests_considered"], 0)
        self.assertEqual(again["requests_updated"], 0)
        self.assertEqual(again["projects_inserted"], 0)

    def test_backfill_sets_null_session_project_when_session_row_exists(self):
        path = self._write("omp.jsonl", [omp_title(), omp_header(), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self._make_legacy_omp_rows()

        summary = backfill_omp_project_ids(self.conn)
        self.assertEqual(summary["sessions_project_set"], 1)
        session_project = self.conn.execute(
            "SELECT project_id FROM sessions WHERE session_id = ?", (HEADER_ID,)
        ).fetchone()[0]
        self.assertEqual(session_project, EXPECTED_PROJECT)

    def test_backfill_leaves_rows_without_header_cwd_null(self):
        path = self._write("omp.jsonl", [omp_title(), omp_header(cwd=None), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self.conn.execute("UPDATE canonical_requests SET project_id = NULL WHERE origin_harness = 'oh-my-pi'")
        self.conn.commit()

        summary = backfill_omp_project_ids(self.conn)
        self.assertEqual(summary["requests_considered"], 1)
        self.assertEqual(summary["requests_updated"], 0)
        self.assertEqual(summary["requests_unresolved"], 1)
        self.assertEqual(summary["omp_requests_project_still_null"], 1)
        self.assertEqual(self._omp_projects(), {"resp_a": None})

    def test_backfill_cli_runs_against_db_file(self):
        path = self._write("omp.jsonl", [omp_title(), omp_header(), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self._make_legacy_omp_rows()

        env = {**os.environ, "PYTHONPATH": str(self.root / "src")}
        cmd = [sys.executable, "-m", "token_ledger", "backfill-omp-project-ids", "--db", self.db_path]
        first = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["requests_updated"], 1)
        second = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["requests_updated"], 0)
        self.assertEqual(self._omp_projects(), {"resp_a": EXPECTED_PROJECT})


if __name__ == "__main__":
    unittest.main()
