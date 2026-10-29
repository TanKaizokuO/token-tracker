# Handoff: Token Ledger improvements

Written 2026-10-15 by Claude Code. Meant for any coding agent picking up work on the Token Ledger dashboard.

## What this is

The **Token Ledger** is a private claude.ai artifact (https://claude.ai/artifact/Y1iiTF4oWh5ANAqhfZR3gY). It shows token usage and API-equivalent cost for two coding agents: **omp** (oh-my-pie) and **Claude Code**. All money is shown in ₹ at 95.56/USD.

The page is `dashboard_template.html` with one JSON payload (`const DATA = {...};`) put in by `tokentrack.py`.

**You cannot publish the artifact.** Only Claude Code can read or publish it, using its Artifact tool. Edit the files here, test locally, then hand back to Claude Code to publish (see "Handing back").

## File map

| Path | What it is |
|---|---|
| `tokentrack.py` | Parses omp and Claude Code session logs, prices every message from `pricing.json`, and outputs the payload. Day grouping is at line ~402 (UTC). Model ID normalisation is at lines 38–50. HTML output is at ~672. |
| `pricing.json` | Per-model USD prices and the INR rate. **Has uncommitted changes** (includes the GPT-6.1 Sol prices). Check `git diff pricing.json` before editing. |
| `dashboard_template.html` | The UI. **Synced to the live page on 2026-10-15**, so editing it is safe. Keep `const DATA = /*__DATA__*/;` exactly as written; `tokentrack.py` substitutes it. |
| `token-ledger.live.html` | A copy of the live artifact as published 2026-10-15 05:21 UTC, with real data. Reference only. Gitignored: **never commit it** (the repo is public). |
| `harness.py` | Optional `--harness` instrumentation. The UI shows a Harness card only when `DATA.harness` exists. |
| `~/.claude/skills/token-ledger-update/` | The Claude Code skill that refreshes the artifact's data (`SKILL.md`, `merge_data.py`). It splices a new `DATA` into the live page and never touches the UI. |
| `~/.claude/skills/llm-cost-audit/scripts/` | `audit.py` (the weekly audit) and `render_audit_tab.py`, which writes the **Audit** tab. |

**Do not use `~/.agents/skills/token-ledger-update/`.** It is a corrupted copy: "Claude Code" has been replaced with "Codex", including in the artifact link. Read the `~/.claude/skills/` copy instead.

### Data shape

`DATA.cells` rows are `[day, source, model, project, n, in, out, cr, cw, cost_usd]`. `source` is `omp` or `claude-code`. `cr`/`cw` are cache read/write tokens. `DATA.sub_cells` uses the same row plus a sub-repo name. The JS recomputes every view from `cells`; the precomputed `totals` and `by_*` keys are not used by the UI. There are no session IDs, timestamps or provider fields yet.

## Hard constraints

- Keep the Audit block markers. `render_audit_tab.py` replaces everything from `    <!-- Audit View -->` up to `\n\n  </div>\n</div>\n<div id="tip"`, and both must appear exactly once. In the template the block is an empty-state placeholder.
- No real usage data, project names or audit text in `dashboard_template.html` (public repo).
- The artifact is one self-contained HTML page:
  - It must stay under 16 MB; it is about 270 KB now.
  - External scripts may come only from cdnjs, jsdelivr/npm, unpkg or code.jquery.com, with exact pinned versions.
  - It must work in both themes and at phone width (16 px side gutter, no horizontal scroll).
- `localStorage` is fine for per-viewer conveniences such as saved filters, wrapped in try/catch. It is **not** fine for anything that must persist. Persistent state needs an artifact database capability that Claude Code sets up; see item 13.
- Money is shown in ₹. Prices stay in USD in `pricing.json`.
- Never use omp's own recorded `cost_*` columns. They are about 100× too high for several models. `tokentrack.py` reprices from `pricing.json`, which is correct.

## Test loop

```sh
./tokentrack.py dashboard all --html /tmp/x/test.dashboard.html --no-open
# then open it, or check for runtime errors headless:
google-chrome --headless=new --disable-gpu --enable-logging=stderr --v=0 \
  --virtual-time-budget=4000 --dump-dom file:///tmp/x/test.dashboard.html 2> chrome.log >/dev/null
grep -i -e Uncaught -e 'CONSOLE.*error' chrome.log
```

A fresh build totals about ₹2,95,225 all-time. The live page shows about ₹2,97,356 because it keeps Sep 12–13 Claude Code rows whose logs have since been deleted (`merge_data.py` restores them).

## Verified state (2026-10-15)

- **The data is accurate.** For 10-12, 10-13 and 10-14, messages, tokens and cost match the source logs exactly for every source and model. The gaps on 08-29 and 09-17 are real (no usage). 10-15 is a partial day.
- **Audit Parity:** `render_audit_tab.py` was aligned with `tokentrack.py` timezone and pricing structures, achieving 0.00% parity across non-archived cells in the audit date range with a warning badge when difference exceeds 1%.
- **Pricing Status:**
  - **Gemini 3.5 Flash:** Inferred rate from Gemini 3.6 Flash Standard; provider documents 3.5 as an alias automatically routed to 3.6 with no distinct 3.5 price entry.
  - **Gemini 3.6 Flash:** Input, output, and cache-read match Standard list through 2026-12-31; cache-write (`cw5m`/`cw1h`) is unknown (provider publishes no cache-write rates; preserved as estimates).
  - **GPT-6.1 Sol:** Input, output, and 5% cache-read verified against provider docs; cache-write rates (`cw5m`/`cw1h` 2.0) are estimated user assumptions and preserved without changing configured numeric rates.
  - **Unpriced models:** Effort-suffixed IDs (e.g. `claude-opus-5-5-low`, `claude-sonnet-5-5-medium`) remain explicitly classified as `unpriced` (never false ₹0).
- **Archived Stored Cost & Coverage Limits:** Restored historical rows retain their stored cost and legacy timezones, with session and hourly logs explicitly marked as unrecheckable. Session detail records are capped newest-first to 8 MiB (`SESSION_DETAIL_BYTES`).
- **Artifact Publication:** Publication remains exclusively the responsibility of Claude Code using its Artifact tool; no hosted artifact has been changed.

## Verification Record

There is deliberately **no permanent test suite** in this repository. Verification has been established empirically across all layers:
1. **CLI Backend Smoke Verification:** Validated `--tz` timezone cut handling, `--events` validation, cache versioning (`parse-v3`), and hourly bucket integrity.
2. **Audit Tab Rendering:** Verified parity on `/tmp/tt-ui/fixture.dashboard.html` with exit code 0 and 0.00% discrepancy.
3. **Browser Automation Proof:** Executed on `/tmp/tt-ui/final.dashboard.html` (97,756 messages, 2.17 MB compressed / 5.27 MB expanded, well under the 16 MB cap):
   - 6 native `<button type="button">` sort headers on `#sessions-table` (Cost, Tokens, Reqs, Duration) tested with zero console errors.
   - View transitions verified across Overview, Models, Costs, Sessions, and Needs Review.
   - Layout responsive pass on 1440px desktop and 390px phone viewports in dark and light modes with zero horizontal overflow (`scrollWidth <= innerWidth`).

## Improvements, in build order (All 17 Features Implemented Locally)

### Phase 1: make the numbers trustworthy

1. **Fix the Audit/Overview mismatch at the source.**
   - *Status: Implemented locally (2026-10-15).* `render_audit_tab.py` re-aggregates from `cells` for the audit's date range and sources, displaying pricing version, timezone, exclusions, and a parity warning badge (>1% discrepancy). Verified at 0.00% error.
2. **Show how much usage is priced.**
   - *Status: Implemented locally (2026-10-15).* Tri-state `priced: true | false | "estimated"` map emitted by `tokentrack.py`. Overview card displays separate priced, estimated, and unpriced counts; unpriced models show as unpriced, never ₹0.
3. **Make freshness obvious.**
   - *Status: Implemented locally (2026-10-15).* Top-bar badge indicates "Data through [date] · refreshed [time] [tz]". Today's bar is tagged as a partial day.
4. **Group days by local timezone, not UTC.**
   - *Status: Implemented locally (2026-10-15).* Added `--tz ZONE` CLI flag defaulting to system local timezone (`local`), supporting arbitrary IANA timezones and falling back safely to UTC.
5. **Remove the hardcoded "0 failed" on Requests tile.**
   - *Status: Implemented locally (2026-10-15).* Real failure counts (malformed lines, unreadable files, missing rates) tracked and displayed.
6. **Mark archived rows.**
   - *Status: Implemented locally (2026-10-15).* Restored rows carry `archived: true` flag, display tooltip notices, and are excluded from live parity checks.
7. **Check unverified prices.**
   - *Status: Implemented locally (2026-10-15).* Audited rates in `pricing.json`: Gemini 3.5 inferred, Gemini 3.6 cw unknown, GPT Sol cr verified / cw estimated; numeric rates preserved.

### Phase 2: make it actionable

8. **Comparison with the previous period.**
   - *Status: Implemented locally (2026-10-15).* Main tiles show change pills (`.delta` for cost, requests, cost/req, cache share) and a "What drove the change" breakdown by project and model.
9. **Cost explanation on click.**
   - *Status: Implemented locally (2026-10-15).* Clicking any cost opens a slide-out drawer detailing token breakdown (input, output, 5m/1h cache writes, cache reads) × unit rates, pricing source/version, and INR conversion.
10. **Models table columns.**
    - *Status: Implemented locally (2026-10-15).* Added effective ₹ per 1M tokens, cache hit rate %, and relative share of total spend.
11. **Monthly budget card.**
    - *Status: Implemented locally (2026-10-15).* Month-to-date spend, projected month-end spend, and configurable ₹ budget target line stored in `localStorage`.
12. **Session explorer & hourly heatmap.**
    - *Status: Implemented locally (2026-10-15).* Detailed sessions table with native `<button type="button">` headers for multi-key sorting, search filters, detail inspection drawer, 8 MiB payload cap (`SESSION_DETAIL_BYTES`), and 24-hour activity heatmap.

### Phase 3: decision support

13. **Turn Audit into a review queue.**
    - *Status: Implemented locally (2026-10-15).* Renamed "waste" to "needs review", flagging expensive un-edited subagents, large context reads, and sleep loops. Triage supports tagging sessions as `research`, `planning`, `implementation`, `abandoned`, or `avoidable`.
    - **Review Store Hook:** Exposes `window.tokenLedgerReviewStore` for durable persistence:
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
      Resolving `save` to `true` signifies durable persistence. Defaults to `localStorage` per viewer until Claude Code provisions an artifact database.
14. **Savings simulator.**
    - *Status: Implemented locally (2026-10-15).* In-browser simulator computing projected ₹ savings from subagent model migrations and context reductions client-side.
15. **Track whether changes worked.**
    - *Status: Implemented locally (2026-10-15).* Loads `events.json` via `--events` and draws vertical timeline markers on daily charts.
16. **Saved views.**
    - *Status: Implemented locally (2026-10-15).* Allows saving and restoring range/filter combinations in `localStorage`.
17. **Refined labels.**
    - *Status: Implemented locally (2026-10-15).* Headline cost labeled "Estimated API value", unpriced tokens kept distinct from ₹0.

### Small fixes

- Renamed header to "Token Ledger".
- Filter inputs populated when top model/project links are clicked.

The landing page answers: **How much usage? What changed? What needs review? How complete is the data?**

## Handing back

When handing back to Claude Code:
1. All 17 roadmap features are implemented and tested locally.
2. Production code and template maintain strict constraints: single `/*__DATA__*/` slot, exact Audit markers (`    <!-- Audit View -->` and `\n\n  </div>\n</div>\n<div id="tip"`), no embedded private paths or user data.
3. Publication still requires Claude Code: run `merge_data.py` to splice the new payload, run `render_audit_tab.py`, and republish via Claude Code's Artifact tool.
