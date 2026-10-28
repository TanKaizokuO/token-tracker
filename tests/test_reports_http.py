"""Loopback HTTP integration tests for report and export routes."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from token_ledger.api.server import create_server
from token_ledger.ingest.runner import ingest_file
from token_ledger.store.database import open_database


class TestReportHttpApi(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "reports.sqlite3")
        self.server = create_server(self.db_path, host="127.0.0.1", port=0)
        self.port = self.server.server_port
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _request(self, method, path, headers=None, body=None):
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            request_headers.update(headers)
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            if not isinstance(body, bytes):
                request_headers.setdefault("Content-Type", "application/json")
            request_headers["Content-Length"] = str(len(payload))
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _write_headers(self):
        status, headers, body = self._request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        cookie_header = headers.get("set-cookie") or headers.get("Set-Cookie", "")
        return {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Cookie": cookie_header.split(";")[0],
            "X-CSRF-Token": json.loads(body)["csrf_token"],
        }

    def _report(self, item_id, name, frequency):
        return {
            "item_id": item_id,
            "name": name,
            "frequency": frequency,
            "filter_state": {"contract_version": "1", "metric_id": "summary", "filters": {}},
            "threshold_config": {},
            "pricing_assumptions": {},
            "project_id": None,
            "account_alias": "default",
        }

    def test_source_health_projects_ingested_cursors_and_scoped_observations(self):
        native = Path(self.test_dir) / "native.jsonl"
        shutil.copyfile(Path(__file__).parent / "fixtures" / "trace-native.jsonl", native)
        omp = Path(self.test_dir) / "omp.jsonl"
        omp.write_text(json.dumps({
            "type": "message", "id": "health-tool-result", "timestamp": "2026-10-25T14:00:11Z",
            "message": {"role": "toolResult", "toolCallId": "health-call", "toolName": "read",
                        "content": [{"type": "text", "text": "synthetic output"}]},
        }) + "\n{broken\n", encoding="utf-8")
        empty = Path(self.test_dir) / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        connection = open_database(self.db_path, run_migrations=False)
        try:
            result = ingest_file(connection, str(native), source_adapter="claude_code",
                                 account_alias="synthetic", origin_harness="claude_code")
            self.assertEqual(result.lines_read, 9)
            self.assertEqual(result.canonical_requests_inserted, 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 6)
            partial = ingest_file(connection, str(omp), source_adapter="omp")
            self.assertEqual(partial.status, "partial")
            ingest_file(connection, str(empty), source_adapter="omp")
            self.assertEqual(connection.execute(
                "SELECT parse_status FROM source_observations WHERE event_id = ?",
                ("health-tool-result",),
            ).fetchone()[0], "skipped")
            self.assertGreater(connection.execute(
                "SELECT COUNT(*) FROM events WHERE canonical_id IS NULL"
            ).fetchone()[0], 0)
            persisted = {
                row[0]: tuple(row[1:]) for row in connection.execute(
                    "SELECT file_locator, last_line_index, last_byte_offset, file_size "
                    "FROM source_checkpoints"
                )
            }
        finally:
            connection.close()
        status, _, body = self._request("GET", "/health")
        self.assertEqual(status, 200, body)
        status, _, body = self._request(
            "POST", "/api/v1/query", headers=self._write_headers(),
            body={"contract_version": "1", "metric_id": "coverage", "filters": {}},
        )
        self.assertEqual(status, 200, body)
        response = json.loads(body)
        self.assertEqual(response["values"]["live_requests_count"], 3)
        checkpoints = response["checkpoints"]
        self.assertEqual(len(checkpoints), 3)
        for checkpoint in checkpoints:
            self.assertEqual(
                (checkpoint["last_line_index"], checkpoint["last_byte_offset"], checkpoint["file_size"]),
                persisted[checkpoint["file_locator"]],
            )
            self.assertTrue(checkpoint["file_locator"].startswith("loc_"))
            self.assertIsNone(checkpoint["newest_record_timestamp"])
        native_health = next(item for item in checkpoints if item["source_adapter"] == "claude_code")
        self.assertEqual(
            (native_health["last_line_index"], native_health["last_byte_offset"], native_health["file_size"]),
            (9, native.stat().st_size, native.stat().st_size),
        )
        self.assertEqual((native_health["observations_count"], native_health["parsed_count"],
                          native_health["skipped_count"], native_health["unparseable_lines"]), (9, 3, 6, 0))
        partial_health = next(item for item in checkpoints if item["status"] == "partial")
        self.assertEqual((partial_health["observations_count"], partial_health["parsed_count"],
                          partial_health["skipped_count"], partial_health["unparseable_lines"]), (2, 0, 1, 1))
        self.assertEqual(partial_health["error_count"], 1)
        empty_health = next(item for item in checkpoints if item["file_size"] == 0)
        self.assertEqual((empty_health["last_line_index"], empty_health["last_byte_offset"],
                          empty_health["observations_count"], empty_health["parsed_count"]), (0, 0, 0, 0))
        self.assertNotIn(str(native).encode(), body)
        self.assertNotIn(b"synthetic output", body)

    def test_report_crud_generation_scheduler_inventory_and_download(self):
        write_headers = self._write_headers()
        hostile_headers = {**write_headers, "Origin": "http://attacker.invalid"}
        status, _, body = self._request(
            "POST",
            "/api/v1/reports/definitions",
            headers=hostile_headers,
            body=self._report("blocked-report", "Blocked", "manual"),
        )
        self.assertEqual(status, 403, body)
        self.assertEqual(
            json.loads(self._request("GET", "/api/v1/reports/definitions")[2])["reports"],
            [],
        )
        connection = open_database(self.db_path, run_migrations=False)
        try:
            connection.execute(
                "INSERT INTO projects (project_id, project_name, created_at) VALUES (?, ?, ?)",
                ("project-http", "Synthetic HTTP project", "2026-01-01T00:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()

        manual = self._report("http-manual", "HTTP manual report", "manual")
        due = self._report("http-due", "HTTP due report", "daily")
        due["project_id"] = "project-http"
        due["account_alias"] = "scheduled"
        for definition in (manual, due):
            status, _, body = self._request(
                "POST", "/api/v1/reports/definitions", headers=write_headers, body=definition
            )
            self.assertEqual(status, 200, body)
            saved = json.loads(body)["report"]
            self.assertEqual(saved["item_id"], definition["item_id"])
            self.assertEqual(saved["item_type"], "report")
            self.assertIsNotNone(saved["created_at"])
            self.assertIsNotNone(saved["updated_at"])

        status, _, body = self._request("GET", "/api/v1/reports/definitions")
        self.assertEqual(status, 200, body)
        self.assertEqual({item["item_id"] for item in json.loads(body)["reports"]}, {"http-manual", "http-due"})

        status, _, body = self._request(
            "GET",
            "/api/v1/reports/definitions?project_id=project-http&account_alias=scheduled",
        )
        self.assertEqual(status, 200, body)
        self.assertEqual([item["item_id"] for item in json.loads(body)["reports"]], ["http-due"])

        status, _, body = self._request(
            "POST",
            "/api/v1/reports/generate",
            headers=write_headers,
            body={"report_id": "http-manual", "formats": ["json"]},
        )
        self.assertEqual(status, 200, body)
        generated = json.loads(body)
        self.assertEqual(generated["report_id"], "http-manual")
        self.assertEqual([record["export_format"] for record in generated["exports"]], ["json"])
        export_record = generated["exports"][0]
        export_id = export_record["export_id"]
        expected_download = Path(export_record["file_path"]).read_bytes()

        status, headers, downloaded = self._request(
            "GET", f"/api/v1/exports/{export_id}/download"
        )
        self.assertEqual(status, 200, downloaded)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertIn(b"http-manual", downloaded)
        self.assertEqual(downloaded, expected_download)

        status, _, body = self._request("GET", "/api/v1/exports")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(json.loads(body)["exports"]), 1)
        self.assertEqual(json.loads(body)["exports"][0]["export_id"], export_id)

        status, _, body = self._request("GET", "/api/v1/reports/scheduler/due")
        self.assertEqual(status, 200, body)
        self.assertEqual([report["item_id"] for report in json.loads(body)["due_reports"]], ["http-due"])

        status, _, body = self._request(
            "POST", "/api/v1/reports/scheduler/run-due", headers=write_headers, body={}
        )
        self.assertEqual(status, 200, body)
        run = json.loads(body)
        self.assertEqual(run["count"], 1)
        self.assertEqual(run["executed"][0]["report_id"], "http-due")
        self.assertEqual(len(run["executed"][0]["exports"]), 5)

        status, _, body = self._request("GET", "/api/v1/exports?limit=100")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(json.loads(body)["exports"]), 6)

        status, _, body = self._request(
            "DELETE", "/api/v1/reports/definitions/http-manual", headers=write_headers
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted"], "http-manual")
        status, _, body = self._request(
            "DELETE", "/api/v1/reports/definitions/http-manual", headers=write_headers
        )
        self.assertEqual(status, 404, body)
        status, _, body = self._request("GET", "/api/v1/reports/definitions")
        self.assertEqual(status, 200, body)
        self.assertEqual([item["item_id"] for item in json.loads(body)["reports"]], ["http-due"])

    def test_export_download_blocks_path_traversal_and_symlink_records(self):
        write_headers = self._write_headers()
        status, _, body = self._request(
            "POST",
            "/api/v1/reports/definitions",
            headers=write_headers,
            body=self._report("path-test", "Path test", "manual"),
        )
        self.assertEqual(status, 200, body)
        status, _, body = self._request(
            "POST",
            "/api/v1/reports/generate",
            headers=write_headers,
            body={"report_id": "path-test", "formats": ["json"]},
        )
        self.assertEqual(status, 200, body)
        export_id = json.loads(body)["exports"][0]["export_id"]

        status, _, body = self._request(
            "GET", "/api/v1/exports/%2E%2E%2Foutside/download"
        )
        self.assertEqual(status, 404, body)

        outside_path = os.path.join(self.test_dir, "outside.json")
        Path(outside_path).write_bytes(b"outside managed root")
        connection = open_database(self.db_path, run_migrations=False)
        try:
            connection.execute(
                "UPDATE export_inventory SET file_path = ? WHERE export_id = ?",
                (outside_path, export_id),
            )
            connection.commit()
        finally:
            connection.close()
        status, _, body = self._request(
            "GET", f"/api/v1/exports/{export_id}/download"
        )
        self.assertEqual(status, 403, body)
        self.assertNotEqual(body, b"outside managed root")

        symlink_path = os.path.join(self.server.export_root, "outside-link.json")
        os.symlink(outside_path, symlink_path)
        connection = open_database(self.db_path, run_migrations=False)
        try:
            connection.execute(
                "UPDATE export_inventory SET file_path = ? WHERE export_id = ?",
                (symlink_path, export_id),
            )
            connection.commit()
        finally:
            connection.close()
        status, _, body = self._request(
            "GET", f"/api/v1/exports/{export_id}/download"
        )
        self.assertEqual(status, 403, body)


if __name__ == "__main__":
    unittest.main()
