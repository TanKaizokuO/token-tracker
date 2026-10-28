# Provider & Access-Route Analytics, Quotas, Burn-Rate, and Budgets (WP16)

This specification defines the semantic analytics, data structures, and operational guarantees for **Providers & Limits** in Token Ledger.

## 1. Executive Summary & Design Principles

Token Ledger strictly distinguishes between:
1. **Provider & Route Identity vs. Model Identity:** A single model (e.g. `claude-3-5-sonnet`) can be invoked across distinct access routes (`direct_api`, `bedrock_cross_region`, `openrouter`). Route identity is never collapsed into model identity.
2. **Financial Measure Separation:**
   - **Actual Billed Spend:** Direct verified financial fact from invoices/billing records.
   - **API-Equivalent Valuation:** Standard catalog rate calculation for comparison across providers.
   - **Subscription Fees:** Flat platform fees distinct from per-token usage.
   - **Cache Savings:** Estimated financial delta from prompt caching.
   - **Unpriced Usage:** Requests lacking verified rate cards are tracked explicitly, never coerced to $0.00.
   - **Historical FX:** Exact effective date and rate are recorded and reported; no silent retroactive recalculations.
3. **Official Quotas vs. Local Inferences:**
   - Authoritative quota metrics (`subscription_quotas`) require official provider snapshot headers or API records.
   - When quota data is missing or unverified, the system explicitly reports `Quota unavailable` (`is_available = false`, `percent_used = null`).
   - Local token counters are **never** used to infer remaining quota percentages.
4. **Token Burn-Rate & Live Sessions:**
   - Burn rate requires a declared interval/denominator (e.g., 15 minutes) and explicitly flags partial current buckets.
   - Baseline period comparison abstains (`typical_burn_rate = null`) when historical baseline samples are sparse, never reporting an artificial 0.0% change.
   - Live session tracking cleanly separates `last_observed_time` from heuristic active evaluation (`is_heuristic_active`), preventing unsupported precision.
5. **Usage Budgets & Local Safeguards:**
   - Supports daily tokens, monthly cost, project, model, and session thresholds.
   - Respects known-eligible pricing coverage.
   - Alerts and notifications are strictly local events without external network telemetry.

---

## 2. Metric Families & Engine Interface

Dispatched via `analyze(conn, envelope)`:

### A. `family: "providers"` (`metric_id: "providers_breakdown"`)
- Returns rows grouped by `(provider, account_alias, access_route)`.
- Aggregates disjoint token counters (`fresh_input`, `cache_read`, `cache_write_5m`, `cache_write_1h`, `cache_write_unknown_ttl`, `output`, `reasoning`).
- Computes `financial_summary` with explicit `fx_date`, `fx_rate_usd_to_inr`, and unpriced request counts.
- Attaches verified `quotas` cards for inspected providers.

### B. `family: "burn"` (`metric_id: "token_burn_rate"`)
- `declared_interval_minutes`: declared measurement window.
- `current_burn_rate_tpm`: tokens per minute across window.
- `current_burn_rate_cost_per_hour`: estimated hourly cost velocity.
- `typical_burn_rate_tpm`: baseline equivalent-period burn rate (or `null` if sparse).
- `high_burn_sessions`: ranked active sessions by consumption velocity.

### C. `family: "live"` (`metric_id: "live_sessions"`)
- Rollup of all sessions active within `heuristic_window_minutes`.
- `last_observed_time`: exact ISO UTC timestamp of latest activity.
- `is_heuristic_active`: boolean based on explicit heuristic cutoff.
- `tokens_so_far`: accumulated disjoint tokens.
- `status`: `'active'` | `'idle'`.

### D. `family: "budgets"` (`metric_id: "usage_budgets"`)
- Evaluates configured user thresholds from `alerts_and_reports`.
- Tracks `percent_consumed`, `status` (`'ok'` | `'warning'` | `'breached'`).
- Generates `local_notifications` for breached/warning budgets when `notifications_enabled = true`.

---

## 3. Quota Snapshot Ingestion

Official snapshots are ingested into `subscription_quotas` via `import_quota_snapshot(conn, snapshot)`.
Snapshot schema:
```python
{
    "provider": str,              # required
    "account_alias": str,         # defaults to 'default'
    "access_route": str,          # defaults to 'default'
    "official_source": str,       # required (e.g. 'anthropic-ratelimit-headers')
    "window_type": str,           # '5m' | 'minute' | 'hour' | 'day' | 'month' | 'sliding_window'
    "limit_value": Optional[float],
    "used_value": Optional[float],
    "remaining_value": Optional[float],
    "reset_at": Optional[str],    # ISO UTC
    "units": str,                 # 'tokens' | 'requests'
    "status": str,                # 'available' | 'unavailable' | 'unsupported' | 'stale'
    "percent_used": Optional[float],
}
```

Quota expiry samples current UTC wall-clock time once per analysis, independently of request timestamps, even when selected requests are historical or the filtered request set is empty. If `reset_at` is at or before that instant (including equality), `analyze` marks the quota card as `status = 'stale'`, `is_available = false`, and `percent_used = null`; selecting older requests never extends quota availability.

Quota cards are keyed by provider, account, route **and window type**. Each card
includes all selected snapshots in ascending measurement-time order (ID breaks
ties), retaining source, raw units/counters, reported status/percentage and reset
time. The newest snapshot alone controls current availability. History preserves
the reported percentage separately from current expiry; percentages are never
calculated from counters. The UI shows official counters even when an official
percentage was not supplied.

Recent burn uses non-overlapping five-minute buckets over at least thirty
minutes, ending at the latest selected local request. Every point declares its
five-minute denominator, request/priced counts, and partial/active state; absent
observations/counters or prices remain unknown, not a fabricated zero. All
buckets reflect partial local collection, not guaranteed upstream completeness.
The chart and expandable evidence table expose the same points.

Live sessions are queried independently of burn. Estimated value is explicitly
USD; partial pricing is labeled, wholly unpriced sessions are unavailable.
Last sync is the latest successful persisted source checkpoint for that harness,
not query-generation time; no checkpoint means unavailable. Activity status
remains a heuristic relative to the latest selected local request, not proof
that a process is still running.

Budget scope entry distinguishes project IDs from model identities and preserves
that key across save/reload/delete. Session thresholds remain absolute
guardrails: API and UI explicitly abstain from comparable-task comparisons
(`no_baseline`) because matched task labels and adequate samples have not been
established.

## Historical subscription payments

Providers & Limits allows manual creation and editing of actual historical
payments with provider, account alias, access route, payment date, amount,
currency, and source. The form and `GET/POST /api/v1/subscription-fees` and
`PATCH /api/v1/subscription-fees/{fee_id}` use an independent local table;
writes require the same origin and CSRF protection as other mutations. GET
accepts `limit` (1–200, default 50) and `offset` (default 0), and returns
`total`, `limit`, and `offset` with payments ordered by date and ID. A recorded
zero is distinct from no recorded payment. No conversion is inferred.

This independent, non-additive list is all-history and does not apply any
dashboard filters, including date, provider, account, route, task or project.
These account-level facts are not allocated to requests or incorporated in
the existing request-linked fee summary, subscription filter, or shared query
totals. In particular, saving a fee never changes actual billed request facts
or API-equivalent valuations. Existing per-request subscription fee values
remain visible separately as request-linked fees.
