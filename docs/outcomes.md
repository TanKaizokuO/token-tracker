# Token Ledger Outcome Tracking & Effectiveness Scoring (WP18)

This document specifies the persistence architecture, manual task labeling, local Git linkage with privacy consent gating, and effectiveness metrics implementation in `token_ledger.outcomes` and `token_ledger.metrics.outcomes`.

---

## 1. Architectural Scope & Principles

1. **Shared Filter Row Access:**
   - Outcome and task efficiency analytics operate strictly on the canonical request set returned by `token_ledger.query.filtered_request_rows(conn, envelope)`.
   - Never runs un-scoped queries or divergent filtering logic.

2. **Persistence Schema & Extended Metadata:**
   - Persists into canonical SQLite table `manual_settings_and_outcomes` (001_initial.sql).
   - Valid completion statuses: `'complete'`, `'partial'`, `'unsuccessful'`, `'abandoned'`.
   - Standard task labels: `'debugging'`, `'feature'`, `'refactoring'`, `'research'`, `'review'`, `'docs'`, `'quick_question'`.
   - Structured envelope in `notes` column preserves extended attributes (`was_corrected`, `was_restarted`, `user_ownership`, `source`, `version`, `git_metadata`) without requiring schema modifications.

3. **Strict Separation of Manual vs. Inferred Outcomes:**
   - `is_inferred = 0` designates user-owned records; manual ratings (`manual_rating`) are evaluated in `user_ratings` aggregates.
   - `is_inferred = 1` designates automated inferences (`rule`, `ai`, `heuristic`); confidence is evaluated in `inference_confidence` aggregates.
   - User ratings and inference confidences are never conflated into a single pooled score.

4. **Strict Privacy Consent Gate for Local Git Linkage:**
   - Controlled by versioned opt-in `git_linkage` via `token_ledger.privacy.is_opt_in_enabled`.
   - **Consent Disabled:** PERFORMS NO GIT COMMAND. Subprocess execution is strictly skipped.
   - **Consent Enabled:**
     - Inspects ONLY local Git metadata on an explicitly provided repository path.
     - Never guesses or auto-discovers repository paths.
     - Extracts: commit hash, changed file counts, line insertions, line deletions, and optional test outcome.
     - File paths are anonymized to safe token aliases (e.g. `file_a1b2c3d4.py`); user directory structures and file names are never leaked.
     - Strictly no source code reading, no transcripts, and no network operations.

5. **Configurable Sample Adequacy & Abstention Semantics:**
   - Enforces a declared sample threshold (default: 3 completed tasks, configurable via query envelope).
   - When completed tasks for a slice or group are below threshold:
     - `adequate_sample = false`
     - Per-completion metrics (`tokens_per_completed_task`, `time_per_completed_task_seconds`, `requests_per_completed_task`, `cost_per_success_usd`) are strictly `None`.
     - Explicit disclaimer returned in `abstention_reason` explaining that sample size is sparse.
   - Never invents adequacy or extrapolates per-completion efficiency on sparse data. Counter completeness is an independent gate: a derived request total requires all six disjoint billable components (fresh input, cache read, cache write 5m, cache write 1h, cache write unknown TTL, output); reasoning is not added. Missing components are not zero. An explicitly supplied total of `0` remains zero rather than triggering component fallback.

6. **Priced Coverage Verification:**
   - `cost_per_success_usd` and `cost_per_success_inr` require 100% priced coverage across all canonical requests in completed tasks.
   - Unpriced completed tasks cause cost per success to evaluate to `None` with an explicit warning.

---

## 2. Public Module APIs

### `token_ledger.outcomes`
- `create_outcome(...) -> OutcomeRecord`
- `get_outcome(conn, outcome_id) -> Optional[OutcomeRecord]`
- `get_outcome_for_entity(conn, entity_type, entity_id) -> Optional[OutcomeRecord]`
- `list_outcomes(conn, ...) -> List[OutcomeRecord]`
- `update_outcome(conn, outcome_id, ...) -> Optional[OutcomeRecord]`
- `delete_outcome(conn, outcome_id) -> bool`
- `inspect_git_metadata(repo_path, conn=None, consent_granted=None, commit_ref='HEAD') -> Dict[str, Any]`
- `link_git_outcome(conn, outcome_id, repo_path, commit_ref='HEAD', test_outcome=None) -> OutcomeRecord`

### `token_ledger.metrics.outcomes`
- `analyze(conn: sqlite3.Connection, envelope: Dict[str, Any]) -> Dict[str, Any]`: preserves nullable token totals from filtered rows. An absent total can be derived only from all six known components. Any unknown contributing request total makes its task total unavailable; any unknown contributing task total makes the aggregate unavailable. Token-per-completion metrics abstain on incomplete completed-task totals even when sample adequacy passes; request/time denominators and their adequacy rules are unchanged.

---

## 3. Dimensional Slices

- `task_type`: debugging, feature, refactoring, research, review, docs, quick_question; token totals remain null if any contributing task total is unknown.
- `model`: model effectiveness scoring (tokens/completed task, time/task, cost/success, rating), with the same token-completeness gate.
- `harness`: harness efficiency comparison (tokens, duration, cost per completed task by harness), with the same token-completeness gate.
- `completion_status`: breakdown across complete, partial, unsuccessful, abandoned, unlabeled; status token totals remain null if any contributing task total is unknown. Overall token totals and per-completion token metrics likewise never present partial known sums as exact totals.
- `inference_method`: distribution and confidence across manual_user, rule, ai, heuristic.

## Manual entry in Sessions

Open a session and expand **Manual task and outcome**. Choose the session or a
listed canonical request, enter the account alias, task label and completion
status, and optionally provide a rating and correction/restart feedback.
Records are user-owned and marked with API provenance; inferred records are
displayed but not editable in this form. Blank ratings and unknown feedback are
not treated as zero or false. On edit, clearing the rating sends an explicit
`null`; omitting `manual_rating` in a PATCH preserves its existing value.
The outcome query groups by session and uses a linked request outcome only
when no session outcome exists. The selector lists the
requests currently loaded in the session detail (up to 200, subject to filters).
