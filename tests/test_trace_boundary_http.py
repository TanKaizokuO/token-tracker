"""Request-filter boundaries through real HTTP and synthetic native ingestion."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from token_ledger.api.server import create_server
from token_ledger.ingest.runner import ingest_file
from token_ledger.store import open_database


class TraceBoundaryHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ledger.db")
        self.server = create_server(db_path=self.db, host="127.0.0.1", port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, headers, bootstrap = self.call("GET", "/api/v1/bootstrap")
        self.auth = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Cookie": headers["Set-Cookie"].split(";")[0],
            "X-CSRF-Token": bootstrap["csrf_token"],
        }
        conn = open_database(self.db, run_migrations=False)
        try:
            result = ingest_file(
                conn=conn,
                path=str(Path(__file__).parent / "fixtures" / "trace-native.jsonl"),
                source_adapter="claude_code",
                origin_harness="claude_code",
                account_alias="acct_native_root",
            )
            self.assertEqual((result.lines_read, result.events_inserted), (9, 6))
            with conn:
                conn.execute(
                    "UPDATE canonical_requests SET normalized_model = 'selected-model', access_route = 'direct'"
                )
                conn.execute(
                    "UPDATE canonical_requests SET normalized_model = 'excluded-model', "
                    "access_route = 'gateway' WHERE canonical_id = "
                    "(SELECT canonical_id FROM events WHERE event_id = 'toolu_native_002')"
                )
        finally:
            conn.close()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.request(method, path, json.dumps(body) if body is not None else None,
                           {"Content-Type": "application/json", **(headers or {})})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            client.close()

    def query(self, **selection):
        envelope = {"contract_version": "1", "harness_scope": "combined",
                    "metric_id": "traces", "group_by": ["session"], **selection}
        status, _, result = self.call("POST", "/api/v1/query", envelope, self.auth)
        self.assertEqual(status, 200, result)
        print("TRACE_BOUNDARY_HTTP " + json.dumps({
            "selection": selection, "total_requests": result["values"]["total_requests"],
            "total_events": result["values"]["total_events"], "event_scope": result["event_scope"],
            "event_ids": sorted(e["event_id"] for e in result["events"]),
            "session_level_event_ids": sorted(e["event_id"] for e in result["session_level_events"]),
            "session_level_event_scope": result["session_level_event_scope"],
        }, sort_keys=True))
        return result

    def test_session_group_preserves_request_boundary_and_standalone_scope(self):
        for selection in [
            {"time_range": {"start": "2026-10-25T14:00:09Z", "end": "2026-10-25T14:00:12Z", "timezone": "UTC"}},
            {"filters": {"session": ["sess_root_100"], "model": ["selected-model"], "route": ["direct"]}},
        ]:
            with self.subTest(selection=selection):
                result = self.query(**selection)
                self.assertEqual(result["values"]["total_requests"], 1)
                self.assertEqual([e["event_id"] for e in result["events"]], ["toolu_native_001"])
                self.assertEqual(result["values"]["total_events"], 1)
                self.assertEqual(result["event_scope"], "filtered_requests")
                self.assertEqual(result["values"]["mcp_calls_count"], 0)
                self.assertEqual(result["values"]["compaction_events_count"], 0)
                self.assertEqual({e["event_id"] for e in result["session_level_events"]},
                                 {"res_toolu_native_001", "compact_native_001"})
                self.assertEqual(result["session_level_event_scope"], "linked_sessions_unfiltered_by_request_or_time")
                self.assertEqual(result["session_level_analysis"]["compaction"]["compaction_events_count"], 1)
                self.assertTrue(any("excluded from request-scoped metrics" in w for w in result["warnings"]))

    def test_unfiltered_native_events_remain_visible_in_separate_scopes(self):
        result = self.query()
        self.assertEqual(result["values"]["total_requests"], 3)
        self.assertEqual(result["values"]["total_events"], 3)
        self.assertEqual(len(result["session_level_events"]), 3)
        self.assertEqual(len({e["event_id"] for e in result["events"] + result["session_level_events"]}), 6)
        self.assertTrue(all(e["canonical_id"] is not None for e in result["events"]))
        self.assertTrue(all(e["canonical_id"] is None for e in result["session_level_events"]))
        self.assertEqual(result["session_level_analysis"]["compaction"]["compaction_events_count"], 2)


if __name__ == "__main__":
    unittest.main()
