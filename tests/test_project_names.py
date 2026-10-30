"""Project names: the folder base name is stored as project_name, project_id stays hashed."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from token_ledger.ingest.project_names import backfill_project_names
from token_ledger.ingest.runner import ingest_file
from token_ledger.store import open_database

CWD = "/home/user/Code/token-tracker"
PROJECT_ID = "proj_" + hashlib.sha256(b"token-tracker").hexdigest()[:12]
HEADER_ID = "01a12c4b-9440-71c7-9789-771d8d18c633"
OMP_CWD = "/home/user/omp_project_alpha"
OMP_PROJECT_ID = "proj_" + hashlib.sha256(b"omp_project_alpha").hexdigest()[:12]


def claude_record(req_id: str, cwd: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-10-25T12:00:00Z",
        "requestId": req_id,
        "cwd": cwd,
        "sessionId": "sess_claude_names",
        "message": {
            "id": f"msg_{req_id}",
            "model": "claude-sonnet-5-5-20251001",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        },
    })


def omp_header(cwd: str) -> str:
    return json.dumps({"type": "session", "version": 3, "id": HEADER_ID,
                       "timestamp": "2026-10-25T12:00:00Z", "cwd": cwd})


def omp_message(call_id: str) -> str:
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
            "usage": {"input": 2000, "output": 400, "cacheRead": 600,
                      "cacheWrite": 150, "reasoning": 50, "totalTokens": 3200},
            "duration": 2500,
            "ttft": 500,
        },
    })


class TestProjectNames(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="token_ledger_test_names_")
        self.db_path = os.path.join(self.tmp_dir, "ledger.db")
        self.conn = open_database(self.db_path, run_migrations=True)
        self.root = Path(__file__).resolve().parents[1]
        # Claude Code files live under a projects root so backfill can match their locators.
        self.claude_root = os.path.join(self.tmp_dir, ".claude", "projects")
        os.makedirs(os.path.join(self.claude_root, "-home-user-Code-token-tracker"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write(self, rel_path, lines):
        path = os.path.join(self.tmp_dir, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _project(self, project_id):
        row = self.conn.execute(
            "SELECT project_name, project_alias FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return tuple(row) if row is not None else None

    def _make_legacy(self):
        # Simulates rows written before names existed: project_name equal to project_id.
        self.conn.execute("UPDATE projects SET project_name = project_id")
        self.conn.commit()

    def test_name_is_derived_at_ingest_and_id_stays_hashed(self):
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_n1", CWD + "/")])
        ingest_file(self.conn, path, "claude_code", "work")
        self.assertEqual(self._project(PROJECT_ID), ("token-tracker", PROJECT_ID))
        requests = [row[0] for row in self.conn.execute("SELECT project_id FROM canonical_requests")]
        self.assertEqual(requests, [PROJECT_ID])

    def test_omp_ingest_names_project_from_header_cwd(self):
        path = self._write("omp.jsonl", [omp_header(OMP_CWD), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self.assertEqual(self._project(OMP_PROJECT_ID)[0], "omp_project_alpha")

    def test_no_absolute_path_is_stored_in_projects(self):
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_n2", CWD + "/")])
        ingest_file(self.conn, path, "claude_code", "work")
        rows = self.conn.execute("SELECT project_id, project_name, project_alias, metadata_json FROM projects").fetchall()
        self.assertTrue(rows)
        for row in rows:
            for value in row:
                self.assertNotIn("/", str(value))

    def test_backfill_names_legacy_claude_project_from_its_log_cwd(self):
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_b1", CWD)])
        ingest_file(self.conn, path, "claude_code", "work")
        self._make_legacy()
        summary = backfill_project_names(self.conn, claude_root=self.claude_root)
        self.assertEqual(summary["considered"], 1)
        self.assertEqual(summary["named"], 1)
        self.assertEqual(summary["still_unnamed"], 0)
        self.assertEqual(self._project(PROJECT_ID)[0], "token-tracker")

    def test_backfill_names_legacy_omp_project_from_db_provenance(self):
        path = self._write("omp.jsonl", [omp_header(OMP_CWD), omp_message("a")])
        ingest_file(self.conn, path, "omp", "personal")
        self._make_legacy()
        summary = backfill_project_names(self.conn, claude_root=None)
        self.assertEqual(summary, {"considered": 1, "named": 1, "still_unnamed": 0, "claude_files_matched": 0})
        self.assertEqual(self._project(OMP_PROJECT_ID)[0], "omp_project_alpha")

    def test_backfill_is_idempotent(self):
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_b2", CWD)])
        ingest_file(self.conn, path, "claude_code", "work")
        self._make_legacy()
        first = backfill_project_names(self.conn, claude_root=self.claude_root)
        second = backfill_project_names(self.conn, claude_root=self.claude_root)
        self.assertEqual(first["named"], 1)
        self.assertEqual(second, {"considered": 0, "named": 0, "still_unnamed": 0,
                                  "claude_files_matched": 0})
        self.assertEqual(self._project(PROJECT_ID)[0], "token-tracker")

    def test_user_set_name_is_not_overwritten(self):
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_u1", CWD)])
        ingest_file(self.conn, path, "claude_code", "work")
        self.conn.execute("UPDATE projects SET project_name = 'My Custom Name' WHERE project_id = ?", (PROJECT_ID,))
        self.conn.commit()
        summary = backfill_project_names(self.conn, claude_root=self.claude_root)
        self.assertEqual(summary["considered"], 0)
        self.assertEqual(self._project(PROJECT_ID)[0], "My Custom Name")

    def test_ingest_does_not_overwrite_user_set_name(self):
        self.conn.execute(
            "INSERT INTO projects (project_id, project_name, project_alias, account_alias, is_active, created_at, metadata_json)"
            " VALUES (?, 'Keep Me', ?, 'work', 1, '2026-10-25T00:00:00Z', '{}')",
            (PROJECT_ID, PROJECT_ID),
        )
        self.conn.commit()
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_u2", CWD)])
        ingest_file(self.conn, path, "claude_code", "work")
        self.assertEqual(self._project(PROJECT_ID)[0], "Keep Me")

    def test_cli_refuses_verified_path_and_writes_nothing(self):
        verified_dir = os.path.join(self.tmp_dir, "verified")
        os.makedirs(verified_dir)
        target = os.path.join(verified_dir, "ledger.sqlite3")
        env = {**os.environ, "PYTHONPATH": str(self.root / "src")}
        cmd = [sys.executable, "-m", "token_ledger", "backfill-project-names", "--db", target,
               "--claude-root", self.claude_root]
        result = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("verified", result.stderr)
        self.assertFalse(os.path.exists(target))

    def test_cli_runs_against_a_copy_and_is_idempotent(self):
        path = self._write(".claude/projects/-home-user-Code-token-tracker/s.jsonl",
                           [claude_record("req_c2", CWD)])
        ingest_file(self.conn, path, "claude_code", "work")
        self._make_legacy()
        self.conn.close()
        env = {**os.environ, "PYTHONPATH": str(self.root / "src")}
        cmd = [sys.executable, "-m", "token_ledger", "backfill-project-names", "--db", self.db_path,
               "--claude-root", self.claude_root]
        first = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["named"], 1)
        second = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["named"], 0)
        self.conn = open_database(self.db_path, run_migrations=True)
        self.assertEqual(self._project(PROJECT_ID)[0], "token-tracker")


if __name__ == "__main__":
    unittest.main()
