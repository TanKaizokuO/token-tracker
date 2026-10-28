# Token Ledger Local Service & Security Architecture

The Token Ledger local service (`token_ledger.api`) provides a hardened, local-first HTTP interface for the analytics dashboard and external scripting. It is designed to run completely offline on the user's workstation without exposing sensitive telemetry or data to the local network or external adversaries.

---

## 1. Security Architecture & Threat Model

### 1.1 Exclusively Loopback-Bound
- **Host Validation:** The server strictly binds to loopback interfaces (`127.0.0.1`, `::1`, or `localhost`).
- **Refusal to Bind Non-Loopback:** Any attempt to bind to `0.0.0.0`, LAN interfaces, or arbitrary hostnames is rejected before socket creation.
- **DNS Rebinding & Host Spoofing Defense:**
  Every inbound HTTP request must present a `Host` header that resolves exclusively to loopback and specifies the exact bound port (e.g. `127.0.0.1:8765`). Requests with arbitrary hostnames or mismatched ports are immediately rejected with HTTP 400/403.

### 1.2 Zero CORS & Same-Origin State Mutation
- **No Cross-Origin Resource Sharing (CORS):** The server does not emit `Access-Control-Allow-Origin` headers. Preflight `OPTIONS` requests are rejected with HTTP 405 Method Not Allowed.
- **Same-Origin Verification:** On all state-changing HTTP requests (`POST`, `PUT`, `DELETE`, `PATCH`), the `Origin` header (or `Referer` if omitted) must strictly match the server's loopback scheme, hostname, and port.

### 1.3 Strict Session Cookies & CSRF Tokens
- **Scoped Cookies:** The session cookie `tl_session` is set with `SameSite=Strict`, `HttpOnly`, and scoped to `Path=/api`. It is not transmitted on broad root requests or cross-site contexts.
- **Per-Session CSRF Tokens:** State-changing requests must provide an `X-CSRF-Token` header that cryptographically matches the session token generated at bootstrap (`GET /api/v1/bootstrap`).

### 1.4 Path Traversal & Injection Prevention
- **Path Traversal Guard:** Static asset requests are strictly verified against the canonical realpath of the root UI directory (`web/dist`). Paths attempting to escape the root (e.g., `..`, `%2e%2e`) receive HTTP 403 Forbidden.
- **No Arbitrary SQL or Caller-Provided Paths:** The service exposes no endpoints that accept arbitrary SQL or caller-supplied deletion paths. All mutations use parameterized queries and typed validation. Retention may delete generated snapshot files only inside the server-managed root; raw source logs are never deleted.

### 1.5 Domain-Backed Resource APIs

Resource routes persist through the existing domain modules rather than a separate API-only store:

| Resource | Routes | Boundary |
|---|---|---|
| Outcomes | `GET/POST/PATCH/DELETE /api/v1/outcomes` | Manual, user-owned records use `token_ledger.outcomes` validation and CRUD. |
| Budgets | `GET/POST/DELETE /api/v1/budgets` | Uses provider budget persistence; enabling notifications requires the `notifications` opt-in. |
| Quotas | `POST /api/v1/quotas/snapshot` | Imports through the provider quota domain validator; `provider` and `official_source` are required, and normalized provenance is persisted. |
| Privacy | `GET/POST /api/v1/privacy` | Accepts only the four explicit opt-in keys documented in the privacy policy. |
| Retention | `GET/POST /api/v1/privacy/retention`, `POST /api/v1/privacy/retention/run` | Accepts the documented scoped-retention controls; arbitrary managed paths are not client inputs. |

All state-changing requests remain subject to the Host, same-origin, and CSRF checks above.

---

## 2. CLI Usage

The service and data operations are managed through `token_ledger`:

### 2.1 Serve Command
Starts the loopback HTTP server:
```bash
PYTHONPATH=src python3 -m token_ledger serve --db token_ledger.db --host 127.0.0.1 --port 8765
```
Options:
- `--db PATH`: Path to SQLite database file (required)
- `--host HOST`: Host interface to bind (default: `127.0.0.1`; loopback only)
- `--port PORT`: Port number to bind (default: `8765`)
- `--web-dist PATH`: Path to static bundled UI directory (default: `web/dist`)

### 2.2 Ingest Command
Incrementally ingests an explicit log file into the database with zero automatic discovery:
```bash
PYTHONPATH=src python3 -m token_ledger ingest --db token_ledger.db --file /path/to/session.jsonl --adapter omp --harness omp
```

### 2.3 Archive Command
Imports aggregate partition JSON bundles into the database:
```bash
PYTHONPATH=src python3 -m token_ledger archive --db token_ledger.db --file /path/to/archive_bundle.json
```

### 2.4 Migrate Command
Applies pending database schema migrations:
```bash
PYTHONPATH=src python3 -m token_ledger migrate --db token_ledger.db
```

---

## 3. Local Dashboard Deployment

When built, the frontend application resides in `web/dist`. The local server serves these files directly from the filesystem with offline MIME types and SPA fallback. No CDN assets, external scripts, or remote network calls are permitted.
