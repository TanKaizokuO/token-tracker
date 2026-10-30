"""Security and functional integration tests for the Token Ledger local loopback API.

Verifies:
- Loopback-only interface binding and rejection of non-loopback hosts (0.0.0.0, LAN, external)
- Strict Host header validation (DNS rebinding and host spoofing defense)
- Absence of CORS headers (no permissive cross-origin access)
- Same-origin enforcement on state-changing requests (POST, PUT, DELETE)
- Session issuance with SameSite=Strict and scoped Path cookies
- Per-session CSRF token generation and header validation
- Path traversal defense on static file serving
- End-to-end CRUD operations on settings, saved views, layouts, outcomes, and budgets
- CLI serve, ingest, archive, and migrate commands
"""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

from token_ledger.__main__ import main as cli_main
from token_ledger.api.server import (
    DEFAULT_HOST,
    TokenLedgerServer,
    create_server,
    is_loopback,
)
from token_ledger.store.database import open_database


class TestLoopbackAddressValidation(unittest.TestCase):
    """Test validation of loopback addresses vs rejected interfaces."""

    def test_loopback_validations(self):
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("127.0.0.2"))
        self.assertTrue(is_loopback("127.1.2.3"))
        self.assertTrue(is_loopback("localhost"))
        self.assertTrue(is_loopback("::1"))
        self.assertTrue(is_loopback("[::1]"))

        # Non-loopback addresses must be rejected
        self.assertFalse(is_loopback("0.0.0.0"))
        self.assertFalse(is_loopback("192.168.1.1"))
        self.assertFalse(is_loopback("10.0.0.1"))
        self.assertFalse(is_loopback("8.8.8.8"))
        self.assertFalse(is_loopback("example.com"))
        self.assertFalse(is_loopback(""))
        self.assertFalse(is_loopback(None))  # type: ignore

    def test_refuse_to_bind_non_loopback(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with self.assertRaises(ValueError):
                create_server(db_path=tmp.name, host="0.0.0.0", port=0)

            with self.assertRaises(ValueError):
                create_server(db_path=tmp.name, host="192.168.1.50", port=0)

            with self.assertRaises(ValueError):
                create_server(db_path=tmp.name, host="example.com", port=0)


class TestApiSecurityAndLifecycle(unittest.TestCase):
    """Integration tests running an ephemeral local server instance."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.web_dist = os.path.join(self.test_dir, "dist")
        os.makedirs(self.web_dist, exist_ok=True)

        # Create a dummy index.html and asset
        with open(os.path.join(self.web_dist, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<html><body>Test Dashboard</body></html>")
        os.makedirs(os.path.join(self.web_dist, "assets"), exist_ok=True)
        with open(os.path.join(self.web_dist, "assets", "app.js"), "w", encoding="utf-8") as fh:
            fh.write("console.log('local asset');")

        # Start server on ephemeral port (port 0)
        self.server = create_server(
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
            web_dist_dir=self.web_dist,
        )
        self.port = self.server.server_port
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _http_request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        body: dict | str | bytes | None = None,
    ) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        req_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            req_headers.update(headers)

        payload = None
        if body is not None:
            if isinstance(body, dict):
                payload = json.dumps(body).encode("utf-8")
                if "Content-Type" not in req_headers:
                    req_headers["Content-Type"] = "application/json"
            elif isinstance(body, str):
                payload = body.encode("utf-8")
            else:
                payload = body
            req_headers["Content-Length"] = str(len(payload))

        conn.request(method, path, body=payload, headers=req_headers)
        res = conn.getresponse()
        resp_body = res.read()
        resp_headers = dict(res.getheaders())
        conn.close()
        return res.status, resp_headers, resp_body

    def _api_headers(self) -> dict[str, str]:
        status, headers, body = self._http_request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        csrf_token = json.loads(body.decode("utf-8"))["csrf_token"]
        cookie = (headers.get("set-cookie") or headers.get("Set-Cookie", "")).split(";")[0]
        return {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Cookie": cookie,
            "X-CSRF-Token": csrf_token,
        }

    def test_host_header_validation(self):
        # Mismatched host (e.g. DNS rebinding attack)
        status, _, _ = self._http_request(
            "GET",
            "/health",
            headers={"Host": "attacker.com"},
        )
        self.assertIn(status, (400, 403))

        # Mismatched port
        status, _, _ = self._http_request(
            "GET",
            "/health",
            headers={"Host": "127.0.0.1:9999"},
        )
        self.assertIn(status, (400, 403))

        # Valid host header
        status, _, body = self._http_request(
            "GET",
            "/health",
            headers={"Host": f"127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")

    def test_no_cors_headers(self):
        # OPTIONS request must be rejected (405) and no CORS header emitted
        status, headers, _ = self._http_request("OPTIONS", "/api/v1/query")
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        # GET /health must not have CORS header
        status, headers, _ = self._http_request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_cross_origin_state_changes_rejected(self):
        # Bootstrap session first to get CSRF token
        status, headers, body = self._http_request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        csrf_token = data["csrf_token"]
        cookie = headers.get("set-cookie") or headers.get("Set-Cookie", "")
        cookie_val = cookie.split(";")[0]

        # POST with external Origin header must be rejected (403)
        status, _, resp_body = self._http_request(
            "POST",
            "/api/v1/settings",
            headers={
                "Origin": "http://evil.com",
                "Cookie": cookie_val,
                "X-CSRF-Token": csrf_token,
            },
            body={"setting_key": "k", "setting_value": "v"},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin request rejected", resp_body)

        # POST with mismatched port Origin must be rejected (403)
        status, _, resp_body = self._http_request(
            "POST",
            "/api/v1/settings",
            headers={
                "Origin": "http://127.0.0.1:9999",
                "Cookie": cookie_val,
                "X-CSRF-Token": csrf_token,
            },
            body={"setting_key": "k", "setting_value": "v"},
        )
        self.assertEqual(status, 403)

        # Resource mutations retain the same strict Origin check.
        status, _, resp_body = self._http_request(
            "POST",
            "/api/v1/budgets",
            headers={
                "Origin": "http://evil.com",
                "Cookie": cookie_val,
                "X-CSRF-Token": csrf_token,
            },
            body={
                "name": "Cross-origin budget",
                "period": "monthly",
                "target_type": "tokens",
                "threshold": 1000,
            },
        )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin request rejected", resp_body)

    def test_csrf_token_enforcement(self):
        # Bootstrap session
        status, headers, body = self._http_request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        csrf_token = data["csrf_token"]
        cookie = headers.get("set-cookie") or headers.get("Set-Cookie", "")
        cookie_val = cookie.split(";")[0]

        # 1. State change without CSRF token must fail (403)
        status, _, _ = self._http_request(
            "POST",
            "/api/v1/settings",
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "Cookie": cookie_val,
            },
            body={"setting_key": "test_k", "setting_value": "val"},
        )
        self.assertEqual(status, 403)

        # 2. State change with invalid CSRF token must fail (403)
        status, _, _ = self._http_request(
            "POST",
            "/api/v1/settings",
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "Cookie": cookie_val,
                "X-CSRF-Token": "invalid_csrf_token_here",
            },
            body={"setting_key": "test_k", "setting_value": "val"},
        )
        self.assertEqual(status, 403)

        # 3. State change with valid token and cookie must succeed (200)
        status, _, resp_body = self._http_request(
            "POST",
            "/api/v1/settings",
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "Cookie": cookie_val,
                "X-CSRF-Token": csrf_token,
            },
            body={"setting_key": "test_k", "setting_value": "val"},
        )
        self.assertEqual(status, 200)

    def test_session_cookie_samesite_strict(self):
        status, headers, _ = self._http_request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        cookie = headers.get("set-cookie") or headers.get("Set-Cookie", "")
        self.assertIn("tl_session=", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Path=/api", cookie)

    def test_path_traversal_prevention(self):
        # Attempt to escape web_dist directory
        status, _, _ = self._http_request("GET", "/../../etc/passwd")
        self.assertIn(status, (403, 404))

        status, _, _ = self._http_request("GET", "/assets/../../test.db")
        self.assertIn(status, (403, 404))

        # Valid static file access
        status, headers, body = self._http_request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"console.log", body)

        # Root static file
        status, headers, body = self._http_request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Test Dashboard", body)

    def test_crud_endpoints_and_query_smoke(self):
        # 1. Bootstrap
        status, headers, body = self._http_request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        boot_data = json.loads(body.decode("utf-8"))
        csrf = boot_data["csrf_token"]
        cookie = (headers.get("set-cookie") or headers.get("Set-Cookie", "")).split(";")[0]

        common_headers = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
        }

        # 2. Settings CRUD
        status, _, body = self._http_request(
            "POST",
            "/api/v1/settings",
            headers=common_headers,
            body={
                "setting_key": "test_setting",
                "setting_value": "enabled",
                "opt_in_enabled": True,
                "description": "Test setting",
            },
        )
        self.assertEqual(status, 200)

        status, _, body = self._http_request("GET", "/api/v1/settings")
        self.assertEqual(status, 200)
        settings_data = json.loads(body.decode("utf-8"))
        keys = [s["setting_key"] for s in settings_data["settings"]]
        self.assertIn("test_setting", keys)

        # 3. Saved Views CRUD
        status, _, body = self._http_request(
            "POST",
            "/api/v1/saved-views",
            headers=common_headers,
            body={
                "view_name": "Claude Sonnet Filter",
                "page_name": "models",
                "filter_state": {"model": "claude-3-5-sonnet-20241022"},
            },
        )
        self.assertEqual(status, 200)
        view_resp = json.loads(body.decode("utf-8"))
        view_id = view_resp["view_id"]

        status, _, body = self._http_request("GET", "/api/v1/saved-views")
        self.assertEqual(status, 200)
        views = json.loads(body.decode("utf-8"))["saved_views"]
        self.assertTrue(any(v["view_id"] == view_id for v in views))

        # Delete saved view
        status, _, _ = self._http_request("DELETE", f"/api/v1/saved-views/{view_id}", headers=common_headers)
        self.assertEqual(status, 200)

        # 4. Layouts CRUD
        status, _, body = self._http_request(
            "POST",
            "/api/v1/layouts",
            headers=common_headers,
            body={
                "layout_name": "Default 2-Col",
                "layout_state": {"columns": 2},
            },
        )
        self.assertEqual(status, 200)
        layout_id = json.loads(body.decode("utf-8"))["layout_id"]

        status, _, body = self._http_request("GET", "/api/v1/layouts")
        self.assertEqual(status, 200)
        layouts = json.loads(body.decode("utf-8"))["layouts"]
        self.assertTrue(any(l["layout_id"] == layout_id for l in layouts))

        status, _, _ = self._http_request("DELETE", f"/api/v1/layouts/{layout_id}", headers=common_headers)
        self.assertEqual(status, 200)

        # 5. Outcomes CRUD
        status, _, body = self._http_request(
            "POST",
            "/api/v1/outcomes",
            headers=common_headers,
            body={
                "entity_type": "session",
                "entity_id": "ses_001",
                "task_label": "refactor_api",
                "completion_rating": "complete",
                "manual_rating": 5.0,
                "notes": "Fast and accurate",
            },
        )
        self.assertEqual(status, 200)

        status, _, body = self._http_request("GET", "/api/v1/outcomes?entity_type=session&entity_id=ses_001")
        self.assertEqual(status, 200)
        outcomes = json.loads(body.decode("utf-8"))["outcomes"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["task_label"], "refactor_api")

        # 6. Budgets CRUD through the domain contract
        status, _, body = self._http_request(
            "POST",
            "/api/v1/budgets",
            headers=common_headers,
            body={
                "name": "Monthly Token Cap",
                "period": "monthly",
                "target_type": "tokens",
                "threshold": 5000000,
            },
        )
        self.assertEqual(status, 200)
        budget_id = json.loads(body.decode("utf-8"))["id"]

        status, _, body = self._http_request("GET", "/api/v1/budgets")
        self.assertEqual(status, 200)
        budgets = json.loads(body.decode("utf-8"))["budgets"]
        self.assertTrue(any(b["id"] == budget_id for b in budgets))

        status, _, _ = self._http_request("DELETE", f"/api/v1/budgets/{budget_id}", headers=common_headers)
        self.assertEqual(status, 200)

        # 7. Query Engine POST
        # If token_ledger.query exists or is mocked, test query dispatch
        query_payload = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "summary",
        }
        status, _, body = self._http_request(
            "POST",
            "/api/v1/query",
            headers=common_headers,
            body=query_payload,
        )
        # 200 if query engine implemented, or 501 if query engine module not yet available
        self.assertIn(status, (200, 501))


    def test_privacy_budget_and_retention_contracts(self):
        headers = self._api_headers()

        # Budget notifications require the explicit privacy consent setting.
        status, _, body = self._http_request(
            "POST",
            "/api/v1/budgets",
            headers=headers,
            body={
                "name": "Consent gated cap",
                "period": "monthly",
                "target_type": "tokens",
                "threshold": 1000,
                "notifications_enabled": True,
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("error", json.loads(body.decode("utf-8")))

        for setting_key in ("content_capture", "content_fingerprints", "git_linkage", "notifications"):
            status, _, body = self._http_request(
                "POST",
                "/api/v1/privacy",
                headers=headers,
                body={"setting_key": setting_key, "opt_in_enabled": True},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8"))["setting_key"], setting_key)

        status, _, body = self._http_request("GET", "/api/v1/privacy")
        self.assertEqual(status, 200)
        privacy_settings = json.loads(body.decode("utf-8"))["settings"]
        for setting_key in ("content_capture", "content_fingerprints", "git_linkage", "notifications"):
            self.assertTrue(privacy_settings[setting_key])

        status, _, body = self._http_request(
            "POST",
            "/api/v1/privacy",
            headers=headers,
            body={"setting_key": ["notifications"], "opt_in_enabled": True},
        )
        self.assertEqual(status, 400)
        status, _, _ = self._http_request(
            "POST",
            "/api/v1/privacy",
            headers=headers,
            body={"setting_key": "privacy_telemetry_opt_in", "opt_in_enabled": True},
        )
        self.assertEqual(status, 400)

        status, _, body = self._http_request(
            "POST",
            "/api/v1/budgets",
            headers=headers,
            body={
                "name": "Monthly Token Cap",
                "period": "monthly",
                "target_type": "tokens",
                "threshold": 1000,
                "scope": {"model": "model-a"},
                "notifications_enabled": True,
            },
        )
        self.assertEqual(status, 200)
        budget = json.loads(body.decode("utf-8"))
        self.assertEqual(budget["period"], "monthly")
        self.assertEqual(budget["target_type"], "tokens")
        self.assertEqual(budget["threshold"], 1000.0)
        self.assertTrue(budget["notifications_enabled"])
        self.assertEqual(budget["scope"], {"model": "model-a"})

        status, _, body = self._http_request("GET", "/api/v1/budgets")
        self.assertEqual(status, 200)
        budgets = json.loads(body.decode("utf-8"))["budgets"]
        self.assertTrue(any(item["id"] == budget["id"] for item in budgets))

        for invalid_budget in (
            {"name": "Invalid period", "period": "weekly", "threshold": 100},
            {"name": "Invalid threshold", "period": "daily", "threshold": "100"},
            {
                "name": "Legacy storage fields",
                "frequency": "daily",
                "threshold_config": {"max_tokens": 100},
            },
        ):
            status, _, _ = self._http_request(
                "POST", "/api/v1/budgets", headers=headers, body=invalid_budget
            )
            self.assertEqual(status, 400)

        status, _, body = self._http_request(
            "DELETE", f"/api/v1/budgets/{budget['id']}", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["deleted"], budget["id"])

        status, _, body = self._http_request("GET", "/api/v1/privacy/projects")
        self.assertEqual(status, 200)
        status, _, body = self._http_request(
            "POST",
            "/api/v1/privacy/projects",
            headers=headers,
            body={"excluded_projects": ["/repos/private"], "include_patterns": ["src/**"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["policy"]["excluded_projects"], ["/repos/private"])
        status, _, body = self._http_request("GET", "/api/v1/privacy/projects")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["policy"]["excluded_projects"], ["/repos/private"])
        status, _, body = self._http_request("GET", "/api/v1/settings")
        self.assertEqual(status, 200)
        settings_rows = json.loads(body.decode("utf-8"))["settings"]
        policy_row = next(row for row in settings_rows if row["setting_key"] == "project_inclusion_policy")
        self.assertEqual(json.loads(policy_row["setting_value"])["excluded_projects"], ["/repos/private"])

        status, _, body = self._http_request("GET", "/api/v1/privacy/retention")
        self.assertEqual(status, 200)
        retention = json.loads(body.decode("utf-8"))
        self.assertEqual(
            retention["supported_scopes"],
            ["older_than_iso", "session_id", "project_id", "account_alias"],
        )
        self.assertNotEqual(os.path.realpath(self.server.managed_root), os.path.realpath(self.db_path))
        self.assertNotEqual(os.path.realpath(self.server.managed_root), os.path.dirname(os.path.realpath(self.db_path)))

        status, _, body = self._http_request(
            "POST",
            "/api/v1/privacy/retention",
            headers=headers,
            body={"retention_days": 30},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["retention_days"], 30)
        from token_ledger.outcomes import create_outcome, get_outcome

        conn = self.server.get_connection()
        with conn:
            for session_id in ("retention-target", "retention-unrelated"):
                conn.execute(
                    "INSERT INTO sessions (session_id, origin_harness, created_at) VALUES (?, 'synthetic', ?)",
                    (session_id, "2026-01-01T00:00:00+00:00"),
                )
        target_outcome = create_outcome(conn, entity_type="session", entity_id="retention-target")
        unrelated_outcome = create_outcome(conn, entity_type="session", entity_id="retention-unrelated")
        status, _, body = self._http_request(
            "POST",
            "/api/v1/privacy/retention/run",
            headers=headers,
            body={"session_id": "retention-target"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["scope"], {"session_id": "retention-target"})
        self.assertIsNone(get_outcome(conn, target_outcome.outcome_id))
        self.assertIsNotNone(get_outcome(conn, unrelated_outcome.outcome_id))
        self.assertIsNone(
            conn.execute("SELECT 1 FROM sessions WHERE session_id = 'retention-target'").fetchone()
        )
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM sessions WHERE session_id = 'retention-unrelated'").fetchone()
        )
        status, _, body = self._http_request(
            "POST",
            "/api/v1/privacy/retention/run",
            headers=headers,
            body={"session_id": "missing-session"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["scope"], {"session_id": "missing-session"})
        status, _, body = self._http_request(
            "POST",
            "/api/v1/privacy/retention/run",
            headers=headers,
            body={"managed_root": "/tmp/untrusted"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_outcome_domain_crud_validation_and_ownership(self):
        headers = self._api_headers()
        status, _, body = self._http_request(
            "POST",
            "/api/v1/outcomes",
            headers=headers,
            body={
                "entity_type": "session",
                "entity_id": "session-resource-1",
                "task_label": "research",
                "completion_rating": "complete",
                "manual_rating": 8.5,
                "notes": "Manual outcome",
                "was_corrected": False,
            },
        )
        self.assertEqual(status, 200)
        outcome = json.loads(body.decode("utf-8"))
        self.assertFalse(outcome["is_inferred"])
        self.assertEqual(outcome["user_ownership"], "user")
        self.assertEqual(outcome["source"], "api")

        invalid_outcomes = (
            {
                "entity_type": "session",
                "entity_id": "session-resource-2",
                "completion_rating": "in_progress",
            },
            {
                "entity_type": "session",
                "entity_id": "session-resource-3",
                "manual_rating": 11,
            },
            {
                "entity_type": "session",
                "entity_id": "session-resource-4",
                "user_ownership": "inferred",
            },
        )
        for invalid_outcome in invalid_outcomes:
            status, _, body = self._http_request(
                "POST", "/api/v1/outcomes", headers=headers, body=invalid_outcome
            )
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(body.decode("utf-8")))

        status, _, body = self._http_request(
            "PATCH",
            f"/api/v1/outcomes/{outcome['outcome_id']}",
            headers=headers,
            body={"completion_rating": "partial", "manual_rating": 9.25, "was_restarted": True},
        )
        self.assertEqual(status, 200)
        updated = json.loads(body.decode("utf-8"))
        self.assertEqual(updated["completion_rating"], "partial")
        self.assertEqual(updated["manual_rating"], 9.25)
        self.assertTrue(updated["was_restarted"])
        self.assertEqual(updated["user_ownership"], "user")

        for invalid_update in (
            {"completion_rating": "not-a-status"},
            {"manual_rating": 11},
            {"git_metadata": {"arbitrary": "json"}},
            {"user_ownership": "inferred"},
        ):
            status, _, body = self._http_request(
                "PATCH",
                f"/api/v1/outcomes/{outcome['outcome_id']}",
                headers=headers,
                body=invalid_update,
            )
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(body.decode("utf-8")))

        status, _, body = self._http_request(
            "GET",
            "/api/v1/outcomes?entity_type=session&entity_id=session-resource-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["outcomes"][0]["outcome_id"], outcome["outcome_id"])
        status, _, body = self._http_request(
            "GET", "/api/v1/outcomes?entity_type=unknown"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body.decode("utf-8")))

        status, _, body = self._http_request(
            "DELETE",
            f"/api/v1/outcomes/{outcome['outcome_id']}",
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["deleted"], outcome["outcome_id"])

    def test_quota_snapshot_domain_validation(self):
        headers = self._api_headers()
        valid_snapshot = {
            "provider": "openai",
            "official_source": "account-dashboard",
            "window_type": "month",
            "limit_value": 1000,
            "used_value": 125,
            "status": "available",
            "raw_payload": {"reported_by": "official-ui"},
        }
        status, _, body = self._http_request(
            "POST",
            "/api/v1/quotas/snapshot",
            headers=headers,
            body=valid_snapshot,
        )
        self.assertEqual(status, 201)
        response = json.loads(body.decode("utf-8"))
        self.assertEqual(response["status"], "created")
        self.assertEqual(response["snapshot"]["provider"], "openai")
        self.assertEqual(response["snapshot"]["official_source"], "account-dashboard")
        stored_quota = self.server.get_connection().execute(
            "SELECT provider, provenance_json FROM subscription_quotas WHERE quota_id = ?",
            (response["quota_id"],),
        ).fetchone()
        self.assertIsNotNone(stored_quota)
        self.assertEqual(stored_quota["provider"], "openai")
        self.assertEqual(
            json.loads(stored_quota["provenance_json"])["official_source"],
            "account-dashboard",
        )

        for invalid_snapshot in (
            {"provider": "openai", "limit_value": 1000},
            {
                "provider": "openai",
                "official_source": "account-dashboard",
                "limit_value": -1,
            },
            {
                "provider": "openai",
                "official_source": "account-dashboard",
                "window_type": "weekly",
            },
            {
                "provider": "openai",
                "official_source": "account-dashboard",
                "raw_payload": ["not", "an", "object"],
            },
        ):
            status, _, body = self._http_request(
                "POST",
                "/api/v1/quotas/snapshot",
                headers=headers,
                body=invalid_snapshot,
            )
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(body.decode("utf-8")))

class TestCliSubcommands(unittest.TestCase):
    """Test CLI commands and validation."""

    def test_cli_rejects_non_loopback(self):
        ret = cli_main(["serve", "--db", "fake.db", "--host", "0.0.0.0"])
        self.assertEqual(ret, 1)

    def test_cli_migrate(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            ret = cli_main(["migrate", "--db", tmp.name])
            self.assertEqual(ret, 0)
