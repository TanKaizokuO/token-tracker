# token-tracker
Single-file stdlib-only Python CLI (3.9+, no deps) that reads Claude Code and oh-my-pie (`omp`) session logs and reports spend. No daemon/index; logs are the DB.

## Map
- `tokentrack.py` (~660 lines): everything - collectors (`collect_claude_code`, `collect_omp`), pricing (`price`, `normalize_model`), report/table, dashboard, argparse in `main()`.
- `harness.py`: `--harness` instrumentation (cache/timing/resource metrics); `run()` + `render()`.
- `pricing.json`: rates/currency. `pricing.local.json` = gitignored local override.
- `dashboard_template.html`: template for `tokens dashboard`.

## Run
- `./tokentrack.py [week|month|all|N] [daily] [omp|claude] [dashboard|json|raw|clear-cache]` (words in any order); opt flags `--usd --harness --top N --no-open`.
- README suggests `ln -s $PWD/tokentrack.py ~/.local/bin/tokens`.
- Tests: none found. Lint/build: none found.

## Gotchas
- Parse cache at `~/.cache/token-tracker/parse-v2` (override `TOKENTRACK_CACHE`); bump the `parse-vN` suffix when parsed fields change.
- omp's own cost numbers are wrong (the reason this exists); costs come from `pricing.json`, not from logs.
- Generated `dashboard.html` / `*.dashboard.html` contain real usage data; gitignored, never commit.
- Old flag spellings (`--period`, `--html`, `--source`...) are hidden but must keep working.
