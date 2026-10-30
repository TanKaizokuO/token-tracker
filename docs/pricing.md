# Token Ledger Pricing, Model Identity, and Valuation Architecture (WP06)

This document defines the pricing engine, dated rate card schedules, model identity resolution, historical foreign exchange (FX) mechanisms, and financial facts valuation architecture in Token Ledger.

---

## 1. Seven Canonical Display Identities

Per rebuild plan (all76, line 12), plus `Terra 6.1`, Token Ledger explicitly tracks and renders seven canonical model display identities with exact spacing:

1. **`Opus 5.5`**
2. **`Sonnet 5.5`**
3. **`Haiku 5.5`**
4. **`Sol 6.1`**
5. **`Terra 6.1`**
6. **`Luna 6`**
7. **`Gemini 3.8 Flash`**

### 1.1 Configurable Mapping Rules

Model identity mapping is configurable and explicit via `ModelMappingRule`:
- Original model identifiers (`raw_model`) and sanitized base identifiers (`normalized_model`) are strictly preserved in `canonical_requests`.
- Access routes (`default`, `direct`, `bedrock`, `openrouter`) are tracked alongside model identities.
- If a model cannot be resolved to a known identity under the active configuration, its display name is `None` (`is_known = False`).
- **Never Guessed:** Token Ledger never guesses, approximates, or synthesizes a provider or model identity. Unknown models remain explicitly unknown.

```python
from token_ledger.pricing import resolve_model_identity, CANONICAL_DISPLAY_IDENTITIES

res = resolve_model_identity("claude-sonnet-5-5-20251001", provider="anthropic")
assert res.display_name == "Sonnet 5.5"
assert res.is_known is True
assert res.canonical_model_id == "claude-sonnet-5-5"

unknown_res = resolve_model_identity("custom-local-model", provider="local")
assert unknown_res.display_name is None
assert unknown_res.is_known is False
```

---

## 2. Dated Pricing Schedules & Versioning

Rate cards are stored in SQLite under `pricing_versions` and `model_pricing_rates`.

### 2.1 Schedule Integrity Rules

1. **Effective Date Ranges:**
   - Each rate schedule defines `effective_start` (inclusive) and optional `effective_end` (exclusive).
   - Rate schedules are queried by the request's ISO-8601 timestamp (`request_timestamp`).
2. **No Latest Retroactive Fallback:**
   - If a request occurred prior to the earliest effective start, or falls in an uncovered gap, it is marked `pricing_status = 'unpriced'`.
   - Token Ledger **never** applies the latest rate card retroactively to historical requests.
3. **Cryptographic Digest:**
   - New schedules' SHA-256 digests include currency, normalized rates, and mappings.
4. **Currency Basis:**
   - Rates per 1M tokens are denominated in the schedule's three-letter currency.


### 2.2 Query-local identity resolution

The shared request selector loads only dated identity schedule metadata once per
call and memoizes repeated model/provider/route identities within that call.
Both request and schedule instants use SQLite Julian-day semantics, including
half-open boundaries and the existing start/creation/version tie-break order.
Explicit assigned versions (including missing IDs) never fall back to another
effective schedule. Unknown aliases and missing financial values remain null.
Snapshots are not shared across queries: subsequent pricing, provenance, or
request-model writes are visible on the next call, including uncommitted writes
on the same connection. Rate cards and financial valuations are not hydrated by
the identity resolver.
---

## 3. Disjoint Counter Valuation & Thresholds

Token Ledger evaluates requests against the 6 disjoint token counters stored in `normalized_usage`:
- `fresh_input_tokens` (rate: `input_rate_per_m`)
- `cache_read_tokens` (rate: `cache_read_rate_per_m`)
- `cache_write_5m_tokens` (rate: `cache_write_5m_rate_per_m`)
- `cache_write_1h_tokens` (rate: `cache_write_1h_rate_per_m`)
- `output_tokens` (rate: `output_rate_per_m`)
- `cache_write_unknown_ttl_tokens` (no rate of its own; priced at `cache_write_5m_rate_per_m`, see 3.2)

### 3.1 Prompt Thresholds & Tiered Context Pricing

Models with tiered pricing (such as Claude Haiku 5.5) specify `prompt_threshold` (e.g. 100,000 tokens) and `tiered_rates_json`.
- When total prompt tokens (`fresh_input + cache_read + cache_write_5m + cache_write_1h + cache_write_unknown_ttl`) exceed the threshold, the entire request reprices under the long-context rate schedule.

### 3.2 Unknown TTL Handling

- Flat cache-write tokens lacking explicit 5-minute or 1-hour TTL breakdowns are preserved in `cache_write_unknown_ttl_tokens`.
- Assumption: unknown-TTL writes are priced at the model's 5m cache-write rate (the API default TTL). They are fully priced with the request's normal status, and `cache_savings_usd` is computed on them.
- Only a model with no 5m write rate leaves them unpriced: that request is `pricing_status = 'partial'` and `cache_savings_usd` is `None`.

---

## 4. Historical Foreign Exchange (FX)

Exchange rates are stored in `historical_fx`:
- Maps `(base_currency, target_currency, effective_date)` with recorded `source_provenance`.
- For dates falling between recorded exchange rates, the closest prior effective rate is applied.
- **Strict Null Policy:** If no FX rate exists on or prior to the requested date, `get_historical_fx` returns `None`. Unknown FX rates are **never** reported as `0.0`.
- Non-USD cards require a direct card-currency/USD record; no inverse, cross-rate,
  future rate, or latest-card fallback is inferred. USD/INR is then applied
  separately when available. Dates are UTC request dates, including for explicit
  what-if versions. A missing card conversion makes USD and INR unavailable;
  a missing USD/INR conversion leaves USD available and INR null.
- Valuation previews return the applied FX records, including source, effective date,
  and ID; scenario evidence exposes the card conversion. Persisted financial facts
  snapshot the card currency and both applied FX records in
  `valuation_provenance_json`. Facts predating the provenance migration remain null
  until explicitly recalculated; existing billed and subscription facts are preserved.
  Request-inspection query rows return the stored `valuation_pricing_version_id`
  and `valuation_provenance` separately from their display-identity version.

---

## 5. Separation of Financial Measures

Token Ledger maintains strict architectural boundaries between distinct financial concepts:

| Financial Metric | Database Field | Definition & Semantic Guarantee |
|---|---|---|
| **Actual Billed Spend** | `actual_billed_amount` | Ground-truth charges from provider billing records or invoices (e.g., OpenRouter billed cost). Never inferred from token counts. |
| **API-Equivalent Value** | `api_equivalent_usd` / `api_equivalent_inr` | Hypothetical list-price valuation of disjoint tokens based on public rate cards. |
| **Subscription Fees** | `subscription_fee_usd` | Fixed monthly or annual user plan fees (e.g., Claude Pro/Team). Separated from usage-based valuations. |
| **Cache Savings** | `cache_savings_usd` | Estimated economic benefit of prompt caching: `(hypothetical fresh input cost) - (actual cache cost)`. `None` only when a required cache rate is missing (for unknown-TTL writes, a missing 5m rate). |
| **Unpriced Usage** | `pricing_status = 'unpriced'` | Requests where model rates or schedules are missing. Valuations are preserved as `None` (never `$0.00`). |

---

## 6. Legacy `pricing.json` Ingestion Boundary

Existing `pricing.json` files from legacy prototypes may be imported via `import_legacy_pricing()` under the following constraints:
1. Imported as an undated baseline rate card (`status = 'unverified'`).
2. Source provenance explicitly flags the schedule as legacy unverified data.
3. Historical effective schedules are **never fabricated** across past periods.

## 7. Efficiency Scenario Valuation

Efficiency scenarios are evaluated over the request rows selected by the shared, validated query envelope. The scenario does not run a second aggregation: the query's time range, filters, search, and harness scope determine the source rows, and the response reports both the scenario-selected count and the query-filtered count.

Baseline and projected API-equivalent costs are valued per request using its `request_timestamp` and the effective pricing schedule for that date. Baseline rates use the observed provider, raw/normalized model identity, and access route; model-switch scenarios use the selected target model and any supplied target provider or route. Evidence reports the schedule ID, effective window, pricing hash, rate identity/status, provenance, and request count.

Scenario assumptions and warnings are returned with the result. Missing usage counters, unknown-TTL cache writes, missing effective schedules, absent provider/model/route rates, and partial or unpriced rates remain visible as unknown or incomplete; they do not become zero-valued costs or savings. `cache_write_unknown_ttl_tokens` remains distinct from priced cache-write counters.

These results are what-if calculations over observed counts, not forecasts, guarantees, actual billed spend, or estimates of task quality. In particular, a model-switch price comparison does not assert equivalent outcomes.

## 8. Local pricing-management API

The following endpoints use the existing loopback Host validation. POST requests also
require the bootstrap session cookie, same-origin `Origin` or `Referer`, and
`X-CSRF-Token`. No endpoint modifies source observations or normalized usage.

- `GET /api/v1/pricing/versions` returns `{ "versions": [...] }`, including
  each version's ID, digest, UTC effective window, currency, source provenance,
  `model_mappings`, and complete `rates`. A rate contains provider, model identity,
  access route, input/output and optional cache rates per million tokens, tier
  threshold and rates, status, source URL, verification date, and note.
- `POST /api/v1/pricing/versions` creates an immutable version; duplicate IDs return
  409. Supply `version_id`, timezone-aware `effective_start`, optional exclusive
  `effective_end`, three-letter `currency_code`, `source_provenance`
  object, and nonempty `rates` array. Rates require `provider`, `model_identity`,
  `input_rate_per_m`, and `output_rate_per_m`; `access_route` defaults to `default`.
  Numeric rates must be finite and nonnegative. Optional `is_tiered`,
  `prompt_threshold`, and `tiered_rates_json` (an object with the same rate field
  names) configure long-context pricing. Status is `verified`, `estimated`, or
  `unverified`. Correct an existing card by creating a new version, not replacing it.
- Optional `model_mappings` is an array of literal `{provider, access_route, alias,
  model_identity, display_name?}` entries. Each maps a raw or normalized alias,
  including a reasoning variant, to a rate in the *same* version, provider and route.
  Mappings are stored with the version and included in its digest. Unmatched models
  remain unpriced; raw model IDs remain unchanged.
- `GET /api/v1/pricing/fx` returns `{ "fx_rates": [...] }`, including currency
  pair, effective date, rate, source, and creation time. `POST` to the same path
  accepts `base_currency`, `target_currency`, `rate_per_usd` (target units per
  base unit), `effective_date` (`YYYY-MM-DD`), and `source_provenance`. A
  duplicate pair/date returns 409. Direct FX records are selected on or before
  the request's UTC date; missing conversions remain null.
- `POST /api/v1/pricing/recalculate` accepts `{ "canonical_id": "...",
  "pricing_version_id": "..." }`; omit or set the version to null to select the
  schedule effective at the request timestamp. An explicit version is a what-if
  selection even outside its effective window. Responses contain `mode`
  (`historical` or `explicit`), `persisted`, and `valuation`. By default this is a
  **preview**, with no database mutation. To update the derived `financial_facts`
  for that request, pass `"persist": true`; this preserves existing actual billed
  amounts/currency and subscription fees, as well as the canonical request and
  normalized usage. Subsequent historical selection still uses the request date.
  A missing request/version returns 404; incomplete six-counter usage returns 409
  rather than treating missing as zero. A request without an applicable rate
  returns an unpriced valuation.

- Alternatively, supply `query` instead of `canonical_id` to select every matching
  canonical request using the validated shared-query envelope, without pagination.
  Archive aggregates are not requests and are never recalculated. The response
  contains `selected_count`, `valuations`, and `abstentions`; each abstention names
  the request and missing counters. Missing counters are not zero, and abstained
  requests are left unchanged even when `persist` is true. Eligible writes occur
  together in a transaction and preserve source and billed/subscription facts.
  A persisted operation evaluates the query again, so the selected population may
  differ from a previous preview if the underlying data changed.

The Reports & Settings pricing panel lists immutable versions, rates, mappings,
and dated FX, creates versions and FX entries, and previews or explicitly confirms
single-request or current-filter recalculations. Extra rates and aliases can be
supplied as JSON arrays when creating a version. An incomplete single request is
displayed as an abstention. Unpriced and partial valuations remain visible.

Model distribution, request inspection and model profile displays resolve aliases
using the assigned pricing version, or the effective version at the request time
when none is assigned. Mapping is scoped to provider and route; model filters still
match raw or normalized IDs. Model distribution rows are separated by raw identity,
route, provider and version so changing aliases cannot collapse distinct displays.

