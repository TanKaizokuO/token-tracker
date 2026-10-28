"""Local Loopback HTTP API server for Token Ledger.

Implements a secure, local-only HTTP service binding exclusively to loopback
addresses (127.0.0.1, ::1, localhost). Enforces strict Host header validation,
SameSite=Strict scoped session cookies, Same-Origin checking on state-changing
requests, and per-session CSRF token validation.

Provides:
- GET /health, GET /api/v1/health
- GET /api/v1/bootstrap (session & CSRF token issuance)
- POST /api/v1/query (read-only query engine integration)
- Settings CRUD: GET/POST /api/v1/settings and /api/v1/privacy
- Saved Views CRUD: GET/POST/DELETE /api/v1/saved-views
- Layouts CRUD: GET/POST/DELETE /api/v1/layouts
- Outcomes CRUD: GET/POST/PATCH/DELETE /api/v1/outcomes
- Budgets CRUD: GET/POST/DELETE /api/v1/budgets
- Privacy, quota snapshot, and explicitly scoped retention APIs
- Static local bundled UI serving from web/dist (zero CDN/network requests)
"""

from __future__ import annotations

import http.cookies
import http.server
import ipaddress
import json
import math
import mimetypes
import os
import secrets
import socket
import sqlite3
import stat
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from token_ledger.store.database import get_schema_version, open_database

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SERVICE_VERSION = "1.0.0"

def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False

def _pricing_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{field} must be a non-empty string of at most 512 characters")
    return value.strip()


def _pricing_timestamp(value: Any, field: str) -> str:
    text = _pricing_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _validate_pricing_version(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict) or set(body) - {
        "version_id", "effective_start", "effective_end", "currency_code",
        "source_provenance", "rates", "model_mappings",
    }:
        raise ValueError("Invalid pricing version fields")
    version_id = _pricing_text(body.get("version_id"), "version_id")
    start = _pricing_timestamp(body.get("effective_start"), "effective_start")
    end = _pricing_timestamp(body["effective_end"], "effective_end") if body.get("effective_end") is not None else None
    if end and datetime.fromisoformat(end) <= datetime.fromisoformat(start):
        raise ValueError("effective_end must follow effective_start")
    currency = _pricing_text(body.get("currency_code", "USD"), "currency_code").upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ValueError("currency_code must be a three-letter ASCII code")
    provenance = body.get("source_provenance", {})
    if not isinstance(provenance, dict) or "model_mappings" in provenance:
        raise ValueError("source_provenance must be an object without model_mappings")
    rates = body.get("rates")
    if not isinstance(rates, list) or not 1 <= len(rates) <= 1000:
        raise ValueError("rates must be an array of 1 to 1000 entries")
    numeric_fields = {"input_rate_per_m", "output_rate_per_m", "cache_read_rate_per_m",
                      "cache_write_5m_rate_per_m", "cache_write_1h_rate_per_m"}
    allowed = numeric_fields | {"provider", "model_identity", "access_route", "is_tiered",
                                "prompt_threshold", "tiered_rates_json", "status", "source_url", "verified_on", "note"}
    normalized = []
    rate_keys = set()
    for rate in rates:
        if not isinstance(rate, dict) or set(rate) - allowed:
            raise ValueError("Invalid rate fields")
        rate = dict(rate)
        for key in ("provider", "model_identity"):
            rate[key] = _pricing_text(rate.get(key), key)
        rate["provider"] = rate["provider"].lower()
        rate["access_route"] = _pricing_text(rate.get("access_route", "default"), "access_route").lower()
        key = (rate["provider"], rate["model_identity"], rate["access_route"])
        if key in rate_keys:
            raise ValueError("Duplicate provider/model/route rate")
        rate_keys.add(key)
        for name in numeric_fields:
            value = rate.get(name)
            if (name in {"input_rate_per_m", "output_rate_per_m"} or value is not None) and (
                not _is_finite_number(value) or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")
        if rate.get("status", "verified") not in ("verified", "estimated", "unverified"):
            raise ValueError("status must be verified, estimated, or unverified")
        if not isinstance(rate.get("is_tiered", False), bool):
            raise ValueError("is_tiered must be boolean")
        threshold = rate.get("prompt_threshold")
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0):
            raise ValueError("prompt_threshold must be a non-negative integer")
        tier = rate.get("tiered_rates_json", {})
        if not isinstance(tier, dict) or set(tier) - numeric_fields:
            raise ValueError("tiered_rates_json must be an object of supported rate fields")
        if any(not _is_finite_number(value) or value < 0 for value in tier.values()):
            raise ValueError("Tier rates must be finite non-negative numbers")
        if rate.get("is_tiered", False) and (threshold is None or not tier):
            raise ValueError("Tiered rates require prompt_threshold and tiered_rates_json")
        for name in ("source_url", "verified_on", "note"):
            if rate.get(name) is not None:
                rate[name] = _pricing_text(rate[name], name)
        normalized.append(rate)
    mappings = body.get("model_mappings", [])
    if not isinstance(mappings, list) or len(mappings) > 1000:
        raise ValueError("model_mappings must be an array of at most 1000 entries")
    clean_mappings = []
    aliases = set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) - {"provider", "access_route", "alias", "model_identity", "display_name"}:
            raise ValueError("Invalid model mapping fields")
        item = {key: _pricing_text(mapping.get(key), key) for key in ("provider", "alias", "model_identity")}
        item["provider"] = item["provider"].lower()
        item["access_route"] = _pricing_text(mapping.get("access_route", "default"), "access_route").lower()
        if mapping.get("display_name") is not None:
            item["display_name"] = _pricing_text(mapping["display_name"], "display_name")
        alias_key = (item["provider"], item["access_route"], item["alias"].lower())
        if alias_key in aliases:
            raise ValueError("Duplicate provider/route/alias mapping")
        aliases.add(alias_key)
        if (item["provider"], item["model_identity"], item["access_route"]) not in rate_keys:
            raise ValueError("Each mapping must reference a rate with the same provider and route")
        clean_mappings.append(item)
    return {"version_id": version_id, "rates": normalized, "effective_start": start,
            "effective_end": end, "currency_code": currency,
            "source_provenance": {**provenance, "model_mappings": clean_mappings}}


def is_loopback(host: str) -> bool:
    """Check if host string resolves strictly and exclusively to loopback addresses.

    Rejects non-loopback addresses such as 0.0.0.0, public IPs, LAN IPs, and arbitrary
    hostnames that resolve to external addresses.
    """
    if not host or not isinstance(host, str):
        return False

    clean_host = host.strip().lower()
    if clean_host.startswith("[") and clean_host.endswith("]"):
        clean_host = clean_host[1:-1]

    if clean_host in {"localhost", "localhost.localdomain"}:
        return True

    try:
        ip = ipaddress.ip_address(clean_host)
        return ip.is_loopback
    except ValueError:
        pass

    try:
        addr_info = socket.getaddrinfo(clean_host, None)
        if not addr_info:
            return False
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if ip_str.startswith("[") and ip_str.endswith("]"):
                ip_str = ip_str[1:-1]
            ip = ipaddress.ip_address(ip_str)
            if not ip.is_loopback:
                return False
        return True
    except Exception:
        return False


class SessionStore:
    """Thread-safe store for local session IDs and their corresponding CSRF tokens."""

    def __init__(self, ttl_seconds: int = 86400):
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._ttl_seconds = ttl_seconds

    def create_session(self) -> Tuple[str, str]:
        with self._lock:
            self._purge_expired()
            session_id = secrets.token_hex(16)
            csrf_token = secrets.token_hex(32)
            self._sessions[session_id] = {
                "csrf_token": csrf_token,
                "created_at": time.time(),
            }
            return session_id, csrf_token

    def get_or_create_csrf(self, session_id: Optional[str]) -> Tuple[str, str]:
        with self._lock:
            self._purge_expired()
            if session_id and session_id in self._sessions:
                return session_id, self._sessions[session_id]["csrf_token"]
            new_session_id = secrets.token_hex(16)
            new_csrf_token = secrets.token_hex(32)
            self._sessions[new_session_id] = {
                "csrf_token": new_csrf_token,
                "created_at": time.time(),
            }
            return new_session_id, new_csrf_token

    def validate_session_and_csrf(self, session_id: Optional[str], csrf_token: Optional[str]) -> bool:
        if not session_id or not csrf_token:
            return False
        with self._lock:
            self._purge_expired()
            session = self._sessions.get(session_id)
            if not session:
                return False
            expected_csrf = session.get("csrf_token")
            if not expected_csrf:
                return False
            return secrets.compare_digest(expected_csrf, csrf_token)

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [sid for sid, data in self._sessions.items() if now - data["created_at"] > self._ttl_seconds]
        for sid in expired:
            del self._sessions[sid]


class TokenLedgerServer(http.server.ThreadingHTTPServer):
    """Threading HTTPServer restricted to loopback with DB and session management."""

    def __init__(
        self,
        server_address: Tuple[str, int],
        RequestHandlerClass: Any,
        db_path: str,
        web_dist_dir: Optional[str] = None,
        managed_root: Optional[str] = None,
        export_root: Optional[str] = None,
    ):
        host, port = server_address
        if not is_loopback(host):
            raise ValueError(f"Refusing to bind non-loopback host: {host}")

        self.db_path = db_path
        self.web_dist_dir = os.path.abspath(web_dist_dir) if web_dist_dir else None
        database_dir = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else os.getcwd()
        self.managed_root = os.path.realpath(
            managed_root or os.path.join(database_dir, ".token-ledger-managed")
        )
        self.export_root = os.path.realpath(
            export_root or os.path.join(self.managed_root, "exports")
        )
        if (
            self.export_root == self.managed_root
            or not self.export_root.startswith(self.managed_root + os.sep)
        ):
            raise ValueError("export_root must be a child of managed_root")
        os.makedirs(self.managed_root, mode=0o700, exist_ok=True)
        os.makedirs(self.export_root, mode=0o700, exist_ok=True)
        self.session_store = SessionStore()
        self.bound_host = host
        self._thread_local = threading.local()

        # Ensure database is initialized with current migrations
        init_conn = open_database(db_path, run_migrations=True)
        init_conn.close()

        super().__init__(server_address, RequestHandlerClass)

    def get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._thread_local, "conn") or self._thread_local.conn is None:
            self._thread_local.conn = open_database(self.db_path, run_migrations=False)
        return self._thread_local.conn

    def close_thread_connection(self) -> None:
        if hasattr(self._thread_local, "conn") and self._thread_local.conn is not None:
            try:
                self._thread_local.conn.close()
            except Exception:
                pass
            self._thread_local.conn = None


class TokenLedgerRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler implementing loopback security and API routing."""

    server: TokenLedgerServer

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid noisy console output during test runs
        pass

    # -------------------------------------------------------------------------
    # Security Validation
    # -------------------------------------------------------------------------

    def _validate_host_header(self) -> bool:
        host_hdr = self.headers.get("Host")
        if not host_hdr:
            self._send_json(400, {"error": "Missing Host header"})
            return False
        if any(char.isspace() for char in host_hdr):
            self._send_json(400, {"error": "Malformed Host header"})
            return False

        try:
            parsed = urllib.parse.urlsplit(f"//{host_hdr}")
            hdr_host = parsed.hostname or ""
            hdr_port = parsed.port
        except ValueError:
            self._send_json(400, {"error": "Malformed Host header"})
            return False

        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            self._send_json(400, {"error": "Malformed Host header"})
            return False
        if not hdr_host or hdr_port is None or not 1 <= hdr_port <= 65535:
            self._send_json(400, {"error": "Host header must include the bound port"})
            return False
        if not is_loopback(hdr_host):
            self._send_json(403, {"error": "Forbidden Host header: non-loopback"})
            return False
        if hdr_port != self.server.server_port:
            self._send_json(403, {"error": "Host header port mismatch"})
            return False
        return True

    def _validate_same_origin_and_csrf(self) -> bool:
        """Require a same-host loopback Origin/Referer and a session-bound CSRF token."""
        host_header = self.headers.get("Host", "")
        try:
            request_host = urllib.parse.urlsplit(f"//{host_header}").hostname or ""
            request_port = urllib.parse.urlsplit(f"//{host_header}").port
        except ValueError:
            request_host, request_port = "", None

        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        if not origin and not referer:
            self._send_json(403, {"error": "Cross-origin request rejected: missing Origin or Referer"})
            return False

        source = origin or referer or ""
        try:
            parsed = urllib.parse.urlparse(source)
            source_host = parsed.hostname or ""
            source_port = parsed.port
        except ValueError:
            self._send_json(403, {"error": "Cross-origin request rejected: malformed origin"})
            return False

        if parsed.scheme != "http" or not source_host:
            self._send_json(403, {"error": "Cross-origin request rejected: invalid origin"})
            return False
        if origin and (parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            self._send_json(403, {"error": "Cross-origin request rejected: malformed Origin"})
            return False
        if not origin and (parsed.username is not None or parsed.password is not None or parsed.fragment):
            self._send_json(403, {"error": "Cross-origin request rejected: malformed Referer"})
            return False
        if not is_loopback(source_host):
            self._send_json(403, {"error": "Cross-origin request rejected: non-loopback origin"})
            return False
        if source_host.lower() != request_host.lower() or source_port != request_port:
            self._send_json(403, {"error": "Cross-origin request rejected: origin host or port mismatch"})
            return False

        session_id = self._get_session_id_from_cookie()
        csrf_token = self.headers.get("X-CSRF-Token")
        if not self.server.session_store.validate_session_and_csrf(session_id, csrf_token):
            self._send_json(403, {"error": "Invalid or missing CSRF token"})
            return False
        return True

    def _get_session_id_from_cookie(self) -> Optional[str]:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        try:
            cookie = http.cookies.SimpleCookie(cookie_header)
            if "tl_session" in cookie:
                return cookie["tl_session"].value
        except Exception:
            pass
        return None

    def _send_json(self, status: int, data: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Security headers
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # Ensure no CORS header is emitted
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Tuple[Optional[Any], Optional[str]]:
        content_length_str = self.headers.get("Content-Length")
        if not content_length_str:
            return None, "Missing Content-Length header"
        if not content_length_str.isascii() or not content_length_str.isdecimal():
            return None, "Invalid Content-Length header"
        content_length = int(content_length_str)
        if content_length <= 0:
            return None, "Content-Length must be positive"
        if content_length > 10 * 1024 * 1024:
            return None, "Request body exceeds maximum allowed size"
        if self.headers.get("Transfer-Encoding"):
            return None, "Transfer-Encoding is not supported"

        raw_body = self.rfile.read(content_length)
        try:
            return json.loads(raw_body.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "Malformed JSON body"

    # -------------------------------------------------------------------------
    # HTTP Method Handlers
    # -------------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        if not self._validate_host_header():
            return
        # Strict No CORS policy: OPTIONS is not allowed
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._validate_host_header():
            return

        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)

        # Health checks
        if path in ("/health", "/api/v1/health"):
            self._send_json(200, {"status": "ok", "version": SERVICE_VERSION})
            return

        # Bootstrap session & CSRF
        if path == "/api/v1/bootstrap":
            self._handle_bootstrap()
            return

        # Read-only resource endpoints
        if path == "/api/v1/pricing/versions":
            self._handle_list_pricing_versions()
            return
        if path == "/api/v1/pricing/fx":
            self._handle_list_pricing_fx()
            return
        if path == "/api/v1/settings":
            self._handle_get_settings()
            return
        if path == "/api/v1/privacy":
            self._handle_get_privacy()
            return
        if path == "/api/v1/privacy/projects":
            self._handle_get_privacy_projects()
            return
        if path == "/api/v1/privacy/retention":
            self._handle_get_privacy_retention()
            return
        if path == "/api/v1/saved-views":
            self._handle_get_saved_views()
            return
        if path == "/api/v1/layouts":
            self._handle_get_layouts()
            return
        if path == "/api/v1/outcomes":
            self._handle_get_outcomes(query_params)
            return
        if path == "/api/v1/subscription-fees":
            self._handle_list_subscription_fees(query_params)
            return
        if path == "/api/v1/budgets":
            self._handle_get_budgets()
            return
        if path == "/api/v1/reports/definitions":
            self._handle_get_report_definitions(query_params)
            return
        if path == "/api/v1/reports/scheduler/due":
            self._handle_get_due_reports()
            return
        if path == "/api/v1/exports":
            self._handle_get_exports(query_params)
            return
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["api", "v1", "exports"] and parts[4] == "download":
            self._handle_download_export(urllib.parse.unquote(parts[3]))
            return
        if path.startswith("/api/"):
            self._send_json(404, {"error": f"Endpoint not found: {path}"})
            return

        # Static file serving (bundled local dashboard)
        self._serve_static_file(path)
    def do_POST(self) -> None:
        if not self._validate_host_header():
            return

        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if not self._validate_same_origin_and_csrf():
            return

        body, err = self._read_json_body()
        if err:
            status = 413 if err == "Request body exceeds maximum allowed size" else 400
            self._send_json(status, {"error": err})
            return

        if path == "/api/v1/pricing/versions":
            self._handle_create_pricing_version(body)
            return
        if path == "/api/v1/pricing/fx":
            self._handle_create_pricing_fx(body)
            return
        if path == "/api/v1/pricing/recalculate":
            self._handle_pricing_recalculate(body)
            return
        if path == "/api/v1/query":
            self._handle_post_query(body)
            return
        if path == "/api/v1/settings":
            self._handle_post_settings(body)
            return
        if path == "/api/v1/privacy":
            self._handle_post_privacy(body)
            return
        if path == "/api/v1/privacy/projects":
            self._handle_post_privacy_projects(body)
            return
        if path == "/api/v1/privacy/retention":
            self._handle_post_privacy_retention(body)
            return
        if path == "/api/v1/privacy/retention/run":
            self._handle_run_privacy_retention(body)
            return
        if path == "/api/v1/saved-views":
            self._handle_post_saved_views(body)
            return
        if path == "/api/v1/layouts":
            self._handle_post_layouts(body)
            return
        if path == "/api/v1/outcomes":
            self._handle_post_outcomes(body)
            return
        if path == "/api/v1/subscription-fees":
            self._handle_save_subscription_fee(body)
            return
        if path == "/api/v1/budgets":
            self._handle_post_budgets(body)
            return
        if path == "/api/v1/quotas/snapshot":
            self._handle_post_quota_snapshot(body)
            return
        if path == "/api/v1/reports/definitions":
            self._handle_post_report_definitions(body)
            return
        if path == "/api/v1/reports/generate":
            self._handle_generate_report(body)
            return
        if path == "/api/v1/reports/scheduler/run-due":
            self._handle_run_due_reports(body)
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_DELETE(self) -> None:
        if not self._validate_host_header():
            return
        path = urllib.parse.urlparse(self.path).path
        if not self._validate_same_origin_and_csrf():
            return

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["api", "v1", "saved-views"]:
            self._handle_delete_saved_view(urllib.parse.unquote(parts[3]))
            return
        if len(parts) == 4 and parts[:3] == ["api", "v1", "layouts"]:
            self._handle_delete_layout(urllib.parse.unquote(parts[3]))
            return
        if len(parts) == 4 and parts[:3] == ["api", "v1", "budgets"]:
            self._handle_delete_budget(urllib.parse.unquote(parts[3]))
            return
        if len(parts) == 4 and parts[:3] == ["api", "v1", "outcomes"]:
            self._handle_delete_outcome(urllib.parse.unquote(parts[3]))
            return
        if len(parts) == 5 and parts[:4] == ["api", "v1", "reports", "definitions"]:
            self._handle_delete_report_definition(urllib.parse.unquote(parts[4]))
            return
        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_PATCH(self) -> None:
        self._handle_outcome_update_request()

    def do_PUT(self) -> None:
        self._handle_outcome_update_request()

    def _handle_outcome_update_request(self) -> None:
        if not self._validate_host_header() or not self._validate_same_origin_and_csrf():
            return
        body, err = self._read_json_body()
        if err:
            status = 413 if err == "Request body exceeds maximum allowed size" else 400
            self._send_json(status, {"error": err})
            return
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["api", "v1", "outcomes"]:
            self._handle_patch_outcome(urllib.parse.unquote(parts[3]), body)
            return
        if len(parts) == 4 and parts[:3] == ["api", "v1", "subscription-fees"]:
            self._handle_save_subscription_fee(body, urllib.parse.unquote(parts[3]))
            return
        self._send_json(404, {"error": "Endpoint not found"})

    # -------------------------------------------------------------------------
    # Endpoint Handlers
    # -------------------------------------------------------------------------
    def _handle_list_subscription_fees(self, query_params: Dict[str, List[str]]) -> None:
        if set(query_params) - {"limit", "offset"} or any(len(values) != 1 for values in query_params.values()):
            self._send_json(400, {"error": "Unsupported or repeated subscription fee query parameter"})
            return
        try:
            limit_text = query_params.get("limit", ["50"])[0]
            offset_text = query_params.get("offset", ["0"])[0]
            if not limit_text.isascii() or not limit_text.isdecimal() or not offset_text.isascii() or not offset_text.isdecimal():
                raise ValueError
            limit, offset = int(limit_text), int(offset_text)
            if not 1 <= limit <= 200 or offset > 9_223_372_036_854_775_807:
                raise ValueError
        except ValueError:
            self._send_json(400, {"error": "limit must be 1-200 and offset must be a non-negative integer"})
            return
        conn = self.server.get_connection()
        total = conn.execute("SELECT COUNT(*) FROM subscription_fees").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM subscription_fees ORDER BY paid_date DESC, fee_id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        self._send_json(200, {"subscription_fees": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset})

    def _handle_save_subscription_fee(self, body: Any, fee_id: Optional[str] = None) -> None:
        fields = {"provider", "account_alias", "access_route", "paid_date", "amount", "currency", "source"}
        try:
            if not isinstance(body, dict) or set(body) != fields:
                raise ValueError("All subscription fee fields are required")
            values = {key: _pricing_text(body[key], key) for key in fields - {"amount"}}
            if not _is_finite_number(body["amount"]) or body["amount"] < 0:
                raise ValueError("amount must be a non-negative finite number")
            if date.fromisoformat(values["paid_date"]).isoformat() != values["paid_date"]:
                raise ValueError("paid_date must be an ISO date")
            values["currency"] = values["currency"].upper()
            if len(values["currency"]) != 3 or not values["currency"].isascii() or not values["currency"].isalpha():
                raise ValueError("currency must be a three-letter ASCII code")
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        conn = self.server.get_connection()
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            if fee_id is None:
                fee_id = secrets.token_hex(16)
                conn.execute(
                    "INSERT INTO subscription_fees VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fee_id, values["provider"], values["account_alias"], values["access_route"],
                     values["paid_date"], body["amount"], values["currency"], values["source"], now, now),
                )
            else:
                result = conn.execute(
                    "UPDATE subscription_fees SET provider=?, account_alias=?, access_route=?, paid_date=?, amount=?, currency=?, source=?, updated_at=? WHERE fee_id=?",
                    (values["provider"], values["account_alias"], values["access_route"],
                     values["paid_date"], body["amount"], values["currency"], values["source"], now, fee_id),
                )
                if not result.rowcount:
                    self._send_json(404, {"error": "Subscription fee not found"})
                    return
        row = conn.execute("SELECT * FROM subscription_fees WHERE fee_id=?", (fee_id,)).fetchone()
        self._send_json(200, dict(row))


    def _handle_bootstrap(self) -> None:
        current_sid = self._get_session_id_from_cookie()
        session_id, csrf_token = self.server.session_store.get_or_create_csrf(current_sid)

        conn = self.server.get_connection()
        schema_ver = get_schema_version(conn)

        cookie_str = f"tl_session={session_id}; Path=/api; SameSite=Strict; HttpOnly"
        self._send_json(
            200,
            {
                "status": "ok",
                "csrf_token": csrf_token,
                "version": SERVICE_VERSION,
                "schema_version": schema_ver,
                "harness_options": ["combined", "claude-code", "omp"],
            },
            extra_headers={"Set-Cookie": cookie_str},
        )

    def _handle_list_pricing_versions(self) -> None:
        conn = self.server.get_connection()
        versions = []
        for row in conn.execute("SELECT * FROM pricing_versions ORDER BY effective_start DESC, version_id"):
            item = dict(row)
            item["source_provenance"] = json.loads(item.pop("source_provenance_json"))
            item["model_mappings"] = item["source_provenance"].pop("model_mappings", [])
            item["rates"] = []
            for rate in conn.execute("SELECT * FROM model_pricing_rates WHERE version_id = ? ORDER BY rate_id", (item["version_id"],)):
                entry = dict(rate)
                entry["is_tiered"] = bool(entry["is_tiered"])
                entry["tiered_rates_json"] = json.loads(entry["tiered_rates_json"])
                item["rates"].append(entry)
            versions.append(item)
        self._send_json(200, {"versions": versions})

    def _handle_create_pricing_version(self, body: Any) -> None:
        from token_ledger.pricing import create_pricing_version, get_pricing_version
        from dataclasses import asdict

        try:
            data = _validate_pricing_version(body)
            conn = self.server.get_connection()
            with conn:
                create_pricing_version(conn, **data)
            version = asdict(get_pricing_version(conn, data["version_id"]))
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except sqlite3.IntegrityError:
            self._send_json(409, {"error": "Pricing version already exists"})
            return
        self._send_json(201, {"version": version})

    def _handle_list_pricing_fx(self) -> None:
        conn = self.server.get_connection()
        rows = conn.execute("SELECT * FROM historical_fx ORDER BY effective_date DESC, base_currency, target_currency")
        self._send_json(200, {"fx_rates": [dict(row) for row in rows]})

    def _handle_create_pricing_fx(self, body: Any) -> None:
        if not isinstance(body, dict) or set(body) != {
            "base_currency", "target_currency", "rate_per_usd", "effective_date", "source_provenance"
        }:
            self._send_json(400, {"error": "Invalid FX fields"})
            return
        try:
            base = _pricing_text(body["base_currency"], "base_currency").upper()
            target = _pricing_text(body["target_currency"], "target_currency").upper()
            date = _pricing_text(body["effective_date"], "effective_date")
            source = _pricing_text(body["source_provenance"], "source_provenance")
            if len(base) != 3 or not base.isascii() or not base.isalpha() or len(target) != 3 or not target.isascii() or not target.isalpha() or base == target:
                raise ValueError("Currencies must be distinct three-letter ASCII codes")
            if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
                raise ValueError("effective_date must be YYYY-MM-DD")
            rate = body["rate_per_usd"]
            if not _is_finite_number(rate) or rate <= 0:
                raise ValueError("rate_per_usd must be a finite positive number")
            conn = self.server.get_connection()
            with conn:
                cursor = conn.execute(
                    "INSERT INTO historical_fx (base_currency, target_currency, rate_per_usd, effective_date, source_provenance, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (base, target, rate, date, source, datetime.now(timezone.utc).isoformat()),
                )
                fx_id = cursor.lastrowid
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except sqlite3.IntegrityError:
            self._send_json(409, {"error": "FX rate for currency pair and date already exists"})
            return
        self._send_json(201, {"fx_id": fx_id})

    def _handle_pricing_recalculate(self, body: Any) -> None:
        from dataclasses import asdict
        from token_ledger.pricing import calculate_request_valuation, get_pricing_version, persist_financial_facts
        from token_ledger.query.engine import filtered_request_rows

        if isinstance(body, dict) and "query" in body:
            if set(body) - {"query", "pricing_version_id", "persist"} or "canonical_id" in body:
                self._send_json(400, {"error": "Provide query, optional pricing_version_id and optional persist"})
                return
            try:
                version_id = body.get("pricing_version_id")
                if version_id is not None:
                    version_id = _pricing_text(version_id, "pricing_version_id")
                if not isinstance(body.get("persist", False), bool):
                    raise ValueError("persist must be boolean")
                conn = self.server.get_connection()
                if version_id and not get_pricing_version(conn, version_id):
                    self._send_json(404, {"error": "Pricing version not found"})
                    return
                rows = filtered_request_rows(conn, body["query"])
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            counters = ("fresh_input_tokens", "cache_read_tokens", "cache_write_5m_tokens",
                        "cache_write_1h_tokens", "cache_write_unknown_ttl_tokens", "output_tokens")
            valuations, abstentions = [], []
            with conn:
                for row in rows:
                    missing = [name for name in counters if row[name] is None]
                    if missing:
                        abstentions.append({"canonical_id": row["canonical_id"], "missing_counters": missing})
                        continue
                    result = calculate_request_valuation(conn, row["canonical_id"],
                        override_pricing_version_id=version_id, use_assigned_version=False)
                    billed = conn.execute(
                        "SELECT actual_billed_amount, actual_billed_currency, subscription_fee_usd FROM financial_facts WHERE canonical_id = ?",
                        (row["canonical_id"],),
                    ).fetchone()
                    if billed:
                        result.actual_billed_amount, result.actual_billed_currency, result.subscription_fee_usd = billed
                    if body.get("persist", False):
                        persist_financial_facts(conn, result, preserve_billing=True, update_request_version=False)
                    valuations.append(asdict(result))
            self._send_json(200, {"mode": "explicit" if version_id else "historical",
                                  "persisted": body.get("persist", False),
                                  "selected_count": len(rows), "valuations": valuations,
                                  "abstentions": abstentions})
            return

        if not isinstance(body, dict) or set(body) - {"canonical_id", "pricing_version_id", "persist"}:
            self._send_json(400, {"error": "Provide canonical_id, optional pricing_version_id and optional persist"})
            return
        try:
            canonical_id = _pricing_text(body.get("canonical_id"), "canonical_id")
            version_id = body.get("pricing_version_id")
            if version_id is not None:
                version_id = _pricing_text(version_id, "pricing_version_id")
            if not isinstance(body.get("persist", False), bool):
                raise ValueError("persist must be boolean")
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        conn = self.server.get_connection()
        if version_id and not get_pricing_version(conn, version_id):
            self._send_json(404, {"error": "Pricing version not found"})
            return
        row = conn.execute(
            "SELECT fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens FROM normalized_usage WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        if not conn.execute("SELECT 1 FROM canonical_requests WHERE canonical_id = ?", (canonical_id,)).fetchone():
            self._send_json(404, {"error": "Canonical request not found"})
            return
        if row is None or any(value is None for value in row):
            self._send_json(409, {"error": "Recalculation requires all six usage counters; missing is not zero"})
            return
        result = calculate_request_valuation(
            conn, canonical_id, override_pricing_version_id=version_id, use_assigned_version=False
        )
        billed = conn.execute(
            "SELECT actual_billed_amount, actual_billed_currency, subscription_fee_usd FROM financial_facts WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        if billed:
            result.actual_billed_amount, result.actual_billed_currency, result.subscription_fee_usd = billed
        if body.get("persist", False):
            with conn:
                persist_financial_facts(conn, result, preserve_billing=True, update_request_version=False)
        self._send_json(200, {"mode": "explicit" if version_id else "historical",
                              "persisted": body.get("persist", False), "valuation": asdict(result)})

    def _handle_post_query(self, envelope: Any) -> None:
        if not isinstance(envelope, dict):
            self._send_json(400, {"error": "Query payload must be a JSON object"})
            return

        conn = self.server.get_connection()
        try:
            # Dynamically import query engine so it works as soon as SharedQueryEngine lands it
            import token_ledger.query as tl_query
            result = tl_query.execute_query(conn, envelope)
            self._send_json(200, result)
        except ImportError:
            # Query engine not yet imported or in development
            self._send_json(501, {"error": "Query engine not yet available"})
        except ValueError as ve:
            self._send_json(400, {"error": str(ve)})
        except Exception as e:
            self._send_json(500, {"error": f"Internal query execution error: {str(e)}"})

    def _handle_get_report_definitions(self, query_params: Dict[str, List[str]]) -> None:
        from token_ledger.export.reports import list_report_definitions

        allowed_params = {"project_id", "account_alias"}
        if set(query_params) - allowed_params or any(len(values) != 1 for values in query_params.values()):
            self._send_json(400, {"error": "Unsupported or repeated report query parameter"})
            return
        filters: Dict[str, str] = {}
        for key in allowed_params:
            value = query_params.get(key, [None])[0]
            if value is not None:
                if not value.strip():
                    self._send_json(400, {"error": f"{key} must be non-empty"})
                    return
                filters[key] = value
        try:
            reports = list_report_definitions(
                self.server.get_connection(),
                project_id=filters.get("project_id"),
                account_alias=filters.get("account_alias"),
            )
        except Exception:
            self._send_json(500, {"error": "Failed to list report definitions"})
            return
        self._send_json(200, {"reports": [report.to_dict() for report in reports]})

    def _handle_post_report_definitions(self, body: Any) -> None:
        from token_ledger.export.reports import ReportDefinition, save_report_definition

        allowed_fields = {
            "item_id", "item_type", "name", "frequency", "filter_state",
            "threshold_config", "pricing_assumptions", "project_id",
            "account_alias", "last_generated_at", "created_at", "updated_at",
        }
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        if set(body) - allowed_fields:
            self._send_json(400, {"error": "Unknown report definition fields"})
            return
        if body.get("item_type", "report") != "report":
            self._send_json(400, {"error": "item_type must be report"})
            return
        string_fields = {
            "item_id", "name", "frequency", "project_id", "account_alias",
            "last_generated_at", "created_at", "updated_at",
        }
        for key in string_fields & body.keys():
            if body[key] is not None and not isinstance(body[key], str):
                self._send_json(400, {"error": f"{key} must be a string or null"})
                return
        for key in ("filter_state", "threshold_config", "pricing_assumptions"):
            if key in body and body[key] is not None and not isinstance(body[key], dict):
                self._send_json(400, {"error": f"{key} must be a JSON object"})
                return
        try:
            report = save_report_definition(
                self.server.get_connection(),
                ReportDefinition.from_dict(body),
            )
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            self._send_json(500, {"error": "Failed to save report definition"})
            return
        self._send_json(200, {"report": report.to_dict()})

    def _handle_delete_report_definition(self, report_id: str) -> None:
        from token_ledger.export.reports import delete_report_definition

        if not report_id or len(report_id) > 128:
            self._send_json(404, {"error": "Report definition not found"})
            return
        try:
            deleted = delete_report_definition(self.server.get_connection(), report_id)
        except Exception:
            self._send_json(500, {"error": "Failed to delete report definition"})
            return
        if deleted:
            self._send_json(200, {"deleted": report_id})
        else:
            self._send_json(404, {"error": "Report definition not found"})

    def _handle_generate_report(self, body: Any) -> None:
        from token_ledger.export.reports import execute_report

        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        if set(body) - {"report_id", "formats"}:
            self._send_json(400, {"error": "Unknown report generation fields"})
            return
        report_id = body.get("report_id")
        if not isinstance(report_id, str) or not report_id.strip() or len(report_id) > 128:
            self._send_json(400, {"error": "report_id must be a non-empty string of at most 128 characters"})
            return
        formats = body.get("formats")
        if "formats" in body and (not isinstance(formats, list) or not formats):
            self._send_json(400, {"error": "formats must be a non-empty array"})
            return
        try:
            result = execute_report(
                self.server.get_connection(),
                report_id,
                formats=formats,
                managed_root=self.server.export_root,
            )
        except KeyError:
            self._send_json(404, {"error": "Report definition not found"})
            return
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            self._send_json(500, {"error": "Failed to generate report"})
            return
        self._send_json(200, self._report_execution_payload(result))

    def _handle_get_due_reports(self) -> None:
        from token_ledger.export.reports import get_due_reports

        try:
            reports = get_due_reports(self.server.get_connection())
        except Exception:
            self._send_json(500, {"error": "Failed to list due reports"})
            return
        self._send_json(200, {"due_reports": [report.to_dict() for report in reports]})

    def _handle_run_due_reports(self, body: Any) -> None:
        from token_ledger.export.reports import run_due_reports_scheduler

        if not isinstance(body, dict) or body:
            self._send_json(400, {"error": "Payload must be an empty JSON object"})
            return
        try:
            results = run_due_reports_scheduler(
                self.server.get_connection(),
                managed_root=self.server.export_root,
            )
        except Exception:
            self._send_json(500, {"error": "Failed to run due reports"})
            return
        self._send_json(
            200,
            {
                "executed": [self._report_execution_payload(result) for result in results],
                "count": len(results),
            },
        )

    @staticmethod
    def _report_execution_payload(result: Any) -> Dict[str, Any]:
        return {
            "report_id": result.report_id,
            "generated_at": result.generated_at,
            "exports": [record.to_dict() for record in result.exports],
            "coverage": result.coverage,
        }

    def _handle_get_exports(self, query_params: Dict[str, List[str]]) -> None:
        from token_ledger.export.snapshot import list_exports

        if set(query_params) - {"limit"} or any(len(values) != 1 for values in query_params.values()):
            self._send_json(400, {"error": "Unsupported or repeated export query parameter"})
            return
        limit_text = query_params.get("limit", ["100"])[0]
        try:
            if not limit_text.isascii() or not limit_text.isdecimal():
                raise ValueError
            limit = int(limit_text)
            if not 1 <= limit <= 1000:
                raise ValueError
        except ValueError:
            self._send_json(400, {"error": "limit must be an integer between 1 and 1000"})
            return
        try:
            exports = list_exports(self.server.get_connection(), limit=limit)
        except Exception:
            self._send_json(500, {"error": "Failed to list exports"})
            return
        self._send_json(200, {"exports": [record.to_dict() for record in exports]})

    def _handle_download_export(self, export_id: str) -> None:
        from token_ledger.export.snapshot import ALLOWED_EXPORT_FORMATS, get_export_by_id
        from token_ledger.privacy import PathTraversalError, verify_path_within_root

        if not export_id or len(export_id) > 128:
            self._send_json(404, {"error": "Export not found"})
            return
        try:
            record = get_export_by_id(self.server.get_connection(), export_id)
        except Exception:
            self._send_json(500, {"error": "Failed to look up export"})
            return
        if record is None:
            self._send_json(404, {"error": "Export not found"})
            return
        if record.export_format not in ALLOWED_EXPORT_FORMATS:
            self._send_json(404, {"error": "Export not found"})
            return
        try:
            verified_path = verify_path_within_root(record.file_path, self.server.export_root)
            file_fd = self._open_verified_export(verified_path)
            file_info = os.fstat(file_fd)
            if not stat.S_ISREG(file_info.st_mode):
                os.close(file_fd)
                self._send_json(403, {"error": "Export file is not a regular managed file"})
                return
        except PathTraversalError:
            self._send_json(403, {"error": "Export file is outside the managed export root"})
            return
        except FileNotFoundError:
            self._send_json(404, {"error": "Export file not found"})
            return
        except OSError:
            self._send_json(403, {"error": "Export file is unavailable"})
            return

        content_types = {
            "csv": "text/csv; charset=utf-8",
            "json": "application/json; charset=utf-8",
            "html": "text/html; charset=utf-8",
            "pdf": "application/pdf",
            "png": "image/png",
        }
        filename = urllib.parse.quote(f"{record.export_id}.{record.export_format}", safe=".-_")
        self.send_response(200)
        self.send_header("Content-Type", content_types[record.export_format])
        self.send_header("Content-Length", str(file_info.st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            with os.fdopen(file_fd, "rb") as export_file:
                while True:
                    chunk = export_file.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            return

    def _open_verified_export(self, verified_path: str) -> int:
        from token_ledger.privacy import PathTraversalError

        root = os.path.realpath(self.server.export_root)
        relative_path = os.path.relpath(verified_path, root)
        parts = relative_path.split(os.sep)
        if (
            relative_path == "."
            or relative_path.startswith("..")
            or os.path.isabs(relative_path)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise PathTraversalError("Export path is outside the managed root")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            root_fd = os.open(root, flags | getattr(os, "O_DIRECTORY", 0))
            current_fd = root_fd
            try:
                for part in parts[:-1]:
                    next_fd = os.open(
                        part,
                        flags | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                    if current_fd != root_fd:
                        os.close(current_fd)
                    current_fd = next_fd
                return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=current_fd)
            finally:
                if current_fd != root_fd:
                    os.close(current_fd)
                os.close(root_fd)
        return os.open(verified_path, flags | getattr(os, "O_NOFOLLOW", 0))

    def _handle_get_settings(self) -> None:
        conn = self.server.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT setting_key, setting_value, opt_in_enabled, description, updated_at FROM privacy_settings ORDER BY setting_key ASC;"
        )
        rows = cur.fetchall()
        cur.close()
        self._send_json(
            200,
            {
                "settings": [
                    {
                        "setting_key": row["setting_key"],
                        "setting_value": row["setting_value"],
                        "opt_in_enabled": bool(row["opt_in_enabled"]),
                        "description": row["description"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
            },
        )

    def _handle_post_settings(self, body: Any) -> None:
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        setting_key = body.get("setting_key")
        setting_value = body.get("setting_value")
        opt_in_enabled = body.get("opt_in_enabled", False)
        description = body.get("description")
        if not isinstance(setting_key, str) or not setting_key or len(setting_key) > 128:
            self._send_json(400, {"error": "setting_key must be a non-empty string of at most 128 characters"})
            return
        if not isinstance(setting_value, str) or len(setting_value) > 65536:
            self._send_json(400, {"error": "setting_value must be a string of at most 65536 characters"})
            return
        if not isinstance(opt_in_enabled, bool):
            self._send_json(400, {"error": "opt_in_enabled must be a boolean"})
            return
        if description is not None and not isinstance(description, str):
            self._send_json(400, {"error": "description must be a string or null"})
            return

        from token_ledger.privacy import VALID_OPT_IN_KEYS
        protected_keys = VALID_OPT_IN_KEYS | {
            "privacy_telemetry_opt_in",
            "privacy_content_opt_in",
            "privacy_git_metadata_opt_in",
            "notifications_opt_in",
            "project_inclusion_policy",
            "project_exclusion_list",
            "retention_days",
        }
        if setting_key in protected_keys:
            self._send_json(400, {"error": "Use the dedicated privacy endpoint for this setting"})
            return

        now_utc = datetime.now(timezone.utc).isoformat()
        conn = self.server.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO privacy_settings (setting_key, setting_value, opt_in_enabled, description, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    opt_in_enabled = excluded.opt_in_enabled,
                    description = COALESCE(excluded.description, privacy_settings.description),
                    updated_at = excluded.updated_at;
                """,
                (setting_key, setting_value, int(opt_in_enabled), description, now_utc),
            )
        self._send_json(
            200,
            {
                "setting_key": setting_key,
                "setting_value": setting_value,
                "opt_in_enabled": opt_in_enabled,
                "description": description,
                "updated_at": now_utc,
            },
        )

    def _handle_get_privacy(self) -> None:
        from token_ledger.privacy import load_privacy_settings

        try:
            settings = load_privacy_settings(self.server.get_connection())
        except Exception:
            self._send_json(500, {"error": "Failed to load privacy settings"})
            return
        self._send_json(
            200,
            {
                "settings": {
                    "content_capture": settings.content_capture,
                    "content_fingerprints": settings.content_fingerprints,
                    "git_linkage": settings.git_linkage,
                    "notifications": settings.notifications,
                    "policy_version": settings.policy_version,
                    "opt_in_versions": settings.opt_in_versions,
                    "updated_at": settings.updated_at,
                }
            },
        )

    def _handle_post_privacy(self, body: Any) -> None:
        from token_ledger.privacy import VALID_OPT_IN_KEYS, load_privacy_settings, save_privacy_setting
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        if set(body) - {"setting_key", "opt_in_enabled", "description"}:
            self._send_json(400, {"error": "Unknown privacy setting fields"})
            return
        key = body.get("setting_key")
        enabled = body.get("opt_in_enabled")
        description = body.get("description")
        if not isinstance(key, str) or key not in VALID_OPT_IN_KEYS:
            self._send_json(400, {"error": f"setting_key must be one of: {', '.join(sorted(VALID_OPT_IN_KEYS))}"})
            return
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "opt_in_enabled must be a boolean"})
            return
        if description is not None and (not isinstance(description, str) or len(description) > 512):
            self._send_json(400, {"error": "description must be a string of at most 512 characters or null"})
            return
        conn = self.server.get_connection()
        try:
            save_privacy_setting(conn, key, enabled, description=description)
            settings = load_privacy_settings(conn)
        except Exception:
            self._send_json(500, {"error": "Failed to save privacy setting"})
            return
        self._send_json(200, {
            "setting_key": key,
            "opt_in_enabled": enabled,
            "settings": {
                "content_capture": settings.content_capture,
                "content_fingerprints": settings.content_fingerprints,
                "git_linkage": settings.git_linkage,
                "notifications": settings.notifications,
                "policy_version": settings.policy_version,
                "opt_in_versions": settings.opt_in_versions,
                "updated_at": settings.updated_at,
            },
        })

    def _load_project_policy(self):
        from token_ledger.privacy import ProjectInclusionPolicy
        conn = self.server.get_connection()
        row = conn.execute(
            "SELECT setting_value FROM privacy_settings WHERE setting_key = 'project_inclusion_policy'"
        ).fetchone()
        if row is None:
            return ProjectInclusionPolicy()
        try:
            raw = json.loads(row["setting_value"])
            if not isinstance(raw, dict):
                return ProjectInclusionPolicy()
            values = {}
            for key in ("allowed_projects", "excluded_projects", "include_patterns", "exclude_patterns"):
                value = raw.get(key)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) or not item for item in value)
                ):
                    return ProjectInclusionPolicy()
                values[key] = set(value) if key in ("allowed_projects", "excluded_projects") and value is not None else value
            return ProjectInclusionPolicy(**values)
        except (json.JSONDecodeError, TypeError, ValueError):
            return ProjectInclusionPolicy()

    @staticmethod
    def _project_policy_dict(policy: Any) -> Dict[str, Any]:
        return {
            "allowed_projects": sorted(policy.allowed_projects) if policy.allowed_projects is not None else None,
            "excluded_projects": sorted(policy.excluded_projects) if policy.excluded_projects is not None else None,
            "include_patterns": list(policy.include_patterns) if policy.include_patterns is not None else None,
            "exclude_patterns": list(policy.exclude_patterns) if policy.exclude_patterns is not None else None,
        }

    def _handle_get_privacy_projects(self) -> None:
        self._send_json(200, {"policy": self._project_policy_dict(self._load_project_policy())})

    def _handle_post_privacy_projects(self, body: Any) -> None:
        from token_ledger.privacy import ProjectInclusionPolicy
        allowed = {"allowed_projects", "excluded_projects", "include_patterns", "exclude_patterns"}
        if not isinstance(body, dict) or set(body) - allowed:
            self._send_json(400, {"error": "Project policy must be an object containing only supported fields"})
            return
        values: Dict[str, Any] = {}
        for key in allowed:
            value = body.get(key)
            if value is not None:
                if (
                    not isinstance(value, list)
                    or len(value) > 500
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 512 for item in value)
                ):
                    self._send_json(400, {"error": f"{key} must be null or an array of at most 500 non-empty strings"})
                    return
                value = [item.strip() for item in value]
            values[key] = value
        policy = ProjectInclusionPolicy(
            allowed_projects=set(values["allowed_projects"]) if values["allowed_projects"] is not None else None,
            excluded_projects=set(values["excluded_projects"]) if values["excluded_projects"] is not None else None,
            include_patterns=values["include_patterns"],
            exclude_patterns=values["exclude_patterns"],
        )
        normalized = self._project_policy_dict(policy)
        now_utc = datetime.now(timezone.utc).isoformat()
        conn = self.server.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO privacy_settings (setting_key, setting_value, opt_in_enabled, description, updated_at)
                VALUES ('project_inclusion_policy', ?, 0, 'Project inclusion and exclusion policy', ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    opt_in_enabled = 0,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(normalized, sort_keys=True), now_utc),
            )
        self._send_json(200, {"policy": normalized, "updated_at": now_utc})

    def _handle_get_privacy_retention(self) -> None:
        try:
            row = self.server.get_connection().execute(
                "SELECT setting_value FROM privacy_settings WHERE setting_key = 'retention_days'"
            ).fetchone()
        except Exception:
            self._send_json(500, {"error": "Failed to load retention settings"})
            return
        retention_days = None
        if row is not None:
            try:
                parsed = int(row["setting_value"])
                retention_days = parsed if 1 <= parsed <= 36500 else None
            except (TypeError, ValueError):
                pass
        self._send_json(200, {
            "retention_days": retention_days,
            "supported_scopes": ["older_than_iso", "session_id", "project_id", "account_alias"],
        })

    def _handle_post_privacy_retention(self, body: Any) -> None:
        if not isinstance(body, dict) or set(body) != {"retention_days"}:
            self._send_json(400, {"error": "Payload must contain only retention_days"})
            return
        days = body.get("retention_days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 36500:
            self._send_json(400, {"error": "retention_days must be an integer from 1 to 36500"})
            return
        now_utc = datetime.now(timezone.utc).isoformat()
        conn = self.server.get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO privacy_settings (setting_key, setting_value, opt_in_enabled, description, updated_at)
                    VALUES ('retention_days', ?, 0, 'Local managed-data retention period in days', ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value,
                        opt_in_enabled = 0,
                        description = excluded.description,
                        updated_at = excluded.updated_at
                    """,
                    (str(days), now_utc),
                )
        except Exception:
            self._send_json(500, {"error": "Failed to save retention settings"})
            return
        self._send_json(200, {"retention_days": days, "updated_at": now_utc})

    def _handle_run_privacy_retention(self, body: Any) -> None:
        scope_fields = {"older_than_iso", "session_id", "project_id", "account_alias"}
        allowed_fields = scope_fields | {"delete_snapshot_files"}
        if not isinstance(body, dict) or set(body) - allowed_fields:
            self._send_json(400, {"error": "Retention accepts only a supported scope and delete_snapshot_files"})
            return
        delete_snapshot_files = body.get("delete_snapshot_files", True)
        if not isinstance(delete_snapshot_files, bool):
            self._send_json(400, {"error": "delete_snapshot_files must be a boolean"})
            return

        selected_scopes = set(body) & scope_fields
        if body and len(selected_scopes) != 1:
            self._send_json(400, {"error": "Provide exactly one supported retention scope"})
            return
        scope: Dict[str, str] = {}
        cutoff = None
        retention_days = None
        if selected_scopes:
            scope_key = next(iter(selected_scopes))
            scope_value = body[scope_key]
            if not isinstance(scope_value, str) or not scope_value.strip() or len(scope_value) > 512:
                self._send_json(400, {"error": f"{scope_key} must be a non-empty string of at most 512 characters"})
                return
            scope_value = scope_value.strip()
            if scope_key == "older_than_iso":
                try:
                    parsed_cutoff = datetime.fromisoformat(scope_value.replace("Z", "+00:00"))
                except ValueError:
                    self._send_json(400, {"error": "older_than_iso must be an ISO-8601 timestamp"})
                    return
                if parsed_cutoff.tzinfo is None or parsed_cutoff.utcoffset() is None:
                    self._send_json(400, {"error": "older_than_iso must include a timezone"})
                    return
                cutoff = parsed_cutoff.astimezone(timezone.utc).isoformat()
                if parsed_cutoff > datetime.now(parsed_cutoff.tzinfo):
                    self._send_json(400, {"error": "older_than_iso must not be in the future"})
                    return
            else:
                scope[scope_key] = scope_value
        else:
            days_state = self.server.get_connection().execute(
                "SELECT setting_value FROM privacy_settings WHERE setting_key = 'retention_days'"
            ).fetchone()
            if days_state is None:
                self._send_json(409, {"error": "Configure retention_days or provide an explicit retention scope"})
                return
            try:
                retention_days = int(days_state["setting_value"])
            except (TypeError, ValueError):
                self._send_json(409, {"error": "Stored retention_days is invalid"})
                return
            if not 1 <= retention_days <= 36500:
                self._send_json(409, {"error": "Stored retention_days is invalid"})
                return
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

        from dataclasses import asdict
        from token_ledger.privacy import cascade_delete_retention
        try:
            result = cascade_delete_retention(
                self.server.get_connection(),
                managed_root=self.server.managed_root,
                older_than_iso=cutoff,
                delete_snapshot_files=delete_snapshot_files,
                **scope,
            )
        except (OSError, ValueError, TypeError) as exc:
            self._send_json(500, {"error": f"Retention execution failed: {exc}"})
            return
        except Exception:
            self._send_json(500, {"error": "Retention execution failed"})
            return
        response = {"result": asdict(result)}
        if cutoff is not None:
            response["older_than_iso"] = cutoff
        if retention_days is not None:
            response["retention_days"] = retention_days
        if scope:
            response["scope"] = scope
        self._send_json(200, response)
    def _handle_get_saved_views(self) -> None:
        conn = self.server.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT view_id, view_name, is_default, page_name, filter_state_json, layout_state_json, created_at, updated_at
            FROM saved_views_and_layouts
            WHERE view_type = 'saved_view'
            ORDER BY view_name ASC;
            """
        )
        rows = cur.fetchall()
        cur.close()

        views = []
        for r in rows:
            try:
                filters = json.loads(r["filter_state_json"])
            except Exception:
                filters = {}
            try:
                layouts = json.loads(r["layout_state_json"])
            except Exception:
                layouts = {}
            views.append(
                {
                    "view_id": r["view_id"],
                    "view_name": r["view_name"],
                    "is_default": bool(r["is_default"]),
                    "page_name": r["page_name"],
                    "filter_state": filters,
                    "layout_state": layouts,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )
        self._send_json(200, {"saved_views": views})

    def _handle_post_saved_views(self, body: Any) -> None:
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return

        view_name = body.get("view_name")
        if not view_name or not isinstance(view_name, str):
            self._send_json(400, {"error": "view_name must be a non-empty string"})
            return

        filter_state = body.get("filter_state", {})
        if not isinstance(filter_state, dict):
            self._send_json(400, {"error": "filter_state must be a dictionary"})
            return

        layout_state = body.get("layout_state", {})
        if not isinstance(layout_state, dict):
            self._send_json(400, {"error": "layout_state must be a dictionary"})
            return

        view_id = body.get("view_id") or f"view_{secrets.token_hex(8)}"
        is_default = 1 if body.get("is_default") else 0
        page_name = body.get("page_name")
        now_utc = datetime.now(timezone.utc).isoformat()

        conn = self.server.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO saved_views_and_layouts (
                    view_id, view_type, view_name, is_default, page_name,
                    filter_state_json, layout_state_json, created_at, updated_at
                ) VALUES (?, 'saved_view', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(view_id) DO UPDATE SET
                    view_name = excluded.view_name,
                    is_default = excluded.is_default,
                    page_name = excluded.page_name,
                    filter_state_json = excluded.filter_state_json,
                    layout_state_json = excluded.layout_state_json,
                    updated_at = excluded.updated_at;
                """,
                (
                    view_id,
                    view_name,
                    is_default,
                    page_name,
                    json.dumps(filter_state),
                    json.dumps(layout_state),
                    now_utc,
                    now_utc,
                ),
            )

        self._send_json(
            200,
            {
                "view_id": view_id,
                "view_name": view_name,
                "is_default": bool(is_default),
                "page_name": page_name,
                "filter_state": filter_state,
                "layout_state": layout_state,
                "updated_at": now_utc,
            },
        )

    def _handle_delete_saved_view(self, view_id: str) -> None:
        conn = self.server.get_connection()
        with conn:
            cur = conn.execute(
                "DELETE FROM saved_views_and_layouts WHERE view_id = ? AND view_type = 'saved_view';",
                (view_id,),
            )
            deleted = cur.rowcount > 0
        if deleted:
            self._send_json(200, {"deleted": view_id})
        else:
            self._send_json(404, {"error": f"Saved view not found: {view_id}"})

    def _handle_get_layouts(self) -> None:
        conn = self.server.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT view_id, view_name, is_default, page_name, filter_state_json, layout_state_json, created_at, updated_at
            FROM saved_views_and_layouts
            WHERE view_type = 'layout'
            ORDER BY view_name ASC;
            """
        )
        rows = cur.fetchall()
        cur.close()

        layouts = []
        for r in rows:
            try:
                layout_st = json.loads(r["layout_state_json"])
            except Exception:
                layout_st = {}
            layouts.append(
                {
                    "layout_id": r["view_id"],
                    "layout_name": r["view_name"],
                    "is_default": bool(r["is_default"]),
                    "page_name": r["page_name"],
                    "layout_state": layout_st,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )
        self._send_json(200, {"layouts": layouts})

    def _handle_post_layouts(self, body: Any) -> None:
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return

        layout_name = body.get("layout_name") or body.get("view_name")
        if not layout_name or not isinstance(layout_name, str):
            self._send_json(400, {"error": "layout_name must be a non-empty string"})
            return

        layout_state = body.get("layout_state", {})
        if not isinstance(layout_state, dict):
            self._send_json(400, {"error": "layout_state must be a dictionary"})
            return

        layout_id = body.get("layout_id") or body.get("view_id") or f"layout_{secrets.token_hex(8)}"
        is_default = 1 if body.get("is_default") else 0
        page_name = body.get("page_name")
        now_utc = datetime.now(timezone.utc).isoformat()

        conn = self.server.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO saved_views_and_layouts (
                    view_id, view_type, view_name, is_default, page_name,
                    filter_state_json, layout_state_json, created_at, updated_at
                ) VALUES (?, 'layout', ?, ?, ?, '{}', ?, ?, ?)
                ON CONFLICT(view_id) DO UPDATE SET
                    view_name = excluded.view_name,
                    is_default = excluded.is_default,
                    page_name = excluded.page_name,
                    layout_state_json = excluded.layout_state_json,
                    updated_at = excluded.updated_at;
                """,
                (layout_id, layout_name, is_default, page_name, json.dumps(layout_state), now_utc, now_utc),
            )

        self._send_json(
            200,
            {
                "layout_id": layout_id,
                "layout_name": layout_name,
                "is_default": bool(is_default),
                "page_name": page_name,
                "layout_state": layout_state,
                "updated_at": now_utc,
            },
        )

    def _handle_delete_layout(self, layout_id: str) -> None:
        conn = self.server.get_connection()
        with conn:
            cur = conn.execute(
                "DELETE FROM saved_views_and_layouts WHERE view_id = ? AND view_type = 'layout';",
                (layout_id,),
            )
            deleted = cur.rowcount > 0
        if deleted:
            self._send_json(200, {"deleted": layout_id})
        else:
            self._send_json(404, {"error": f"Layout not found: {layout_id}"})

    def _handle_get_outcomes(self, query_params: Dict[str, List[str]]) -> None:
        from token_ledger.outcomes import VALID_COMPLETION_STATUSES, VALID_ENTITY_TYPES, list_outcomes

        allowed_params = {
            "entity_type", "entity_id", "task_label", "completion_rating",
            "is_inferred", "account_alias", "limit", "offset",
        }
        if set(query_params) - allowed_params or any(len(values) != 1 for values in query_params.values()):
            self._send_json(400, {"error": "Unsupported or repeated outcome query parameter"})
            return
        filters: Dict[str, Any] = {}
        for key in ("entity_type", "entity_id", "task_label", "completion_rating", "account_alias"):
            value = query_params.get(key, [None])[0]
            if value is not None:
                if not value.strip():
                    self._send_json(400, {"error": f"{key} must be non-empty"})
                    return
                filters[key] = value
        entity_type = filters.get("entity_type")
        if entity_type is not None and entity_type not in VALID_ENTITY_TYPES:
            self._send_json(400, {"error": f"entity_type must be one of: {', '.join(sorted(VALID_ENTITY_TYPES))}"})
            return
        completion_rating = filters.get("completion_rating")
        if completion_rating is not None and completion_rating not in VALID_COMPLETION_STATUSES:
            self._send_json(400, {"error": f"completion_rating must be one of: {', '.join(sorted(VALID_COMPLETION_STATUSES))}"})
            return

        inferred_text = query_params.get("is_inferred", [None])[0]
        if inferred_text is not None and inferred_text not in {"true", "false"}:
            self._send_json(400, {"error": "is_inferred must be true or false"})
            return
        filters["is_inferred"] = None if inferred_text is None else inferred_text == "true"
        try:
            limit_text = query_params.get("limit", ["100"])[0]
            offset_text = query_params.get("offset", ["0"])[0]
            if not limit_text.isascii() or not limit_text.isdecimal() or not offset_text.isascii() or not offset_text.isdecimal():
                raise ValueError
            limit = int(limit_text)
            offset = int(offset_text)
            if not 1 <= limit <= 1000 or offset > 1_000_000:
                raise ValueError
        except ValueError:
            self._send_json(400, {"error": "limit must be 1-1000 and offset must be a non-negative integer"})
            return

        try:
            records = list_outcomes(
                self.server.get_connection(),
                entity_type=filters.get("entity_type"),
                entity_id=filters.get("entity_id"),
                task_label=filters.get("task_label"),
                completion_rating=filters.get("completion_rating"),
                is_inferred=filters["is_inferred"],
                account_alias=filters.get("account_alias"),
                limit=limit,
                offset=offset,
            )
        except Exception:
            self._send_json(500, {"error": "Failed to list outcomes"})
            return
        self._send_json(200, {"outcomes": [record.to_dict() for record in records]})

    def _handle_post_outcomes(self, body: Any) -> None:
        from token_ledger.outcomes import VALID_COMPLETION_STATUSES, VALID_ENTITY_TYPES, create_outcome

        allowed_fields = {
            "outcome_id", "entity_type", "entity_id", "account_alias", "task_label",
            "completion_rating", "manual_rating", "git_commit_link", "notes",
            "was_corrected", "was_restarted",
        }
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        if set(body) - allowed_fields:
            self._send_json(400, {"error": "Unknown outcome fields"})
            return

        entity_type = body.get("entity_type")
        entity_id = body.get("entity_id")
        if not isinstance(entity_type, str) or entity_type not in VALID_ENTITY_TYPES:
            self._send_json(400, {"error": f"entity_type must be one of: {', '.join(sorted(VALID_ENTITY_TYPES))}"})
            return
        if not isinstance(entity_id, str) or not entity_id.strip() or len(entity_id) > 512:
            self._send_json(400, {"error": "entity_id must be a non-empty string of at most 512 characters"})
            return
        for key in ("outcome_id", "account_alias", "task_label", "git_commit_link", "notes"):
            value = body.get(key)
            max_length = 65536 if key == "notes" else 512
            if value is not None and (not isinstance(value, str) or len(value) > max_length):
                self._send_json(400, {"error": f"{key} must be a string of at most {max_length} characters or null"})
                return
        if body.get("outcome_id") is not None and not body["outcome_id"].strip():
            self._send_json(400, {"error": "outcome_id must be non-empty"})
            return
        if body.get("account_alias") is not None and not body["account_alias"].strip():
            self._send_json(400, {"error": "account_alias must be non-empty"})
            return
        status = body.get("completion_rating")
        if status is not None and (not isinstance(status, str) or status not in VALID_COMPLETION_STATUSES):
            self._send_json(400, {"error": f"completion_rating must be one of: {', '.join(sorted(VALID_COMPLETION_STATUSES))}, or null"})
            return
        rating = body.get("manual_rating")
        if rating is not None and not _is_finite_number(rating):
            self._send_json(400, {"error": "manual_rating must be a finite number or null"})
            return
        for key in ("was_corrected", "was_restarted"):
            if key in body and not isinstance(body[key], bool):
                self._send_json(400, {"error": f"{key} must be a boolean"})
                return

        try:
            record = create_outcome(
                self.server.get_connection(),
                entity_type=entity_type,
                entity_id=entity_id,
                outcome_id=body.get("outcome_id"),
                account_alias=body.get("account_alias", "default"),
                task_label=body.get("task_label"),
                completion_rating=status,
                manual_rating=rating,
                is_inferred=False,
                git_commit_link=body.get("git_commit_link"),
                notes=body.get("notes"),
                was_corrected=body.get("was_corrected"),
                was_restarted=body.get("was_restarted"),
                user_ownership="user",
                source="api",
            )
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            self._send_json(500, {"error": "Failed to create outcome"})
            return
        self._send_json(200, record.to_dict())

    def _handle_patch_outcome(self, outcome_id: str, body: Any) -> None:
        from token_ledger.outcomes import VALID_COMPLETION_STATUSES, update_outcome

        allowed_fields = {
            "task_label", "completion_rating", "manual_rating", "git_commit_link",
            "notes", "was_corrected", "was_restarted",
        }
        if not outcome_id or len(outcome_id) > 512:
            self._send_json(400, {"error": "outcome_id must be a non-empty string of at most 512 characters"})
            return
        if not isinstance(body, dict) or not body or set(body) - allowed_fields:
            self._send_json(400, {"error": "Payload must contain only supported outcome fields"})
            return
        for key in ("task_label", "git_commit_link", "notes"):
            value = body.get(key)
            max_length = 65536 if key == "notes" else 512
            if key in body and (not isinstance(value, str) or len(value) > max_length):
                self._send_json(400, {"error": f"{key} must be a string of at most {max_length} characters"})
                return
        if "completion_rating" in body and (
            not isinstance(body["completion_rating"], str) or body["completion_rating"] not in VALID_COMPLETION_STATUSES
        ):
            self._send_json(400, {"error": f"completion_rating must be one of: {', '.join(sorted(VALID_COMPLETION_STATUSES))}"})
            return
        if "manual_rating" in body and body["manual_rating"] is not None and not _is_finite_number(body["manual_rating"]):
            self._send_json(400, {"error": "manual_rating must be a finite number or null"})
            return
        for key in ("was_corrected", "was_restarted"):
            if key in body and not isinstance(body[key], bool):
                self._send_json(400, {"error": f"{key} must be a boolean"})
                return

        try:
            record = update_outcome(
                self.server.get_connection(),
                outcome_id,
                **body,
            )
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            self._send_json(500, {"error": "Failed to update outcome"})
            return
        if record is None:
            self._send_json(404, {"error": f"Outcome not found: {outcome_id}"})
            return
        self._send_json(200, record.to_dict())

    def _handle_delete_outcome(self, outcome_id: str) -> None:
        from token_ledger.outcomes import delete_outcome

        if not outcome_id or len(outcome_id) > 512:
            self._send_json(400, {"error": "outcome_id must be a non-empty string of at most 512 characters"})
            return
        try:
            deleted = delete_outcome(self.server.get_connection(), outcome_id)
        except Exception:
            self._send_json(500, {"error": "Failed to delete outcome"})
            return
        if deleted:
            self._send_json(200, {"deleted": outcome_id})
        else:
            self._send_json(404, {"error": f"Outcome not found: {outcome_id}"})
    def _handle_get_budgets(self) -> None:
        from token_ledger.metrics.providers import list_budgets

        try:
            budgets = list_budgets(self.server.get_connection())
        except Exception:
            self._send_json(500, {"error": "Failed to list budgets"})
            return
        self._send_json(200, {"budgets": budgets})

    def _handle_post_budgets(self, body: Any) -> None:
        from token_ledger.metrics.providers import VALID_BUDGET_PERIODS, VALID_BUDGET_TARGETS, save_budget
        from token_ledger.privacy import is_opt_in_enabled

        allowed_fields = {
            "id", "name", "period", "target_type", "threshold",
            "scope", "notifications_enabled", "account_alias",
        }
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        if set(body) - allowed_fields:
            self._send_json(400, {"error": "Unknown budget fields"})
            return
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            self._send_json(400, {"error": "name must be a non-empty string"})
            return
        budget_id = body.get("id")
        if budget_id is not None and (not isinstance(budget_id, str) or not budget_id.strip()):
            self._send_json(400, {"error": "id must be a non-empty string or null"})
            return
        period = body.get("period", "monthly")
        if not isinstance(period, str) or period not in VALID_BUDGET_PERIODS:
            self._send_json(400, {"error": f"period must be one of: {', '.join(sorted(VALID_BUDGET_PERIODS))}"})
            return
        target_type = body.get("target_type", "tokens")
        if not isinstance(target_type, str) or target_type not in VALID_BUDGET_TARGETS:
            self._send_json(400, {"error": f"target_type must be one of: {', '.join(sorted(VALID_BUDGET_TARGETS))}"})
            return
        threshold = body.get("threshold")
        if not _is_finite_number(threshold) or threshold <= 0:
            self._send_json(400, {"error": "threshold must be a finite positive number"})
            return
        scope = body.get("scope", {})
        if not isinstance(scope, dict) or set(scope) - {"project_id", "model"}:
            self._send_json(400, {"error": "scope may contain only project_id and model"})
            return
        for key, value in scope.items():
            if not isinstance(value, str) or not value.strip():
                self._send_json(400, {"error": f"scope.{key} must be a non-empty string"})
                return
        account_alias = body.get("account_alias", "default")
        if not isinstance(account_alias, str) or not account_alias.strip():
            self._send_json(400, {"error": "account_alias must be a non-empty string"})
            return
        notifications_enabled = body.get("notifications_enabled", False)
        if not isinstance(notifications_enabled, bool):
            self._send_json(400, {"error": "notifications_enabled must be a boolean"})
            return

        conn = self.server.get_connection()
        if notifications_enabled and not is_opt_in_enabled(conn, "notifications"):
            self._send_json(403, {"error": "Enable notifications consent before configuring budget notifications"})
            return
        budget_data = {
            "id": budget_id,
            "name": name.strip(),
            "period": period,
            "target_type": target_type,
            "threshold": threshold,
            "scope": {key: value.strip() for key, value in scope.items()},
            "notifications_enabled": notifications_enabled,
            "account_alias": account_alias.strip(),
        }
        try:
            with conn:
                saved = save_budget(conn, budget_data)
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            self._send_json(500, {"error": "Failed to save budget"})
            return
        self._send_json(200, saved)

    def _handle_delete_budget(self, budget_id: str) -> None:
        from token_ledger.metrics.providers import delete_budget

        if not budget_id or len(budget_id) > 512:
            self._send_json(400, {"error": "budget_id must be a non-empty string of at most 512 characters"})
            return
        conn = self.server.get_connection()
        try:
            with conn:
                deleted = delete_budget(conn, budget_id)
        except Exception:
            self._send_json(500, {"error": "Failed to delete budget"})
            return
        if deleted:
            self._send_json(200, {"deleted": budget_id})
        else:
            self._send_json(404, {"error": f"Budget not found: {budget_id}"})

    def _handle_post_quota_snapshot(self, body: Any) -> None:
        from token_ledger.metrics.providers import (
            VALID_QUOTA_STATUSES,
            VALID_WINDOW_TYPES,
            import_quota_snapshot,
        )
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Payload must be a JSON object"})
            return
        allowed_fields = {
            "provider", "account_alias", "access_route", "official_source",
            "timestamp", "measured_at", "window_type", "status",
            "limit_value", "used_value", "remaining_value", "reset_at",
            "units", "limit_tokens", "used_tokens", "remaining_tokens",
            "limit_requests", "used_requests", "percent_used", "raw_payload",
        }
        if set(body) - allowed_fields:
            self._send_json(400, {"error": "Unknown quota snapshot fields"})
            return
        for key in ("provider", "official_source"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                self._send_json(400, {"error": f"{key} must be a non-empty string"})
                return
        for key in ("account_alias", "access_route", "timestamp", "measured_at", "window_type", "status", "reset_at", "units"):
            if key in body and (not isinstance(body[key], str) or not body[key].strip()):
                self._send_json(400, {"error": f"{key} must be a non-empty string"})
                return
        if body.get("window_type", "month") not in VALID_WINDOW_TYPES:
            self._send_json(400, {"error": f"window_type must be one of: {', '.join(sorted(VALID_WINDOW_TYPES))}"})
            return
        if body.get("status", "available") not in VALID_QUOTA_STATUSES:
            self._send_json(400, {"error": f"status must be one of: {', '.join(sorted(VALID_QUOTA_STATUSES))}"})
            return
        for key in (
            "limit_value", "used_value", "remaining_value", "limit_tokens",
            "used_tokens", "remaining_tokens", "limit_requests", "used_requests", "percent_used",
        ):
            value = body.get(key)
            if value is not None and (not _is_finite_number(value) or value < 0):
                self._send_json(400, {"error": f"{key} must be a finite non-negative number or null"})
                return
        if "raw_payload" in body and not isinstance(body["raw_payload"], dict):
            self._send_json(400, {"error": "raw_payload must be a JSON object"})
            return

        try:
            conn = self.server.get_connection()
            with conn:
                quota_id = import_quota_snapshot(conn, body)
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            self._send_json(500, {"error": "Failed to persist quota snapshot"})
            return
        self._send_json(201, {"status": "created", "quota_id": quota_id, "snapshot": body})

    # -------------------------------------------------------------------------
    # Static File Serving & Path Traversal Guard
    # -------------------------------------------------------------------------

    def _serve_static_file(self, req_path: str) -> None:
        web_dist = self.server.web_dist_dir

        # If web_dist does not exist, serve embedded local fallback
        if not web_dist or not os.path.isdir(web_dist):
            self._serve_fallback_html()
            return

        base_dir = os.path.realpath(web_dist)

        # Unquote URL path and normalize
        clean_path = urllib.parse.unquote(req_path).lstrip("/")
        if clean_path == "" or clean_path.endswith("/"):
            clean_path = os.path.join(clean_path, "index.html")

        target_file = os.path.realpath(os.path.join(base_dir, clean_path))

        # Strict path traversal check: target must be inside base_dir
        if not (target_file == base_dir or target_file.startswith(base_dir + os.sep)):
            self._send_json(403, {"error": "Path traversal detected"})
            return

        # If path is not a file, try fallback index.html for SPA routing
        if not os.path.isfile(target_file):
            index_file = os.path.join(base_dir, "index.html")
            if os.path.isfile(index_file) and not clean_path.startswith("assets/"):
                target_file = index_file
            else:
                self._send_json(404, {"error": f"File not found: {req_path}"})
                return

        try:
            mime_type, _ = mimetypes.guess_type(target_file)
            mime_type = mime_type or "application/octet-stream"

            with open(target_file, "rb") as fh:
                content = fh.read()

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_json(500, {"error": f"Error reading static file: {str(e)}"})

    def _serve_fallback_html(self) -> None:
        html = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <title>Token Ledger Local Service</title>\n"
            "  <style>\n"
            "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #0f172a; color: #f8fafc; }\n"
            "    .card { background: #1e293b; border-radius: 8px; padding: 24px; max-width: 600px; margin: auto; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }\n"
            "    h1 { margin-top: 0; color: #38bdf8; font-size: 20px; }\n"
            "    code { background: #334155; padding: 2px 6px; border-radius: 4px; font-size: 13px; }\n"
            "    p { line-height: 1.5; font-size: 14px; color: #cbd5e1; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <div class=\"card\">\n"
            "    <h1>Token Ledger Local Service</h1>\n"
            "    <p>The Token Ledger loopback server is running.</p>\n"
            "    <p>API endpoints are active at <code>/api/v1/...</code>.</p>\n"
            "    <p>Build the frontend UI into <code>web/dist</code> to serve the dashboard locally with zero external network access.</p>\n"
            "  </div>\n"
            "</body>\n"
            "</html>\n"
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)


def create_server(
    db_path: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    web_dist_dir: Optional[str] = None,
) -> TokenLedgerServer:
    """Create a configured TokenLedgerServer instance without blocking."""
    if not is_loopback(host):
        raise ValueError(f"Refusing to bind non-loopback host: {host}")

    server = TokenLedgerServer((host, port), TokenLedgerRequestHandler, db_path, web_dist_dir)
    return server


def start_server(
    db_path: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    web_dist_dir: Optional[str] = None,
) -> None:
    """Start and run the HTTP server indefinitely."""
    server = create_server(db_path, host, port, web_dist_dir)
    print(f"Token Ledger local service listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()
