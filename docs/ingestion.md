# Token Ledger Ingestion Engine (WP04)

This document describes the incremental log file ingestion architecture, checkpoint management, privacy enforcement, and health monitoring interfaces implemented in `token_ledger.ingest.runner`.

---

## 1. Overview & Core Principles

The ingestion engine processes raw stream logs emitted by coding agent harnesses (such as Claude Code and Oh-My-Pi) into the canonical SQLite store.

Key guarantees:
1. **Durable Checkpoints:** Tracks file byte offsets, line indices, file size, modification timestamps, and generation keys in `source_checkpoints`.
2. **Incomplete-Line Safety:** Reads only up to the last complete newline byte (`\n`). Partial trailing lines written mid-turn remain unconsumed until the next scan completes them.
3. **Rotation & Truncation Detection:** When a log file is truncated (current size < last recorded byte offset) or rotated, a new file generation key (`gen_<N>_<mtime>`) is assigned, and offsets reset cleanly to 0 without missing records.
4. **Strict Privacy Before Persistence:** All accounts, file locators, metadata, and raw snippets pass through `token_ledger.privacy` sanitizers prior to SQLite insertion. User home directories (`~`) and raw absolute paths are never persisted.
5. **No Blind Deduplication / Provider Guessing:** Canonical identity is strictly scoped by `(provider, account_alias, provider_request_id)`. Providers are never guessed solely from display model strings.
6. **Non-Silent Parse Errors:** Corrupted or invalid JSON lines are never dropped. They are recorded in `source_observations` with `parse_status = 'corrupt'`, secret-redacted snippets, and incremented error counters.
 7. **Foreign Key Integrity:** SQLite foreign keys are enforced (`PRAGMA foreign_keys = ON;`). Parent entries in `projects` and `sessions` are guaranteed to exist prior to child `canonical_requests` or `events` insertions.
 8. **No Fabricated Requests on Out-of-Order Parent Linkages:** When a child request arrives before its parent, the engine never manufactures a fake placeholder parent request. Instead, the child's `parent_request_id` remains `NULL`, `is_unresolved_overlap` is set to `1`, the parent reference is staged in `staging_unresolved_parent_links` and retained in safe metadata, and the link automatically resolves when the true parent observation arrives.
 9. **No Fabricated Rate Cards or Pricing Versions:** If a record references an unknown or unvalidated pricing version, the foreign key `pricing_version_id` remains `NULL` (unpriced) rather than fabricating nonexistent pricing rows. The raw version string is safely preserved in metadata.
---

## 2. Public API Interface

Module: `token_ledger.ingest.runner`

### `ingest_file(...) -> IngestResult`

```python
def ingest_file(
    conn: sqlite3.Connection,
    path: str,
    source_adapter: str,
    account_alias: Optional[str] = None,
    provider: Optional[str] = None,
    access_route: Optional[str] = None,
    origin_harness: Optional[str] = None,
) -> IngestResult
```

- **`conn`**: Open SQLite connection migrated to the latest schema.
- **`path`**: Filesystem path to the log file (e.g. `~/.claude/logs.jsonl` or `~/.omp/logs.jsonl`).
- **`source_adapter`**: Recognized source adapter (`claude_code`, `omp`).
- **`account_alias`**: Optional user account alias. If omitted, defaults to a source-local scoped alias (`{adapter}_local`), preventing accidental cross-source merges.
- **`provider`**: Optional explicit provider override.
- **`access_route`**: Optional routing tag (e.g. `direct`, `bedrock`, `openrouter`).
- **`origin_harness`**: Optional harness tag override (defaults to `claude-code` or `oh-my-pi`).

#### `IngestResult` Attributes:
- `file_locator`: Sanitized locator string.
- `lines_read`: Number of valid lines evaluated.
- `observations_inserted`: Physical observations recorded.
- `canonical_requests_inserted`: New canonical requests stored.
- `canonical_requests_updated`: Existing canonical requests updated on conflict.
- `events_inserted`: Trace events recorded.
- `parse_errors`: Number of corrupt/failing lines.
- `bytes_processed`: Number of bytes consumed from disk.
- `file_generation`: Active generation token.
- `rotation_or_truncation_detected`: Boolean indicating if truncation/rotation occurred.
- `status`: `'ok'`, `'partial'` (if parse errors occurred), or `'error'`.
- `error_message`: Most recent error description.

---

### `get_sync_health(...) -> List[SyncHealth]`

```python
def get_sync_health(
    conn: sqlite3.Connection,
    source_adapter: Optional[str] = None,
    file_locator: Optional[str] = None,
) -> List[SyncHealth]
```

Returns diagnostic synchronization metrics joining `source_checkpoints` and `source_observations`:
- `source_adapter`, `file_locator`, `origin_harness`, `account_alias`
- `file_generation`, `file_size`, `last_byte_offset`, `last_line_index`
- `last_scan_at`, `last_success_at`, `status`, `error_count`, `last_error`
- `total_observations`: Total observations recorded for this source.
- `newest_observation_time`: Timestamp of the latest observed record.
