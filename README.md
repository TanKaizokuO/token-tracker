# token-tracker

Know what your coding agents actually cost you.

`token-tracker` reads the session logs that **Claude Code** and **oh-my-pie
(`omp`)** already write, and turns them into a spend report — by tool, by model,
by project, by day. One CLI, one static dashboard, no daemon and no indexing
step: the logs *are* the database, so every run is current.

It was built because `omp`'s own cost numbers are wrong (see
[Costs](#costs)), and because neither tool will tell you what the *other* one
spent.

## Install

Needs Python 3.9+ and nothing else — no dependencies.

```sh
git clone https://github.com/TanKaizokuO/token-tracker
cd token-tracker
./tokentrack.py
```

Put it on your `PATH` if you want it everywhere:

```sh
ln -s "$PWD/tokentrack.py" ~/.local/bin/tokens
```

## Usage

```sh
tokens                      # summary: by tool, model, project
tokens week                 # last 7 days   (also: month, all, or a number of days)
tokens daily                # add a per-day table
tokens omp week             # one tool only (omp or claude)
tokens dashboard            # build the dashboard and open it in your browser
tokens dashboard month      # ...opened on the last-30-days view
tokens json | tokens raw    # aggregates / every priced message, for other tooling
tokens clear-cache          # delete the parse cache
```

Words can go in any order. Key options:
- `--tz ZONE`: cut days, hours, and periods in this IANA zone (e.g. `--tz Asia/Kolkata`); defaults to system local (`"local"`).
- `--events PATH`: JSON array of dated markers `{"date": "YYYY-MM-DD", "label": "..."}` for daily charts (default: `events.json` beside `tokentrack.py`).
- `--usd`: display costs in USD instead of the configured currency.
- `--harness`: instrument the collection run and report cache, timing, and resource metrics.
- `--top N`: number of projects to show in summary tables (default: 10).
- `--no-open`: write dashboard HTML without launching a browser.

Older flag spellings (`--period`, `--days`, `--html PATH`, `--source`, `--rate`, `--no-cache`, etc.) remain supported.
Output (numbers illustrative):

```
Token usage  2026-08-13 -> 2026-09-12  (31 active days, 25117 messages)
  billable in           91.8M
  output                16.5M
  cache read             2.2B
  cache write           43.8M
  total                  2.3B
  cost              $1,128.06   (~$36.39/active day)

By tool
                 msgs    input   output   cache r  cache w    cost (USD)
  omp           23199    91.8M    15.4M      2.0B    38.6M     $1,022.04
  claude-code    1918     8.3K     1.1M    163.0M     5.2M       $106.02
```

## Dashboard

`tokens dashboard` renders `~/.cache/token-tracker/dashboard.html` from
`dashboard_template.html` with fresh data baked in and opens it in your browser.
It is a single self-contained file, so you can also copy it anywhere; use
`--html PATH` to write it somewhere else.

The page has a **Last 7 days / Last 30 days / This month / All time / Custom**
toggle; Custom shows a from/to date picker for any range. Every section —
stat tiles, stacked daily bars, model and project breakdowns, the detail table —
re-aggregates client-side from a compact `(day, tool, model, project)` fact table
embedded in the page, so switching range is instant and needs no regeneration.
Ranges anchor on the last day with data rather than on today, so a saved copy
reads the same whenever it is opened.

### Timezones and Events

By default, daily buckets, hours, and reporting periods are aligned to the system's local timezone (`--tz local`), or any specified IANA timezone (`--tz Asia/Kolkata`, `--tz Europe/London`).

Milestones and config changes can be plotted on daily charts using `--events PATH` (or `events.json` in the repo root). The file must be a JSON array of objects with strict keys:
```json
[
  {"date": "2026-10-10", "label": "Claude Code -> opusplan"},
  {"date": "2026-10-14", "label": "omp routing: Sol default"}
]
```
Invalid schemas or malformed dates produce a clear CLI validation error before processing.

### Session and Hourly Data Limits

The dashboard payload includes detailed `sessions` (with timestamps, request counts, token breakdown, and tool usage) and an hourly heatmap (`hours`).
- **Generation speed:** `speed` rows are `[day, source, model, n, out, secs]`, omp requests only (Claude Code is not timed); tok/s = `sum(out) / sum(secs)`. Uses `duration - ttft` (ms) and drops requests over 600 s.
- **Payload capping:** To keep dashboard HTML self-contained and strictly under 16 MB, session records are capped newest-first to an 8 MB JSON budget (`SESSION_DETAIL_BYTES`). The payload includes `sessions_coverage` (`{"complete": bool, "included": N, "omitted": M}`) so truncation is explicit.
- **Archived rows & legacy timezones:** When historical logs are pruned or deleted, older rows restored from prior artifacts are flagged with `archived: true` so they can be distinguished from re-verifiable disk logs. Archived rows retain their stored costs and legacy timezones, and their hourly/session log details are marked unrecheckable.

### Interactive UI Features

The dashboard implements a complete 17-feature suite for spend tracking and decision support:

1. **Overview Comparisons:** Headline metric cards display comparison pills (change in cost, requests, cost/request, cache share) against the prior equivalent time window, accompanied by a "What drove the change" breakdown of top shifting projects and models.
2. **Cost Explanation Drawer:** Clicking any cost metric opens an inspection drawer showing token volumes (input, output, 5m/1h cache writes, cache reads) multiplied by unit rates, pricing version provenance, exclusions, and INR conversion at the configured rate.
3. **Model Performance Metrics:** The Models view computes effective cost per 1M tokens, cache-hit percentage, and relative share of total spend for each model.
4. **Monthly Budget Tracking:** Displays month-to-date spending against projected month-end totals and a customizable budget threshold line, saved locally per viewer.
5. **Session Explorer:** An interactive session table featuring native accessible `<button type="button">` column headers for multi-key sorting (Cost, Tokens, Requests, Duration), faceted search filters, and an in-depth slide-out detail drawer.
6. **Hourly Heatmap:** Visualizes 24-hour activity distribution in the configured timezone to identify peak development and agent workload hours.
7. **Review Queue ("Needs Review"):** Classifies high-cost and anomalous sessions (e.g. read-only subagents on premium models, large context reads, sleep-poll loops). Sessions can be triaged and tagged as `research`, `planning`, `implementation`, `abandoned`, or `avoidable`.
8. **Review Store Integration Hook:** The review queue exposes a standard interface for durable team persistence:
   ```typescript
   window.tokenLedgerReviewStore = {
     async load(): Promise<Record<sessionId, {
       label: 'research' | 'planning' | 'implementation' | 'abandoned' | 'avoidable',
       timestamp?: string,
       note?: string
     }>>,
     async save(reviews: Record<sessionId, {
       label: string,
       timestamp?: string,
       note?: string
     }>): Promise<boolean>
   };
   ```
   A `save` promise resolving to `true` denotes confirmed durable persistence. In the absence of an artifact database provider, reviews fall back to `localStorage` per viewer.
9. **Savings Simulator:** Interactive what-if scenario calculator estimating ₹ savings from migrating subagent workloads to more economical models or reducing context windows, computed entirely client-side against the embedded rate table.
10. **Timeline Events:** Displays dated deployment and configuration milestones loaded via `--events` as vertical markers on daily spend charts.
11. **Saved Views:** Bookmarks custom combinations of date range, source filters, and search terms in `localStorage`.

`dashboard.html` is gitignored: it contains your real spend and project names.
## Harness

`tokens --harness` instruments the collection run and reports what it actually
did. Run `tokens dashboard --harness` to put the same metrics on the dashboard.

```
Harness  33,378 messages from 883 files in 0.12s

Cache performance (parse cache)
  hit rate             99.7%   880 of 883 accesses
  miss rate             0.3%   3 reparsed from source
  latency                67us  mean hit, p95 179us, miss 106us
  evictions                0   LRU, over a 64.0 MiB cap
  utilization          12.8%   8.2 MiB across 897 entries

Execution performance
  total                0.12s   collect 0.12s, evict 0.002s
  per file          mean 0.10ms  median 0.04ms, p95 0.19ms, p99 0.66ms
  throughput         278,543   messages/s  (35.7 MiB/s)
  cpu                   100%   0.12s of 0.12s wall
  peak memory            55 MiB
```

It also reports disk I/O, context switches, thread count, GC activity, and the
failures the run hit — malformed JSONL lines, unreadable files, and messages
whose model has no rate.

**What it deliberately does not report.** Database queries, API calls, network
requests and data transferred, and lock contention are absent, not zero. This
tool has no database, makes no network calls, and is single-threaded, so those
numbers would be decoration rather than measurement. Test coverage is likewise
absent: there is no test suite yet, and a coverage figure computed against no
tests is worse than none.

### The parse cache

`--harness` exists partly because there is now a cache worth measuring. Parsed
session files are memoized under `~/.cache/token-tracker/parse-v3/`, keyed on
path + mtime + size. The cache directory is versioned (`parse-v3`) so parser and
record schema changes invalidate safely without cache corruption. Old
transcripts never change, so they are parsed once ever; an active session that
grew since the last run misses and is re-read.

On ~880 files it takes a cold run from **1.24s to 0.10s**. The cache is
correctness-neutral — `--no-cache` produces byte-identical aggregates — and
bounded at 64 MiB, evicting least-recently-used entries. `TOKENTRACK_CACHE`
environment variable overrides the cache directory.
## How it works

Both tools append one JSON object per line to a session file, and assistant
messages carry a `usage` block. token-tracker globs those files, keeps the
rows that carry usage, dedupes, prices each one, and aggregates.

| Tool | Path | Dedupe key |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | `(requestId, message.id)` |
| omp | `~/.omp/agent/sessions/**/*.jsonl` | `message.responseId` |

Deduping is not optional. Claude Code writes up to **six rows per API request**
as a response streams, each carrying the same cumulative `usage`. Counting them
naively roughly doubles the apparent bill.

Four token classes are tracked separately, because they bill at different
rates: input, output, cache **write** (5-minute and 1-hour TTL, at 1.25x and 2x
the input rate), and cache **read** (0.1x input). Cache reads typically dominate
token counts while contributing little to cost, which is why a headline "total
tokens" number is close to meaningless on its own — the dashboard says so
explicitly rather than letting the big number mislead.

Model ids are normalized before pricing: date suffixes are stripped
(`claude-haiku-4-5-20251001` → `claude-haiku-4-5`) and effort suffixes (`-low`, `-medium`, `-high`, `-xhigh`, `-max`) on any model
with a priced base id are priced as the base model (`gemini-3.8-flash-high` →
`gemini-3.8-flash`), since effort changes how many tokens you spend, not the
rate. The original id is kept in the tables.

Project names come from each session's recorded `cwd`, falling back to the
encoded directory name.

| File | Role |
|---|---|
| `tokentrack.py` | collector, pricing, CLI |
| `harness.py` | `--harness` instrumentation |
| `pricing.json` | rate table and display currency |
| `dashboard_template.html` | dashboard, with a `/*__DATA__*/` slot |

## Costs

Cost is **always recomputed from raw token counts** using `pricing.json`.
Nothing in either tool's logs is trusted as a cost figure.

That is deliberate. `omp` records its own costs, and they are inconsistent —
some rows are inflated 100x (174 output tokens billed at $0.435, when Opus 5
output is $25/MTok, so it should be $0.00435) while adjacent rows in the same
table are correct. `~/.omp/stats.db` carries the same bad values *and* is a
stale index that silently stopped updating. Both are ignored.

Rates in `pricing.json` are USD per 1M tokens:

```json
"claude-opus-5":  {"in": 5.00, "out": 25.00, "cw5m": 6.25, "cw1h": 10.00, "cr": 0.50}
```

Display currency is converted from USD via the `currency` block:

```json
"currency": {"code": "INR", "symbol": "₹", "per_usd": 95.14}
```

Set `per_usd` to `1` and `code` to `USD` for dollars, or use `--usd` / `--rate`
per run.

**Accuracy and completeness.** Anthropic rates are list pricing. Gemini and OpenAI models have specific verification statuses:
- **Gemini 3.5 Flash:** Inferred rates based on Gemini 3.6 Flash Standard pricing; Google's model directory documents 3.5 as an alias automatically routed to 3.6, with no distinct 3.5 pricing entry.
- **Gemini 3.6 Flash:** Input, output, and cache read rates match the Google Standard list through the 2026-12-31 introductory period; cache-write (`cw5m`/`cw1h`) rates are unknown from provider documentation and preserved as configured estimates.
- **GPT-6.1 Sol:** Input, output, and 5% cache read rates are verified against provider documentation; cache-write rates (`cw5m`/`cw1h`) are estimated user assumptions and preserved without changing configured numeric rates.

Pricing completeness uses a tri-state classification:
- `True`: model rate is verified.
- `"estimated"`: model rate is configured/provisional (e.g. Gemini 3.5/3.6 Flash, GPT-6.1 Sol cache rates).
- `False`: unpriced model (e.g. unconfigured effort variants).

Unpriced models are never counted as $0 or ₹0 in coverage calculations; the coverage block explicitly separates `priced`, `estimated`, and `unpriced` request counts and costs. The pricing table includes a cryptographic version digest (`sha256:...`) over rates and sources to track pricing schema provenance.

## Verification & Artifact Constraints

There is deliberately **no permanent test suite** in this repository. Quality and regression safety are established through empirical verification:

1. **CLI & Parsing Invariants:** Tested across `--tz` timezone cut boundaries, `--events` schema validation, cache versioning (`parse-v5`), and hour distribution completeness.
2. **Audit Parity:** Validated via `render_audit_tab.py`, achieving 0.00% discrepancy between audit figures and non-archived fact cells over the audit date range.
3. **Browser Automation:** Headless Chromium validation over the full production build (2.17 MB compressed / 5.27 MB expanded, well within the 16 MB artifact ceiling):
   - 6 native accessible `<button type="button">` sort headers operating with 0 console errors.
   - Complete client-side view routing (Overview, Models, Costs, Sessions, Needs Review).
   - Responsive layout verified at 1440px desktop and 390px phone viewports with zero horizontal overflow (`scrollWidth <= innerWidth`).

**Artifact Publication:** The production dashboard is hosted as a private Claude Code artifact. Publication remains strictly through Claude Code using its artifact tools; local builds do not alter hosted artifact instances.

## License

MIT
