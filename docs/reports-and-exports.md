# Reports and local exports

Token Ledger stores report definitions in `alerts_and_reports` with `item_type = 'report'`. They are separate from alerts. Create or update them with `save_report_definition`, read them with `get_report_definition` / `list_report_definitions`, and remove them with `delete_report_definition`. A definition contains its name, frequency, versioned query `filter_state`, optional threshold and pricing-assumption objects, project/account scope, and execution timestamps.

## Query and scheduling behavior

A report runs through the canonical `token_ledger.query.execute_query` engine using contract version `1`; it does not issue a parallel analytics query or fetch data over the network. The saved filter state may be a query envelope or an object containing `query`. Project-scoped definitions constrain the query to their `project_id`. The report retains the normalized query, returned summary and rows, coverage, missingness, warnings, metric definition, and the pricing/FX provenance returned by the shared engine. A caller-supplied `pricing_fx` mapping is retained as worker provenance; it does not override historical pricing or change query values.

`get_due_reports(conn, now_iso=None)` identifies due scheduled reports. `manual` reports are never due automatically. Daily and weekly recurrences retain the definition's local wall-clock time in the configured schedule timezone (`schedule_timezone`, `timezone`, or `time_range.timezone`; default `UTC`). Calendar arithmetic is used across daylight-saving changes, not fixed 24-hour/168-hour durations. Monthly recurrences use the original local day and clamp to the final day of shorter months (for example, January 31 → February 28/29). A missed interval runs once when the local worker next checks; missed intervals are not replayed as a burst.

### Equivalent prior-period comparison

Scheduled reports and export snapshots automatically compute an equivalent prior-period comparison across the shared query engine:
- **Duration and Timezone Alignment:** The comparison period matches the current period's exact duration and timezone (`schedule_timezone` or `time_range.timezone`, defaulting to `UTC`). Daily reports compare the preceding calendar day; weekly reports compare the preceding Monday-to-Sunday calendar week; monthly reports shift one calendar month with month-end day clamping (e.g. March 31 → February 28/29); bounded manual reports shift earlier by the exact duration of `[start, end)`.
- **Missing Prior Data Handling:** If the prior equivalent period lacks observed data (zero observed requests), the comparison outputs `unavailable` rather than manufacturing synthetic zero deltas, phantom reductions, or unevidenced trend claims.
- **Export Representation:**
  - **JSON:** Contains `"comparison": "unavailable"` when prior data is missing, or the structured comparison object containing `period_a`, `period_b`, `deltas`, and assumptions.
  - **CSV:** Emits metadata row `comparison,unavailable` when prior data is missing.
  - **HTML:** Renders `<pre>unavailable</pre>` inside the equivalent prior-period comparison section.
  - **PDF & PNG:** Formats comparison status as `unavailable` in the summary table and metadata.
- **Confounder and Disclaimer Controls:** Like all Token Ledger comparative analytics, comparison objects include non-causal observational disclaimers, flag DST shifts and model mix changes, and preserve missing prices and unobserved counters as unavailable.

`execute_report(conn, report_def_or_id, formats=None, managed_root=None, pricing_fx=None, now_iso=None)` runs one report. `formats` may select any non-empty subset of `csv`, `json`, `html`, `pdf`, and `png`; omitting it selects all five. `run_due_reports_scheduler(conn, managed_root=None, pricing_fx=None, now_iso=None)` is the no-network local batch worker for every currently due report.

The local CLI opens and migrates the SQLite database before invoking that same scheduler. From a repository checkout, run:

```sh
PYTHONPATH=src python3 -m token_ledger reports run-due \
  --db /path/to/token-ledger.sqlite3 \
  --managed-root /path/to/exports \
  --now 2026-10-26T12:00:00Z
```

`--managed-root` and `--now` are optional; the output directory defaults to project-local `data/exports/`, and omitted `--now` uses the current time. Standard output is JSON shaped as `{"count": N, "executed": [ReportExecutionResult, ...]}`.

### Unattended Scheduling: Daemon, Systemd, and Cron

For headless servers or unattended operation, the reports scheduler can be run continuously via the daemon command or integrated with system supervisors:

```sh
# Run persistent polling daemon (checks for due reports every --interval seconds)
PYTHONPATH=src python3 -m token_ledger reports daemon --db /path/to/token-ledger.sqlite3 --interval 300

# Print systemd unit and timer templates
python3 -m token_ledger reports daemon --systemd --interval 300

# Print crontab configuration entry
python3 -m token_ledger reports daemon --cron --interval 300
```
 Generated exports are local files; `/data/exports/` is git-ignored.

## REST API

The loopback HTTP API uses the existing Host validation for all routes. `POST` and `DELETE` requests also require a same-origin `Origin` or `Referer` and a session-bound CSRF token. These endpoints use the shared report and export domain; the HTTP layer does not implement a second report engine.

- `GET /api/v1/reports/definitions` returns `{"reports": [...]}` and optionally accepts `project_id` and `account_alias` exact-match filters. `POST /api/v1/reports/definitions` accepts a report definition and returns `{"report": ...}`. Returned definitions include `item_type: "report"`, `item_id`, `name`, `frequency`, `filter_state`, `threshold_config`, `pricing_assumptions`, `project_id`, `account_alias`, `last_generated_at`, `created_at`, and `updated_at`.
- `DELETE /api/v1/reports/definitions/{id}` returns `{"deleted": "..."}` when a report exists and `404` otherwise.
- `POST /api/v1/reports/generate` accepts `{"report_id": "...", "formats": ["json"]}`; `formats` may be any non-empty subset of `csv`, `json`, `html`, `pdf`, and `png`. The response contains `report_id`, `generated_at`, `exports`, and `coverage`.
- `GET /api/v1/reports/scheduler/due` returns `{"due_reports": [...]}`. `POST /api/v1/reports/scheduler/run-due` accepts `{}` and returns `{"executed": [...], "count": N}`; each execution contains the same fields as report generation.
- `GET /api/v1/exports?limit=100` returns `{"exports": [ExportRecord, ...]}`; `limit` defaults to 100 and is bounded from 1 through 1,000. `GET /api/v1/exports/{id}/download` streams the inventoried file as the response body, not JSON.

Generation is local. It updates `last_generated_at` only after all selected files and inventory records are committed. If file generation or database recording fails, created snapshots are removed and the inventory transaction rolls back. Export downloads require an inventory record and reject paths outside the configured managed export root or through symlinks.

## Reports & Settings page

The local dashboard page uses the report-definition, scheduler, export-inventory, privacy, budget, saved-view, and layout resources documented by the API. Creating a report or saved view snapshots the shell's current shared harness, time range, filters, search, and archive setting; it does not change the active dashboard query. Report execution uses the canonical summary query and accepts any non-empty selection of CSV, JSON, HTML, PDF, and PNG. Downloads link to the managed-root download route rather than exposing export paths.

Scheduled definitions are persisted locally. The page's **Run due locally** action invokes the scheduler endpoint on demand; it is not a background worker. Export inventory rows show format, creation time, size, and redaction state, while omitting managed file paths.

The page's source-health view displays the shared coverage query and health endpoint, without source locators or raw checkpoint errors. Pricing and FX are query evidence, not an editor: where the query response omits an effective date or currency label, the page does not infer one. Privacy controls use explicit confirmations for opt-ins; project inclusion, retention settings, and budget rules are saved using their respective local APIs. Running retention is destructive and requires confirmation. Budget notification rules require the notifications opt-in; saving a rule does not itself dispatch an alert. Saved views restore the shared query, and page layouts store a selected Reports section plus the shared query snapshot.


## Output safety and bounds

The Python package and CLI default to the project-local `data/exports/`; the loopback HTTP server uses its configured `export_root`, which defaults to the `exports/` child of `.token-ledger-managed/` beside the SQLite database. `data/exports/` is excluded from version control. Generated files use opaque IDs, are written atomically with owner-only temporary-file permissions, and are checked with the privacy module's managed-root path verifier. No source log paths are included in report data. Before any snapshot or inventory JSON write, privacy redaction removes secrets and private bodies, and path-valued fields are replaced with `[REDACTED_PATH]`. Report queries are capped at 1,000 returned rows and each file is capped at 2 MiB; larger direct-export inputs fail rather than being silently truncated.

- **CSV:** uses standard CSV quoting and prefixes spreadsheet formula-leading values (`=`, `+`, `-`, `@`, tab, carriage return, including after leading whitespace) with an apostrophe. Redaction occurs before CSV serialization.
- **JSON:** contains the redacted report envelope, including query, summary, rows, coverage, missingness, pricing/FX provenance, and warnings.
- **HTML:** is a complete offline document with inline CSS/SVG only—no scripts, remote resources, or external fonts. It displays the actual query summary, chart, returned rows (or summary values when there are no detail rows), query filters/time range, coverage, pricing provenance, and warnings. All dynamic text is HTML-escaped. When the query's observed count exceeds its returned page, the page labels the returned count; it does not invent details.
- **PDF and PNG:** are real local Matplotlib renders, not renamed HTML. They contain the report title, query/filter summary, chart, summary values, and an explicitly labeled fixed-size table preview (up to eight returned rows and six columns). The full bounded result is available in CSV, JSON, and HTML. Rendering performs no browser launch, network request, or remote-resource resolution.

The export inventory stores each format's opaque ID, managed output path, SHA-256, byte size, redaction flag, normalized query filter, pricing/FX provenance, creation time, and session/project/account scope. Migration `003_export_integrity.sql` adds the hash and size fields without changing immutable migrations 001 or 002. Historical inventory rows retain the migration defaults (`sha256_hash = ''`, `byte_size = 0`) because no previous digest or size is inferred; newly generated exports record their actual content hash and byte count. Existing session/project retention cascades continue to delete the inventory row and use the privacy helper's descriptor-relative managed-root unlink for the file.

## Python surface

```python
from token_ledger.export import (
    ReportDefinition,
    execute_report,
    get_due_reports,
    run_due_reports_scheduler,
    save_report_definition,
)

report = save_report_definition(conn, ReportDefinition(
    name="Daily token summary",
    frequency="daily",
    filter_state={
        "contract_version": "1",
        "metric_id": "summary",
        "filters": {},
        "time_range": {"timezone": "America/New_York"},
    },
))
result = execute_report(conn, report.item_id, formats=["html", "json"])
```

The exported names and shared scheduler entry point are available from `token_ledger.export`. The Python package, CLI, and loopback REST API are supported report surfaces.
