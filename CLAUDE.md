# token-tracker (Token Ledger)

Local token and cost ledger for oh-my-pie (`omp`) and Claude Code usage. It reads their session logs into a local SQLite database, prices the usage from `pricing.json`, and serves a React dashboard. Costs come from `pricing.json`, not from omp's own cost numbers, which are wrong.

## Layout
- `src/token_ledger/` - the Python package (stdlib plus matplotlib for PNG/PDF export).
  - `__main__.py` - CLI (`python -m token_ledger`).
  - `pricing.py` - rates and currency from `pricing.json`.
  - `ingest/` - log ingestion, overlap detection, session-id backfill.
  - `metrics/` - token, model, provider, efficiency, trace, forecast metrics.
  - `query/engine.py` - query layer used by the API.
  - `api/server.py` - loopback HTTP service for the dashboard.
  - `store/` - `database.py` (`open_database`) and `migrations/`.
  - `export/` - report and snapshot export.
- `web/` - React and Vite dashboard. Source in `web/src/`.
- `tests/` - pytest suite.
- `docs/` - design notes (pricing, ingestion, privacy, operations).
- `pricing.json` - model rates and `currency` (`code`, `per_usd`).
- `requirements.txt` (runtime), `requirements-dev.txt` (adds pytest).
- Root `tokentrack.py`, `harness.py`, `dashboard_template.html` - the older single-file tool. Not part of the package.

## Tests
Run from the repo root:

    PYTHONPATH=src <python> -m pytest -q

Any Python with the dev requirements works. For example, a temporary scratch venv: `/tmp/claude-1000/-home-tankaizokuo/99312bc8-8c51-4279-8da2-34d6abe35f56/scratchpad/venv2/bin/python`. That path is temporary; do not rely on it.

## CLI
Run from the repo root with `PYTHONPATH=src python -m token_ledger <command>`.
- `serve --db DB [--port N]` - loopback dashboard API (default port 8765).
- `ingest` - ingest an explicit log file.
- `archive` - import an archive bundle JSON.
- `migrate` - apply schema migrations.
- `backfill-session-ids --db DB` - fill NULL omp session ids from transcript headers. Idempotent.
- `backfill-omp-project-ids --db DB` - fill NULL omp project ids from the transcript header cwd. Idempotent.
- `recompute-overlap-flags --db DB` - re-derive overlap flags. Idempotent. Run after backfill.
- `reports` - scheduled report jobs.

Run `python -m token_ledger --help` for the full list.

## Coverage rule
A request row is complete only when it has fresh input, cache read, and output, and at least one cache-write bucket (5m, 1h, or unknown TTL) is observed. Incomplete rows are not priced as zero. They stay visible as unknown or incomplete.

## Money
Show money in INR (₹). Costs are computed in USD and multiplied by `currency.per_usd` from `pricing.json`. Do not hardcode a rate.

## Databases and the verified copy
- The verified database must never be written. Work only on copies.
- Make a copy with the sqlite backup API. Open the source with `mode=ro`.
- Some CLI paths open with `run_migrations=False`. Migrate the copy first with `open_database(path, run_migrations=True)` from `token_ledger.store.database`, then run the CLIs on it.
- Generated dashboard HTML contains real usage data. It is gitignored. Never commit it.

## Conventions
- Keep the old flag spellings of the legacy tool working if you touch it.
- Commit only when asked.
