"""Retention isolation at the real HTTP/report boundary using synthetic ingestion."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from token_ledger.api.server import create_server
from token_ledger.ingest.runner import ingest_file
from token_ledger.store import open_database


class PrivacyScopeHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "ledger.db")
        self.server = create_server(db_path=self.db, host="127.0.0.1", port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        status, headers, body = self.call("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        self.auth = {"Origin": f"http://127.0.0.1:{self.port}",
                     "Cookie": headers["Set-Cookie"].split(";")[0],
                     "X-CSRF-Token": body["csrf_token"]}
        self.source = Path(self.tmp.name) / "synthetic.jsonl"
        records = [{"type": "assistant", "timestamp": "2026-10-27T12:00:00Z",
                    "requestId": f"request-{i}", "sessionId": f"session-{i}",
                    "cwd": f"/home/synthetic/project-{i}",
                    "message": {"id": f"message-{i}", "model": "claude-sonnet-5-5-20251001",
                                "content": [], "usage": {"input_tokens": 1000, "output_tokens": 200}}}
                   for i in (1, 2)]
        self.source_text = "\n".join(json.dumps(record) for record in records) + "\n"
        self.source.write_text(self.source_text)
        conn = open_database(self.db, run_migrations=False)
        try:
            result = ingest_file(conn=conn, path=str(self.source), source_adapter="claude_code",
                                 origin_harness="claude_code", account_alias="synthetic")
            self.assertEqual(result.lines_read, 2)
        finally:
            conn.close()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, method, path, body=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            client.request(method, path, json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json", **getattr(self, "auth", {})})
            response = client.getresponse()
            payload = response.read()
            headers = dict(response.getheaders())
            if "application/json" in headers.get("Content-Type", ""):
                payload = json.loads(payload)
            return response.status, headers, payload
        finally:
            client.close()

    def post(self, path, body):
        status, _, result = self.call("POST", path, body)
        self.assertEqual(status, 200, result)
        return result

    def report(self, name, filters, formats):
        self.post("/api/v1/reports/definitions", {
            "item_id": name, "name": name, "frequency": "manual",
            "filter_state": {"contract_version": "1", "metric_id": "summary", "filters": filters}})
        return self.post("/api/v1/reports/generate", {"report_id": name, "formats": formats})["exports"]

    def test_session_retention_preserves_disjoint_canonical_exports(self):
        formats = ["csv", "json", "html", "pdf", "png"]
        unrelated = self.report("unrelated", {"session": ["session-2"]}, formats)
        affected = self.report("affected", {"session": ["session-1", "session-2"]}, formats)
        unscoped = self.report("unscoped", {}, formats)
        result = self.post("/api/v1/privacy/retention/run", {"session_id": "session-1"})["result"]
        status, _, inventory = self.call("GET", "/api/v1/exports")
        self.assertEqual(status, 200)
        remaining = {item["export_id"] for item in inventory["exports"]}
        print("PRIVACY_HTTP " + json.dumps({"scope": "session-1", "retention": result,
              "unrelated_preserved": all(item["export_id"] in remaining for item in unrelated)}, sort_keys=True))
        for item in unrelated:
            self.assertIn(item["export_id"], remaining)
            self.assertEqual(self.call("GET", f"/api/v1/exports/{item['export_id']}/download")[0], 200)
        for item in affected + unscoped:
            self.assertNotIn(item["export_id"], remaining)
            self.assertFalse(Path(item["file_path"]).exists())
        self.assertEqual(result["deleted_sessions"], 1)
        self.assertEqual(result["deleted_requests"], 1)
        summary = self.post("/api/v1/query", {"contract_version": "1", "metric_id": "summary",
                                             "filters": {"session": ["session-2"]}})
        self.assertEqual(summary["values"]["requests_count"], 1)
        self.assertEqual(self.source.read_text(), self.source_text)

    def test_project_retention_intersects_session_and_project_filters(self):
        projects = []
        for session in ("session-1", "session-2"):
            response = self.post("/api/v1/query", {"contract_version": "1", "metric_id": "projects",
                                                  "filters": {"session": [session]}})
            projects.append(response["rows"][0]["project_id"])
        self.assertNotEqual(*projects)
        preserved = self.report("other-project", {"project": [projects[1]]}, ["json"])
        preserved += self.report("other-session", {"session": ["session-2"]}, ["json"])
        removed = self.report("target-project", {"project": projects}, ["json"])
        removed += self.report("all-history", {}, ["json"])
        result = self.post("/api/v1/privacy/retention/run", {"project_id": projects[0]})["result"]
        status, _, inventory = self.call("GET", "/api/v1/exports")
        self.assertEqual(status, 200)
        remaining = {item["export_id"] for item in inventory["exports"]}
        self.assertEqual(remaining, {item["export_id"] for item in preserved})
        for item in removed:
            self.assertFalse(Path(item["file_path"]).exists())
        for item in preserved:
            self.assertEqual(self.call("GET", f"/api/v1/exports/{item['export_id']}/download")[0], 200)
        self.assertEqual(result["deleted_sessions"], 1)
        self.assertEqual(result["deleted_requests"], 1)
        print("PRIVACY_PROJECT_HTTP " + json.dumps({"deleted_exports": result["deleted_exports"],
              "preserved_exports": len(remaining), "deleted_sessions": result["deleted_sessions"]}))

    def test_unmatched_scope_deletes_no_session_snapshots(self):
        in_scope = self.report("in-scope", {"session": ["session-1"]}, ["json"])
        unrelated = self.report("unrelated", {"session": ["session-2"]}, ["json"])
        result = self.post("/api/v1/privacy/retention/run", {"project_id": "no-such-project"})["result"]
        status, _, inventory = self.call("GET", "/api/v1/exports")
        self.assertEqual(status, 200)
        remaining = {item["export_id"] for item in inventory["exports"]}
        self.assertEqual(result["deleted_exports"], 0)
        self.assertEqual(result["deleted_sessions"], 0)
        for item in in_scope + unrelated:
            self.assertIn(item["export_id"], remaining)
            self.assertTrue(Path(item["file_path"]).exists())
        result = self.post("/api/v1/privacy/retention/run", {"session_id": "session-1"})["result"]
        status, _, inventory = self.call("GET", "/api/v1/exports")
        self.assertEqual(status, 200)
        remaining = {item["export_id"] for item in inventory["exports"]}
        for item in in_scope:
            self.assertNotIn(item["export_id"], remaining)
            self.assertFalse(Path(item["file_path"]).exists())
        for item in unrelated:
            self.assertIn(item["export_id"], remaining)
            self.assertTrue(Path(item["file_path"]).exists())
        self.assertEqual(result["deleted_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
