# Privacy Controls, Redaction, and Retention

Token Ledger is designed from the ground up as a **local-first, loopback-only** observability tool. This document defines the privacy architecture, redaction boundaries, explicit versioned opt-in policies, retention and deletion cascades, cryptographic hashing risk disclosures, and data protection guarantees.

---

## 1. Core Principles

1. **Local-First & Loopback Only:**
   - Token Ledger runs strictly on local loopback (`127.0.0.1`).
   - Zero telemetry, zero external network requests, and no CDN dependencies.
2. **Redact-Before-Persistence & Redact-Before-Export:**
   - Redaction is applied **prior** to database insertion and prior to any export generation (CSV, JSON, HTML, PDF, PNG).
   - Sensitive data never reaches persistent storage unredacted.
3. **Raw Data Untouched:**
   - Source log files on disk (e.g., Claude Code `.jsonl` files, Oh-My-Pi session logs) are strictly read-only.
   - Token Ledger **never** modifies, overwrites, or deletes raw source log files. Retention and deletion operations apply exclusively to Token Ledger's internal SQLite database and generated export snapshot files.
4. **Never Dedupe on Content or Timestamps:**
   - Deduplication relies strictly on canonical identity scoping: `(provider, account_alias, provider_request_id)` or deterministic observation provenance.
   - Content hashing and fuzzy timestamp clustering are never used for deduplication.

---

## 2. Default Exclusion Rules

By default, Token Ledger collects and persists only operational token and execution metrics. The following are **excluded by default**:

- **No Prompt or Response Bodies:** User prompts and assistant responses are omitted or replaced with `[REDACTED_CONTENT]`.
- **No Code Snippets:** Source code submitted to or received from models is not stored.
- **No Tool Inputs or Outputs:** Detailed tool arguments, file contents, command strings, stdout, and stderr are stripped.
- **No Credentials or API Keys:** API keys (`sk-...`, `sk-ant-...`, `AIza...`), tokens (`ghp_...`, `Bearer ...`), private keys (`-----BEGIN PRIVATE KEY-----`), and environment passwords are comprehensively detected and replaced with `[REDACTED_SECRET]`.

---

## 3. Explicit Versioned Opt-In Policies

Token Ledger defines four distinct, versioned user consent toggles. All toggles default to **disabled (`0` / `False`)**.

| Setting Key | Description | Default | Consent Gate |
|---|---|---|---|
| `content_capture` | Captures user prompt bodies, assistant response text, and tool input/output metadata. Sensitive secrets (API keys, passwords) remain scrubbed even when enabled. | `Disabled` | Required for viewing full transcripts or detailed tool content. |
| `content_fingerprints` | Permits SHA-256 fingerprints for content already present in source events and explicit source-content evidence used for repetition analysis. It does not authorize reading local files. | `Disabled` | Required before any content fingerprint is created or used. |
| `git_linkage` | Links sessions and token usage to local Git commit hashes, branch names, and diff statistics. | `Disabled` | Required for source-control attribution. |
| `notifications` | Local desktop or system alert hooks when token burn or error thresholds are reached. | `Disabled` | Required for alert dispatching. |

The HTTP privacy endpoint accepts only these four setting keys and requires a boolean consent value. Budget notification delivery can be enabled only while the `notifications` opt-in is active.

### Policy Versioning
- Current policy version: `PRIVACY_POLICY_VERSION = "1.0"`.
- Each opt-in consent record is version-stamped in the `privacy_settings` table.
- If the policy version changes, existing consent is invalidated until the user explicitly re-consents to the updated policy terms.

---

## 4. Cryptographic Hashing Risks: Equality Leakage & Dictionary Attacks

When `content_fingerprints` is evaluated, Token Ledger explicitly discloses the mathematical and cryptographic limitations of one-way hashes:

1. **Equality Leakage:**
   - SHA-256 hashes are deterministic. If identical text (such as common prompts, standard code snippets, or system instructions) is processed across different sessions or accounts, the resulting hashes are identical.
   - An attacker or shared dashboard viewer with access to content hashes can determine that two sessions processed the exact same content without needing to decrypt the hash.
2. **Dictionary & Rainbow Table Attacks:**
   - Low-entropy inputs (short commands, small file contents, single-word prompts, known API error messages) have small search spaces.
   - An adversary can pre-compute hashes of common phrases, source files, or dictionary words to recover the original plaintext from content hashes.
3. **Mitigations in Token Ledger:**
   - Content hashing is **blocked by default**. `hash_content_fingerprint()` returns `None` unless `content_fingerprints` opt-in is active.
   - Source file locators hash only the **path locator string** (e.g., `loc_<hash>`), never file contents. The trace analyzer does not open source paths; it classifies repeated reads only from an opt-in source content digest or explicit source-change metadata. Without either, content evidence is `unknown`.

---

## 5. Safe Non-Secret Account Aliasing & Source Locators

### Account Aliases
- Multi-account logs may contain sensitive user identifiers, email addresses (`user@domain.com`), local system paths (`/home/username/...`), or API credentials.
- `sanitize_account_alias()` maps sensitive identifiers into deterministic pseudonymous aliases (`acct_<sha256[:12]>`), while retaining safe, non-secret labels (`default`, `work`, `personal`).
- Account identity remains stable for query grouping and deduplication without exposing personal data.

### Source Locators
- Source paths are normalized to strip absolute user home directories (`~/...`).
- When opaque locators are required, `sanitize_file_locator(..., hash_locator=True)` hashes the path string (`loc_<sha256[:16]>`).
- Content hashing is strictly separated from locator hashing.

---

## 6. Retention and Cascading Deletion

The retention engine (`cascade_delete_retention`) supports an age cutoff (`older_than_iso`) or the explicit `session_id`, `project_id`, and `account_alias` scopes. Retention applies to Token Ledger's managed data; source logs remain read-only. For generated snapshots in `export_inventory`, scope matching reads canonical nested `filters.session` and `filters.project` from `query_filter_json`; values within a dimension are OR alternatives, while dimensions intersect against actual target-session identities. Null project identities match canonical `unattributed`. Unknown or malformed scope metadata is handled conservatively, and a report-owner account default is not treated as a canonical query restriction.

### HTTP retention controls

- `GET /api/v1/privacy/retention` reports the saved age period and supported scopes. `POST` accepts only `retention_days`, an integer from 1 through 36,500.
- `POST /api/v1/privacy/retention/run` accepts exactly one explicit scope, or no scope when a valid age period is configured. `older_than_iso` must be timezone-aware and not in the future. The optional `delete_snapshot_files` flag is boolean and defaults to `true`.
- The service supplies its own managed root; callers cannot provide a path. A session-scoped run removes that session and its associated outcomes while leaving unrelated sessions and outcomes intact. For scoped snapshot deletion, explicitly disjoint export scopes are preserved; scopes containing the target and genuinely unscoped aggregates that may include it are invalidated. A session-filtered snapshot disjoint from a target project is also preserved. This behavior is evidenced by isolated synthetic HTTP retention, inventory/download and canonical-query checks, not production/private-history deletion acceptance.

### Database Cascades
Under `PRAGMA foreign_keys = ON;`, deleting a session cascades cleanly through:
- `sessions` → `prompts`, `canonical_requests`, `events`
- `canonical_requests` → `request_observation_links`, `normalized_usage`, `financial_facts`
- `manual_settings_and_outcomes` (purged by entity ID)
- `archive_partitions` (rollups purged by end time)
- `alerts_and_reports` (purged by generation cutoff)
- `export_inventory` (affected export inventory records and local snapshot files, preserving explicitly disjoint scopes). For this artifact type, legacy top-level `session_id`, `session_ids`, `project_id`, and `account_alias` fields in `query_filter_json` remain intentionally supported; canonical nested fields take precedence when present. This compatibility statement does not extend to `alerts_and_reports` or `archive_partitions`. Existing timestamp-cutoff behavior for genuinely unscoped session exports and age-based retention remains in effect.

### Schema Entity Identification
The canonical store schema (`001_initial.sql`) defines 21 tables. Virtual entities referenced in high-level specifications:
- **`rollups`**: Stored under the `archive_partitions` table.
- **`insights`**: Dynamically computed during queries or stored under `alerts_and_reports` and `manual_settings_and_outcomes`.
These are identified as missing independent tables in `RetentionResult.missing_schema_entities` rather than altering the migration schema.

### Path Traversal Defense
When deleting local snapshot files referenced in `export_inventory.file_path`:
- Every target path must pass `verify_path_within_root(path, managed_root)`.
- Rejects relative directory traversal sequences (`../`), absolute paths outside `managed_root` (`/etc/passwd`), and attempts to delete the root folder itself.
- Violating paths raise `PathTraversalError` and are recorded in `skipped_paths_due_to_traversal`.
- Deletion is safely restricted to files within the designated managed export directory.

---

## 7. Python API Reference

Module: `token_ledger.privacy`

- `redact_secrets(text: str, replacement="[REDACTED_SECRET]") -> str`
- `sanitize_metadata(data: Any, allowed_keys=None, opt_ins=None) -> Any`
- `redact_for_export(dataset: Any, opt_ins=None, allowed_columns=None) -> Any`
- `is_opt_in_enabled(conn_or_settings, opt_in_key, expected_version="1.0") -> bool`
- `load_privacy_settings(conn: sqlite3.Connection) -> PrivacySettings`
- `save_privacy_setting(conn: sqlite3.Connection, key: str, enabled: bool, ...) -> None`
- `sanitize_account_alias(raw_account: Optional[str], default="default", salt="") -> str`
- `sanitize_file_locator(file_path: str, base_dir=None, hash_locator=False, salt="") -> str`
- `hash_content_fingerprint(content: str, opt_in=False, salt="") -> Optional[str]`
- `verify_path_within_root(target_path: str, managed_root: str) -> str`
- `delete_snapshot_file(file_path: str, managed_root: str) -> bool`
- `cascade_delete_retention(conn, managed_root, older_than_iso=None, ...) -> RetentionResult`
- `ProjectInclusionPolicy(allowed_projects=..., excluded_projects=..., ...)`
- `SourceInclusionPolicy(allowed_sources=..., excluded_sources=...)`
