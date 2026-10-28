# Token Ledger Query Contract (WP08)

This document specifies the query engine public contract, request validation rules, metric computation semantics, and archive reconciliation behavior for `token_ledger.query` and `token_ledger.metrics`.

---

## 1. Architectural Scope & Principles

1. **Centralized Query Semantics:** All downstream dashboards, API endpoints, CLI reports, and comparison views must query canonical SQLite storage through `token_ledger.query.execute_query`. Hand-rolled SQL queries with divergent aggregation logic are strictly prohibited.
2. **Explicit Contract Versioning:** Every query envelope must provide `contract_version: "1"`. Unknown versions, unknown top-level envelope fields, and unsupported filter keys are rejected immediately with descriptive `ValueError`s.
3. **Half-Open Instant Boundaries:** All time ranges specify half-open intervals $[ \text{start}, \text{end} )$. Start timestamps are inclusive; end timestamps are exclusive. Timestamps are parsed with explicit IANA timezone support and evaluated against canonical ISO-8601 UTC instants.
4. **DST Transition Awareness:** Ambiguous or non-existent local clock times during Daylight Saving Time (DST) spring-forward or fall-back transitions are resolved via standard library `zoneinfo`, flagged in query metadata (`dst_transition: true`), and mapped to deterministic UTC boundaries.
5. **No Silent Sums across Unresolved Overlaps:** In `combined` harness scope, if any canonical request has `is_unresolved_overlap = 1` (or archive partitions exhibit unresolved overlaps), `total_tokens_status` is marked `"partial"`, `has_unresolved_overlap` is set to `true`, and an explicit warning is returned. The engine never emits a silently fabricated "exact" sum.
6. **Mutually Exclusive Archive Aggregation:** Archive partitions are combined with live observations only at supported aggregate grains (`daily`, `weekly`, `monthly`, `aggregate`). Superseded partitions (`is_live_replaced = 1`) are strictly excluded. Fine-grained views (`requests`, `sessions`, `events`) query live data only and emit warnings if archive history exists in the queried window.
7. **Disjoint Billable Tokens:** Normalized usage preserves 6 disjoint billable counters (`fresh_input_tokens`, `cache_read_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens`, `cache_write_unknown_ttl_tokens`, `output_tokens`). Reasoning tokens are tracked as a non-additive reported subset of `output_tokens`. Null values represent unobserved counters; measured zero remains `0`. Aggregates retain available counter sums and expose `"exact"`, `"partial"`, or `"unavailable"` status; an all-null request or group yields a `None` total. Outcome analytics require all six known components to derive a request total, preserve explicit zero totals, and propagate unknown totals through tasks, overall totals, dimensional slices, completion-status totals, and token-per-completion metrics instead of reporting partial sums as exact. Sample adequacy does not override counter incompleteness.
8. **Separated Timed Eligibility:** Generation speed (TPS) and time-to-first-token (TTFT) metrics strictly separate eligible timed requests (`timing_basis != 'none'`) from untimed requests. Untimed requests are excluded from denominators and never assigned a synthetic `0.0` TPS.
9. **Financial Facts Separation:** Actual billed spend, API-equivalent USD/INR value, subscription fees, and cache savings are independent facts from `financial_facts`. Null-only aggregates remain `None`, measured zero remains `$0.00`, and partial sums retain their observed values with partial coverage. API-equivalent INR is never inferred by converting API-equivalent USD; missing fees are not treated as zero.
10. **Hypothetical Cache Simulation:** Break-even projections require all four explicitly supplied hypothetical rate assumptions and are not authoritative observed costs. Baseline simulation is omitted unless the source counter set is complete; missing counters are never substituted with zero.
11. **Speed evidence and anomaly baseline:** Speed uses eligible measured generation intervals with known output counters; a measured zero is included, while unknown output or timing is excluded. Measured timing and log-span proxies are separate. TTFT, generation, and total-duration samples have independent denominators; the stored first-output observation does not establish a specific first-chunk, transport, or last-token instrumentation basis.
12. **Independent historical comparisons:** With a selected start instant, speed anomalies use the preceding 30 elapsed days, exclusive of the selected period, with the same query filters and harness scope. Baselines are grouped by model, provider, account, route, origin harness, timing basis, and recorded task label. Each metric requires at least five eligible historical samples; otherwise it abstains. The nearest-rank historical median, window, sample count, current request/session, observed value, and threshold accompany each flag. TPS flags require at least 20 output tokens, baseline above 5 TPS, and observed TPS below half baseline; TTFT flags require observed latency above both 2.5 times baseline and 1000 ms. Without a range start or database, no historical baseline is inferred.
13. **Speed exploration:** The selected hour/day/week/month grain uses the query timezone; missing intervals are represented as gaps, not zero speeds. Scatter points independently retain TPS and TTFT samples, output tokens, API-equivalent cost when known, and fresh-input-plus-cache-read volume only when both counters are available. This input-volume proxy is not context occupancy; request records currently emit no measured occupancy. Task-by-model comparisons expose eligible and total counts, inferred labels and nearest-rank tails, with separate five-sample gates for generation and TTFT. Sparse metrics abstain; models are not ranked and route, provider, harness and workload differences remain caveats.

---

## 2. Shared Engine Interface

The query engine exports two primary functions from `token_ledger.query`:

```python
def validate_query(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Validates and normalizes an incoming query envelope.
    
    Raises:
        ValueError: If contract_version != '1', unknown fields or filter keys exist,
                    start >= end, or pagination/sort parameters are invalid.
    Returns:
        Dict[str, Any]: Canonical normalized query dictionary.
    """

def execute_query(conn: sqlite3.Connection, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Executes a query envelope against the SQLite storage connection.
    
    Validates the envelope and executes optimized, indexed SQL queries to produce
    the standardized query response dictionary.
    """
```

---

## 3. Query Envelope Specification

### Request Schema

```json
{
  "contract_version": "1",
  "harness_scope": "combined",
  "time_range": {
    "start": "2026-10-01T00:00:00Z",
    "end": "2026-11-01T00:00:00Z",
    "timezone": "America/New_York"
  },
  "filters": {
    "model": ["claude-3-5-sonnet"],
    "provider": ["anthropic"],
    "project": ["project-alpha", "unattributed"],
    "repository": ["token-ledger"],
    "session": ["sess-1234"],
    "route": ["direct"],
    "task": ["refactoring"],
    "application": ["claude-code"],
    "subscription": ["recorded"],
    "duration": ["30m_plus"]
  },
  "metric_id": "summary",
  "group_by": ["day"],
  "sort": "total_tokens:desc",
  "pagination": {
    "limit": 100,
    "offset": 0
  },
  "search": "test_failure",
  "include_archive": true
}
```

### Parameter Details

| Field | Type | Required | Description |
|---|---|---|---|
| `contract_version` | String | Yes | Must be `"1"`. Rejects any other version. |
| `harness_scope` | String | No | Scope filter: `"combined"` (default), `"claude-code"`, or `"omp"`. |
| `time_range` | Object | No | Half-open boundary `[start, end)`. Timestamps ISO-8601 or `YYYY-MM-DD`. `timezone` is IANA name (default `"UTC"`). |
| `filters` | Object | No | Global filter dictionary. Keys allowed: `model`, `provider`, `project`, `repository`, `session`, `route`, `task`, `application`, `subscription`, `duration`. Values can be scalar or list. |
| `metric_id` | String | No | Target metric family or registered metric identifier (default `"summary"`). |
| `group_by` | List[str] | No | Grouping dimensions allowed by target family (e.g. `["day"]`, `["model"]`). |
| `sort` | String / Object | No | Sorting criteria, e.g. `"total_tokens:desc"` or `{"field": "total_tokens", "order": "desc"}`. Checked against per-family allowlists. |
| `pagination` | Object | No | Pagination window: `limit` (default 100, max 5000), `offset` (default 0). |
| `search` | String | No | Parameterized substring search matching sessions, request IDs, models, or payloads. |
| `include_archive` | Boolean | No | Whether to aggregate non-replaced archive partitions (default `true`). |

Filter semantics and source availability:

| Filter | Source and meaning |
|---|---|
| `model` | Matches `canonical_requests.normalized_model` or `raw_model`. |
| `provider` | Matches `canonical_requests.provider`. |
| `project` | Matches project ID, name, or alias through `projects`; the reserved value `unattributed` matches `canonical_requests.project_id IS NULL`. |
| `repository` | Matches `projects.project_alias` exactly. There is no separate repository field in the current schema. |
| `session` | Matches `canonical_requests.session_id`. |
| `route` | Matches `canonical_requests.access_route`. |
| `task` | Matches task labels in `manual_settings_and_outcomes` linked to the request or its session. |
| `application` | Matches `canonical_requests.origin_harness`; this is distinct from the broader `harness_scope` selector. |
| `subscription` | `recorded` means a linked `financial_facts.subscription_fee_usd IS NOT NULL`; `unrecorded` means no such non-NULL fact. It does not infer subscription status from provider or quota data. |
| `duration` | Uses linked `sessions.duration_ms`: `under_1m` (< 60,000 ms), `1m_to_5m` ([60,000, 300,000)), `5m_to_30m` ([300,000, 1,800,000)), `30m_plus` (>= 1,800,000), or `unknown` (no session with a non-NULL duration). |

Request-backed metric families use the shared request filter scope. For `traces`, request-linked `events`, `values.total_events`, tool/subagent/MCP/skill/hook/compaction aggregates, event-kind counts, retry and repetition analyses always use the exact filtered canonical request IDs. `group_by: ["session"]` changes grouping, never eligibility; event eligibility follows request identity rather than independent event timestamps. `session_level_events` separately contains only events with NULL canonical ID from sessions linked to selected requests, labeled `session_level_event_scope: "linked_sessions_unfiltered_by_request_or_time"`. These standalone events may lie outside the selected period, do not inherit model/route/request attribution, and are excluded from request-scoped metrics with a warning even under explicit session grouping. With no selected requests, both event collections are empty. `session_level_analysis` exposes their tools, compaction, subagent/MCP/skills/hooks and `counts_by_kind` breakdowns without duplicating the event list; `session_level_analysis.compaction` inherits `session_level_event_scope`, not request `event_scope`. Top-level `source_coverage` describes request-scoped analysis.

The `coverage` family is global partition metadata and cannot be filtered by request dimensions, search, harness scope, or time range; it returns a warning and leaves that metadata unfiltered. Summary archive partitions are aggregate-only: when request filters, search, or non-combined harness scope are active, archive totals are omitted with a warning rather than mixed with an unscoped aggregate. Provider quota snapshots are filtered by their actual `provider`, `access_route`, and `measured_at` fields; request filters other than provider/route, search, and non-combined harness scope cannot scope those snapshots, so quota cards are omitted with a warning. Quota expiry uses current UTC, not request timestamps, including historical or empty request selections; a reset at or before the sampled current instant makes the quota stale, unavailable, and its percentage null.

Model-distribution rows retain `model` (normalized ID), `raw_model`, provider,
route, and pricing version as separate scopes. Their `display_name` and
`canonical_model_id` use the version-scoped mapping at the request date (or its
assigned version), not a guessed model identity. Rows may therefore share a
normalized model ID when their raw IDs, routes, providers, or versions differ.
Request and model-profile displays use the same version-scoped mapping. Filtering
continues to match stored raw or normalized IDs rather than display names.

Timeline queries accept a grain (`hour`, `day`, `week`, or `month`) and optional
`model` grouping. Bucket boundaries are UTC instants clipped to the selected
half-open range; hourly bucket identities include their local UTC offset, so
the two repeated fall-back hours remain distinct. Daily boundaries follow local
calendar midnights, including 23- and 25-hour days. Missing buckets are not
confirmed zero. Calendar tiles use daily request buckets independently of the
timeline's selected grain; aggregate-only archive history is not represented
as request-level daily activity.

`active_minutes` is the union of explicit `events.event_kind = 'request'`
intervals linked to filtered canonical requests, clipped to the bucket and
selected range. An interval requires an offset-aware `started_at` and either
an offset-aware `ended_at` or nonnegative duration; invalid and zero-length
intervals are excluded. Intervals are included even when their request
observation falls outside the range. `active_minutes_basis` identifies this
evidence, `active_minutes_timed_requests` counts contributing request IDs,
and `active_minutes_status` describes request-timing coverage; an interval-only
bucket is partial. Missing intervals yield null, not zero. Request observation
timestamps plus duration do not establish endpoints, and elapsed session spans
or unlocated session active durations are not treated as active time. Current
adapters may not produce explicit request interval events; in that case the
metric remains unavailable.

The weekday/hour heatmap sums observed offset-distinct hourly buckets over the
selected range (including both repeated DST hours) and labels partial coverage.
Distinct sessions cannot be added across hours, so that heatmap mode is
unavailable. Missing hourly buckets are unknown, not zero.

Model rows include `proportions` for the ranking metrics, calculated across all
filtered model/route/version rows before paging. Shares are unavailable if any
contributing value is missing or partial, or the denominator is zero. Session
shares count model-session associations, so a session using multiple models can
contribute to multiple rows. Summary `session_duration_ms` is the mean of
recorded session durations, not an active-time union; missingness reports
`session_duration_observed_count` and `session_duration_sessions_count`.

---

## 4. Query Response Specification

```json
{
  "contract_version": "1",
  "query": { "...normalized query envelope..." },
  "metric_id": "summary",
  "family": "summary",
  "values": {
    "total_tokens": 125430,
    "total_tokens_status": "exact",
    "has_unresolved_overlap": false,
    "live_disjoint_tokens": 75430,
    "archive_tokens": 50000,
    "fresh_input_tokens": 40000,
    "cache_read_tokens": 20000,
    "cache_write_5m_tokens": 5000,
    "cache_write_1h_tokens": 2000,
    "cache_write_unknown_ttl_tokens": 430,
    "output_tokens": 8000,
    "reasoning_tokens": 2500,
    "requests_count": 120,
    "live_requests_count": 100,
    "archive_records_count": 20,
    "sessions_count": 12,
    "cache_hit_rate": 0.3077,
    "api_equivalent_usd": 1.452,
    "api_equivalent_inr": 123.42,
    "actual_billed_amount": 1.20,
    "cache_savings_usd": 0.45,
    "generation_speed_tps": 48.6,
    "ttft_ms": 350.2,
    "session_duration_ms": 125000.0
  },
  "rows": [],
  "units": {
    "total_tokens": "tokens",
    "cache_hit_rate": "ratio",
    "api_equivalent_usd": "USD",
    "api_equivalent_inr": "INR",
    "generation_speed_tps": "tokens/sec",
    "ttft_ms": "milliseconds"
  },
  "metric_definition": {
    "id": "summary",
    "family": "summary",
    "version": "1.0.0",
    "formula": "Aggregation of disjoint counters, financial facts, and timing across scope",
    "denominator": "Disjoint billable token counters for cache rates; eligible timed requests for TPS"
  },
  "eligible": {
    "total_live_requests": 100,
    "timed_requests": 95,
    "priced_requests": 98,
    "labeled_tasks": 80
  },
  "observed": {
    "measured_timing": 95,
    "estimated_timing": 0,
    "unobserved_timing": 5
  },
  "missingness": {
    "missing_timing_requests_count": 5,
    "eligible_timing_requests_count": 95,
    "unpriced_requests_count": 2,
    "priced_requests_count": 98,
    "unlabeled_task_sessions_count": 3
  },
  "coverage": {
    "harness_scope": "combined",
    "active_archive_partitions_count": 1,
    "superseded_archive_partitions_count": 0,
    "unresolved_overlap_partitions_count": 0,
    "unresolved_overlap_requests_count": 0
  },
  "pricing_fx": {
    "pricing_version": "v1.0.0",
    "default_target_currency": "INR",
    "fx_rate_applied": 85.0
  },
  "warnings": []
}
```

---

## 5. Metric IDs, Families & Sorting Allowlists

The endpoint accepts the metric IDs below. Aliases normalize to the family shown; the response data shape is analyzer-defined (`values`, `rows`, or `slices`). Sort fields are validated against the normalized family's allowlist.

| Accepted metric IDs | Normalized family | Supported sort fields |
|---|---|---|
| `summary` | `summary` | `total_tokens`, `requests_count`, `api_equivalent_usd` |
| `timeline` | `timeline` | `bucket`, `timestamp`, `total_tokens`, `requests_count`, `api_equivalent_usd` |
| `models` | `models` | `model`, `display_name`, `total_tokens`, `output_tokens`, `fresh_input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `requests_count`, `sessions_count`, `api_equivalent_usd`, `generation_speed_tps` |
| `projects` | `projects` | `project_id`, `project_name`, `total_tokens`, `api_equivalent_usd`, `sessions_count`, `requests_count` |
| `sessions` | `sessions` | `session_id`, `started_at`, `duration_ms`, `total_tokens`, `api_equivalent_usd`, `requests_count` |
| `requests` | `requests` | `canonical_id`, `request_timestamp`, `duration_ms`, `ttft_ms`, `generation_speed_tps`, `total_tokens`, `output_tokens`, `fresh_input_tokens`, `api_equivalent_usd` |
| `events` | `events` | `event_id`, `started_at`, `duration_ms`, `output_size_bytes`, `event_kind`, `event_name` |
| `coverage` | `coverage` | `start_time`, `end_time`, `grain`, `source_name`, `total_tokens` |
| `tokens`, `cache` | `tokens` | `total_tokens`, `fresh_input_tokens`, `cache_read_tokens`, `output_tokens`, `requests_count`, `sessions_count`, `api_equivalent_usd`, `group_key` |
| `speed`, `performance`, `generation_speed_tps` | `speed` | `weighted_tps`, `mean_tps`, `median_tps`, `median_ttft_ms`, `total_requests`, `sum_output_tokens` |
| `traces`, `attribution` | `traces` | `total_tokens`, `total_requests`, `total_sessions`, `cumulative_input_tokens`, `session_id`, `timestamp` |
| `providers`, `providers_breakdown` | `providers` | `total_tokens`, `requests_count`, `api_equivalent_usd`, `provider`, `account_alias` |
| `quotas`, `provider_quotas` | `quotas` | `provider`, `percent_used`, `reset_time`, `limit_value` |
| `burn`, `token_burn_rate` | `burn` | `current_burn_rate_tpm`, `typical_burn_rate_tpm`, `ratio_vs_baseline`, `provider`, `account_alias` |
| `live`, `live_sessions` | `live` | `started_at`, `last_active_at`, `total_tokens`, `session_id` |
| `budgets`, `usage_budgets` | `budgets` | `name`, `frequency`, `updated_at`, `created_at` |
| `model_profiles` | `model_profiles` | `model`, `display_name`, `total_tokens`, `cost_usd`, `requests_count`, `weighted_tps`, `median_ttft_ms` |
| `outcomes`, `task_outcomes` | `outcomes` | `completion_rate`, `tokens_per_completed_task`, `cost_per_success_usd`, `user_ratings_mean`, `total_tasks`, `task_label` |
| `efficiency` | `efficiency` | `total_tokens`, `potential_savings_usd`, `efficiency_score` |
| `forecasts`, `forecast`, `usage_forecast` | `forecasts` | None |
| `anomalies`, `usage_anomalies`, `speed_anomalies` | `anomalies` | None |
| `comparisons`, `before_after_comparison` | `comparisons` | None |

Composite pages must compose supported analyzer requests rather than invent a page-level metric ID. Harness Comparison queries `summary`, `speed`, model-grouped `tokens`, day and model/day `timeline`, `comparisons`, and `outcomes` independently for each harness scope with the same global filters. Model rows retain token-field and cost coverage; linked-session counts are not additive across models or days. Daily rows can include partial boundary days. Timing samples retain the analyzer's selected basis and denominator. Outcomes are descriptive, sample-gated, and do not establish matched-workload effectiveness. Efficiency uses `efficiency` and `forecasts`; Providers & Limits uses `providers_breakdown`, `provider_quotas`, `token_burn_rate`, and `usage_budgets`.

Harness time comparisons use `summary.session_duration_distribution` (nearest-rank stored session durations with linked-session coverage; source spans may be inferred and are not active time). Daily and model/day `timeline` rows supply clipped timezone-aware bucket bounds, request/session/model mix, and active minutes from unions of explicit request-event intervals, including coverage and unresolved-overlap status. Speed model, route, and model/route slices retain full filtered request counts alongside primary-basis sample counts and nearest-rank tails. The prior comparison is the immediately preceding equal elapsed-duration interval, sent to `comparisons` with the same filters and harness; only request counts are displayed because that analyzer's other aggregate fields may treat missing counters or prices as zero. Combined-share denominators are canonical counts, shares are withheld for unresolved overlap, and session shares need not sum to one; token shares require exact coverage. Known day/model evidence links apply the returned clipped bounds and model filter.

Account-level subscription payments are a separate, non-additive resource
(`GET /api/v1/subscription-fees`) rather than request facts. Its paginated list
is all-history and does not apply the shared query's time or dimension filters.
These payments are excluded from request-linked subscription fee metrics and
filters, actual API billing, and API-equivalent valuations; no per-request,
task, project, model, or harness allocation is inferred. The Providers page
labels this boundary explicitly.

---

## 6. Verification & Performance Guarantees

All query operations must adhere to strict computational bounds:
- **Indexed Access:** Filter lookups utilize `idx_requests_time_harness`, `idx_requests_session`, and `idx_usage_tokens`.
- **Query Latency:** Ingested datasets up to 10,000 canonical requests must execute all metric families under **100 milliseconds** per query on SQLite in-memory or SSD storage.
- **Deduplication:** Repeated or subagent sessions never inflate `sessions_count` or request volume.

---

## 7. Latency Stage Breakdown UI Contract

The Speed latency stage breakdown panel visualizes request duration components from `token_ledger.metrics.performance` without inventing timing, pooling incompatible instrumentation bases, or assuming an additive waterfall.

### Stage Definitions & Eligibility
1. **Time-to-First-Token (TTFT, `ttft_ms`):**
   - **Source Field:** `canonical_requests.ttft_ms` (source-recorded first-output timing).
   - **Eligibility:** Nonnegative recorded TTFT (`ttft_ms >= 0`). Missing TTFT is strictly unobserved (`None`), never substituted with `0.0 ms`.
   - **Semantics:** TTFT reflects the source adapter's recorded first-output instant; first-chunk network arrival, transport latency, and last-token completion are distinct instrumentation bases and are not interchangeable.
2. **Generation Duration (`generation_duration_ms`):**
   - **Source Field:** `canonical_requests.generation_duration_ms` (source-recorded generation span).
   - **Eligibility:** Positive recorded generation span (`generation_duration_ms > 0`). Token availability is not required: eligible requests with `output_tokens == 0` are retained in sample counts.
   - **Semantics:** Measures generation interval only. Requests with missing generation spans are strictly unobserved and excluded from throughput and latency distributions.
3. **Total Duration (`duration_ms`):**
   - **Source Field:** `canonical_requests.duration_ms` (source-recorded request elapsed duration).
   - **Eligibility:** Nonnegative recorded total span (`duration_ms >= 0`).
   - **Semantics:** Represents total end-to-end request duration observed by the harness adapter.
4. **Queue / Start Span (`queue_duration_ms`):**
   - **Source Field:** `canonical_requests.queue_ms` (or explicit queue instrumentation when supplied by adapter).
   - **Eligibility:** Nonnegative explicitly recorded queue span only (`queue_ms >= 0`).
   - **Semantics:** Queue / start latency is **never** inferred or synthesized by subtracting TTFT, generation duration, or total spans. If explicit queue instrumentation is absent, queue duration status is `"unavailable"` with `None` values and sample count 0.

### Timing Basis Separation
- Stage distributions are segmented by timing basis: `measured` (source-recorded timestamps), `log_span_proxy` (inferred from external log entry spans), and `untimed` (`none`).
- These bases are **never pooled** into an unsegmented aggregate.
- The UI provides a basis selector defaulting to the query's primary timing basis (or `untimed` when unobserved), displaying the basis sample count alongside total filtered requests.

### Independent Sample Sets & Denominators
- Each stage maintains an independent sample count and denominator. A request with a recorded TTFT but missing generation span contributes to TTFT distributions while abstaining from generation distributions.
- Sample sets are descriptive distributions rather than an additive waterfall. TTFT, generation, and total spans are not assumed to sum to total request duration.

### Unavailable Value Presentation
- Any unmeasured, missing, or unsupported stage time is explicitly displayed as `"Unavailable"`. Missing timing is never represented as `0 ms` or zero speed.
- If a timing basis contains zero eligible requests for a stage, its statistics (mean, p50, p90, p95) display `"Unavailable"`.
- Percentiles ($p_{50}, p_{90}, p_{95}, p_{99}$) follow deterministic nearest-rank calculation.
