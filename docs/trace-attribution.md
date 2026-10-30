# Trace Attribution & Context Analysis (WP15)

## Overview

The `token_ledger.metrics.traces` module provides deep hierarchical trace attribution, session tree execution modeling, context window occupancy tracking, compaction analytics, tool usage overhead analysis, error/retry loop detection, and privacy-preserving repetition analysis.

This module implements the requirements for Work Package 15 (WP15) and addresses Rebuild Plan Rows 10, 11, 12, 14, 29, 32, 33, 34, 40, and 41.

---

## 1. Core Principles & Guarantees

### 1.1 Shared Query Filter Selection
All request-scoped trace analyses operate on canonical requests selected by `token_ledger.query.filtered_request_rows`. Request-linked events must always name one of those exact canonical IDs; sharing a session is insufficient, including with `group_by: ["session"]`. Grouping never expands eligibility. `events`, `values.total_events`, tool/subagent/MCP/skill/hook/compaction aggregates, event-kind counts, retry and repetition analyses retain this boundary; event eligibility follows request identity, not independent event timestamps. `session_level_events` separately exposes only events with NULL canonical ID from linked sessions, with `session_level_event_scope: "linked_sessions_unfiltered_by_request_or_time"`: they may be outside the selected period, do not inherit model/route/request attribution, and are excluded from request-scoped metrics with a warning even under explicit session grouping. No selected requests means no linked sessions and both event collections empty. Linked-session hierarchy and outcomes are session-level evidence, not proof of request-level causation.

### 1.2 Exclusive Ownership vs. Inclusive Rollups
- **Exclusive Attribution:** Direct ownership of tokens, costs, and requests by an individual session, subagent, or prompt. Top-level `values["total_tokens"]` strictly reflects the non-overlapping exclusive sum of filtered requests.
- **Inclusive Rollup:** Hierarchical sum aggregating an entity's exclusive usage plus all descendant subagent sessions (`inclusive_tokens`, `inclusive_requests_count`).
- **No Double-Counting:** Inclusive rollups are reported in dedicated fields on session tree nodes and are never double-counted into parent session exclusive metrics or top-level totals.

### 1.3 Context Occupancy vs. Cumulative Input Proxy
- **Reported Context Occupancy:** Calculated as instantaneous turn prompt tokens (`fresh_input_tokens + cache_read_tokens + cache_write_tokens`) divided by the model's verified context window limit (e.g., 200,000 tokens for Claude 3.5 Sonnet).
- **Cumulative Input Proxy:** Distinct metric (`cumulative_input_tokens`) reflecting the total billed input volume accumulated across all sequential turns. Conflating cumulative input with instantaneous context occupancy is strictly prohibited.
- **Unverified Limits:** If a model's context window limit is unverified or unknown, `occupancy_ratio` is returned as `None` (unavailable) and tracked under `missingness.unobserved_context_window_requests_count`.

### 1.4 Compaction Nullability
Compaction events record `before_tokens`, `after_tokens`, `reduction_tokens`, and `reduction_ratio`. When any of these measurements are not recorded by the source harness, they remain `None` (nullable). They are never defaulted to `0`. Top-level compaction uses only request-linked events from filtered canonical IDs. Standalone compaction before/after/reduction analysis is returned separately as `session_level_analysis.compaction`, inheriting `session_level_event_scope: "linked_sessions_unfiltered_by_request_or_time"`, not request `event_scope`.

### 1.5 Privacy Guarantees & Content Protection
- **No Tool Bodies or Payloads:** Tool arguments, input payloads, code snippets, and execution outputs are stripped from trace views. Only allowlisted metadata, measured output size, and explicit evidence fields are preserved.
- **Consent-Gated Content Fingerprints:** Content digests are considered only with explicit `content_fingerprints` opt-in. The analyzer never opens a local source path to read its body. A path alias or its opaque locator hash identifies a location, not content.
- **Repeated-Read Evidence:** Repeated paths are `changed`/`unchanged` only when every read has an opt-in source content digest, or when explicit source-change evidence supports that conclusion. Digest evidence is never inferred from the path; without sufficient evidence the result is `unknown`. Repeated-read classifications remain suspected signals, not proven waste.

### 1.6 Suspected Signals vs. Proven Waste
Repeated file reads, identical commands, and retry sequences are labeled strictly as `suspected_signal` / `suspected_waste_signals`. They include source evidence links (request IDs, session IDs, event IDs) and are never claimed as "proven waste", recognizing legitimate verification patterns.

---

## 2. Native Event Ingestion Status

### Native event normalization and persistence
Both ingestion adapters call the shared native-event extractor before returning skipped non-assistant observations. Tool calls, tool results, and compaction records are stored without creating canonical requests for standalone records. Assistant tool events retain their canonical request association; source event IDs are deduplicated.

For Claude Code, `parentUuid` is an event/message parent, never a session parent. `parentSessionId` and `parentSession` are distinct session-parent fields. When a source event names a parent not yet present in SQLite, ingestion retains the ID in `details_json.unresolved_parent_event_id`; it links the event when the parent arrives in a later batch. Missing source parents remain unresolved and are never fabricated.

Event payloads and tool arguments are not persisted. Safe metadata, measured output size, and nullable compaction counters are retained; measured zero values remain zero. Request-linked traces select exact filtered canonical IDs; standalone session events are exposed separately and are not used in request comparisons.

### Coverage limits
Top-level `source_coverage` reports event classes not observed in request-scoped analysis as unavailable. `session_level_analysis` separately supplies tools, compaction, subagent/MCP/skills/hooks and `counts_by_kind` breakdowns for standalone `session_level_events`, without duplicating their event list or changing request-scoped coverage. Ingestion does not synthesize absent event classes, request records, or parent relationships.

---

## 3. Public API

```python
from token_ledger.metrics.traces import (
    analyze,
    build_session_tree,
    calculate_context_occupancy,
    detect_retry_loops,
    analyze_repetition,
    lookup_context_window,
)
```

### `analyze(conn: sqlite3.Connection, envelope: Dict[str, Any]) -> Dict[str, Any]`
Accepts standard query envelope, retrieves filtered request rows, and produces the complete trace attribution response envelope.

## 4. Sessions Trace Panels and Field Semantics

The Sessions trace view consumes the shared `traces` query response with the current filters and the selected session ID; it does not aggregate a second copy of requests or events in the browser. The request inspector remains scoped to the selected session and can jump to the corresponding linked timeline event.

Trace events are displayed as a parent-linked expandable tree. Tool results appear under their observed parent tool event when that link exists; missing links remain reported as missing rather than synthesized. Expandable sections group the analyzer's safe event categories (tools, compaction, subagents, MCP, skills, and hooks) without presenting raw payloads.

- **Context:** Per-turn reported occupancy, verified window, occupancy ratio, and remaining tokens are distinct from cumulative input proxy. `input_proxy_growth_tokens` is the per-turn growth in the proxy; it is not another occupancy measure. Unknown context windows retain unavailable ratios/headroom.
- **Prompt attribution:** Prompt groups show direct request attribution and unlinked requests/tokens. The response contract does not identify primary versus follow-up prompts, so the view does not infer those roles from prompt names or order. Session nodes expose direct/exclusive values separately from inclusive subagent rollups; missing parent links are not fabricated.
- **Tools:** Tool records retain original tool names, invocation counts, observed timing, success/failure counts, and measured output bytes. `timed_invocations_count` and `output_size_observed_count` distinguish measurements from missing measurements.
- **Compaction:** Before/after token counts, reduction, and turns compacted come from source events. Unreported measurements remain unavailable. Request-scoped compaction and standalone `session_level_analysis.compaction` are distinct; standalone measurements retain the linked-session-unfiltered-by-request-or-time scope and must not be presented as filtered request totals.
- **Retry and loops:** Request retry attempts/status and consecutive same-name, same-session tool calls ending in an error/timeout/failed status produce `suspected_signal` records. Token counts on retry signals are observed request tokens; a suspected signal does not establish avoidable waste.
- **Repeated reads:** Read/view/cat/open tool calls with a sanitized path alias are grouped when repeated. The evidence state uses a full set of explicit, consent-gated digests or explicit source-change metadata; otherwise it is `unknown`. Opaque path hashes are locator aliases, never content fingerprints. Source details and file bodies are not fetched to fill gaps.

### Harness and equivalent-period comparisons

Harness tool and reliability rows consume the shared trace analyzer, retaining original tool names, observed timing and output-size counts, status counts, source coverage, and request/event/session evidence for suspected retry signals. Unlinked session events are counted separately and never included in those request-scoped metrics. Subagent session counts are linked-session evidence; subagent event counts are request-linked.

Equivalent-period comparisons use identical filters and half-open intervals. Token and financial values carry `coverage` of `exact`, `partial` (an observed subset), or `unavailable` (null); measured zero remains zero. Billable totals use the six disjoint counters, including unknown-TTL writes. Cache hit rate uses requests with both input counters; deltas for partial or unavailable totals, prices, and cache rates are withheld. Outcomes reuse the shared outcome analyzer and may refer to linked sessions containing other requests; sparse outcomes do not establish effectiveness. Retry rates describe observed retry attempts or error statuses, not proven waste. Model mix, task distribution, duration and DST confounders remain descriptive, not causal.

### Synthetic browser interaction evidence

The trace panels were exercised with a synthetic fixture only. Selecting a session sent both the request-inspector and `traces` queries through the shared query contract with that session scoped in the filters. The browser expanded a parent tool event and its linked result, expanded the context, prompt, tool, hierarchy, compaction, retry, repeated-read, and source-category panels, and used the inspector's jump action to open and scroll to the matching timeline event. The synthetic repeated-path example remained `unknown` with fingerprint consent disabled; no payload body or digest was displayed.
