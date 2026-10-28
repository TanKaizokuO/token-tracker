# Token Ledger Rebuild — Source Capability Matrix

**Status:** Verified Groundwork (WP01 Capability Slice)  
**Date:** 2026-10-26  
**Audience:** Rebuild implementation agents (WP02–WP24)  
**Source Evidence:** `tokentrack.py`, `harness.py`, `pricing.json`, `TOKEN_LEDGER_REBUILD_PLAN.md`, `local://token-ledger-rebuild-research.md`  

---

## 1. Overview and Core Architectural Distinctions

This document inventories source capabilities across the two supported collector inputs (**Claude Code** and **Oh-My-Pi (OMP)**) and defines the boundary between **verified existing capabilities** and **unavailable or unverified fields**.

### Key Rebuild Principles:
1. **Measured Zero vs. Unavailable:** `0` represents an explicitly measured quantity of zero. `null` / `unavailable` represents data that the source cannot supply. A missing counter must never be coerced to `0`.
2. **Observation vs. Canonical Request:** Raw source observations (file locator, byte/line offset, parser version, raw source timestamps and raw counter payloads) must remain distinct from canonical deduplicated requests.
3. **Disjoint Billable Tokens:** Billable token totals are derived exclusively from disjoint normalized counters (`fresh_input`, `cache_read`, `cache_write_5m`, `cache_write_1h`, `cache_write_unknown_ttl`, `output`). Reported totals and reasoning subsets are preserved separately and never blindly summed.
4. **No Invented Thresholds or Hooks:** Official quota, subscription tier limits, streaming TTFT (for Claude Code), and network billing APIs are unavailable locally. They must display explicit unavailable states rather than heuristic approximations or unverified network calls.

---

## 2. Dimension Capability Matrix

The table below catalogs capabilities across all required evaluation dimensions.

| Dimension | Field / Concept | Claude Code Status | Oh-My-Pi (OMP) Status | Canonical Rebuild Requirement | Verified vs. Unavailable |
|---|---|---|---|---|---|
| **IDs** | Request ID | Available (`d["requestId"]`) | Available via `msg["responseId"]` (or fallback to `d["id"]`) | Stored on canonical request. Stable only within `(provider, nonsecret_account_id)`. | **Verified** |
| | Message ID | Available (`msg["id"]`) | Available via event ID `d["id"]` | Preserved in observation record. Claude dedupe uses `[requestId, msg.id]`. | **Verified** |
| | Session ID | Derived (`s_<hash(claude-code\0rel)>`) | Derived (`s_<hash(omp\0rel)>`) | Stable hash based on relative path under source root. File paths must not be persisted in payloads. | **Verified** |
| | Parent Session ID | Inferred from path: `<project>/<session>/subagents/<agent>.jsonl` | Explicit header entry: `d["type"] == "session"` -> `d["parentSession"]` | Explicit `parent_session_id` link. Distinguish exclusive child work from inclusive parent rollups. | **Verified** |
| **Account** | Account Identifier | Not present in transcript JSONL | Not present in session JSONL | Scope request identity to `(provider, account_alias)`. Must use a local non-secret account alias (`default`). Never assume cross-user or multi-tenant scope. | **Unavailable** in raw sources; require fallback local alias |
| **Origin** | Origin Harness | `claude-code` | `omp` | Stored as `origin_harness`. Retain origin identity even if parsed via another adapter. | **Verified** |
| **Observation** | Observation Record | In legacy `tokentrack.py`, observation was merged into request row | In legacy `tokentrack.py`, observation was merged into request row | Decouple `request_observations` from `canonical_requests`. Track source file generation, line index, raw payload, parse status. | **Unavailable** in legacy; target schema requirement |
| **Provider** | Provider Name | Hardcoded to `"anthropic"` | Explicit in assistant message: `msg.get("provider") or "unknown"` | Canonical request stores verified provider name. Never merge routes across providers. | **Verified** (OMP); **Verified constant** (Claude Code) |
| **Route** | Access Route | Unverified / absent (direct Anthropic API implied) | Partial: OpenRouter model prefix (`anthropic/...`) or provider `"openrouter"`; else unverified | Preserve raw route string if present; otherwise mark route as `unknown` or `direct`. Do not invent routes. | **Unavailable** (direct routes); **Partial** (OMP OpenRouter) |
| **Token Semantics** | Fresh Input | `usage.input_tokens` | `usage.input` | Normalized `fresh_input_tokens`. Represents non-cached prompt tokens. | **Verified** |
| | Cache Read | `usage.cache_read_input_tokens` | `usage.cacheRead` | Normalized `cache_read_tokens`. Subtracted or separate depending on provider billing rules. | **Verified** |
| | Cache Write (5m TTL) | `usage.cache_creation.ephemeral_5m_input_tokens` | `usage.cttl.ephemeral5m` | Normalized `cache_write_5m_tokens`. Priced at 5m rate (1.25x input). | **Verified** |
| | Cache Write (1h TTL) | `usage.cache_creation.ephemeral_1h_input_tokens` | `usage.cttl.ephemeral1h` | Normalized `cache_write_1h_tokens`. Priced at 1h rate (2.0x input). | **Verified** |
| | Cache Write (Unknown TTL) | Legacy flat field: `usage.cache_creation_input_tokens` | Legacy flat field: `usage.cacheWrite` | Legacy `tokentrack.py` assigned flat writes to 5m TTL. Target MUST store as `cache_write_unknown_ttl` and avoid claiming verified 5m pricing. | **Verified present in logs**; **Legacy coercion must be eliminated** |
| | Output Tokens | `usage.output_tokens` | `usage.output` | Normalized `output_tokens`. Generation count. | **Verified** |
| | Reasoning Tokens | Absent in legacy logs (or subsumed in output) | Subsumed in `output` or provider-specific variant | Modeled as reported subset of `output_tokens` unless provider explicitly proves disjoint. | **Unavailable / Unreported** in existing legacy logs |
| | Reported Total | Not stored; legacy computed sum `in + out + cr + cw5m + cw1h` | Not stored; raw `cost` in OMP ignored due to 100x inflation | Keep provider-reported total if present; never sum overlapping subsets. | **Unavailable** in legacy logs; maintain separate field |
| **Time Units** | Timestamp Format | ISO-8601 string or epoch ms (`_iso()`) | ISO-8601 string or epoch ms (`_iso()`) | Parse to UTC ISO-8601 string; project to display timezone via IANA `ZoneInfo`. | **Verified** |
| | Duration | Unavailable / noisy (`secs: None`) | `msg["duration"]` (milliseconds) | Total request round-trip time. Measured in milliseconds, converted to seconds. | **Verified** (OMP); **Unavailable** (Claude Code) |
| | Time to First Token (TTFT) | Unavailable | `msg["ttft"]` (milliseconds) | Time from request dispatch to first stream token. Must not be pooled with non-streaming latency. | **Verified** (OMP); **Unavailable** (Claude Code) |
| | Generation Duration | Unavailable | `(dur - ttft) / 1000` (seconds) | Usable if `dur > 0` and `0 < secs <= 600`. Disclose proxy nature; do not assign zero speed to untimed requests. | **Verified** (OMP); **Unavailable** (Claude Code) |
| **Tool Usage** | Tool Invocations | `d["type"] == "assistant"` -> `msg.content` blocks with `type == "tool_use"` (`[ts, id, name]`) | `msg.content` blocks with `type == "toolCall"` (`[ts, id, name]`) | Capture tool ID, name, timestamp. Group standard tools; never store arguments or bodies by default. | **Verified** |
| | Edit Tool Classification | `EDIT_TOOLS = {"edit", "write", "Edit", "Write", "MultiEdit", "NotebookEdit"}` | Same set applied | Count tool calls vs edit tool calls. | **Verified** |
| | Tool Output Size | Not parsed in legacy collector | Not parsed in legacy collector | Target deep observability (WP15) captures output character/byte length metadata without storing bodies. | **Unavailable** in legacy; target requirement |
| **Trace** | Subagent Linkage | Inferred from path containing `/subagents/` or `d.isSidechain` | Explicit via `type == "session"` and `parentSession` pointer | Model explicit parent/child session trees. Reconcile exclusive child usage with inclusive parent sessions. | **Verified** |
| | Prompt Attribution | Unavailable (prompt user messages not parsed/linked) | Unavailable (session entries lack structured prompt linking) | Attribution of tool loops and subagents to parent prompt is unavailable without transcript linking (WP15). | **Unavailable** in legacy logs |
| **Rotation** | File Rotation / Truncation | Unavailable (legacy `ParseCache` keys on `st_mtime_ns:st_size`, reparses entire file on change) | Unavailable (same parse cache mechanism) | Incremental ingestion (WP04) requires durable byte/line checkpoints and inode/file-generation tracking. | **Unavailable** in legacy |
| **Billing** | Pricing Models | Defined in `pricing.json` (Anthropic models, Gemini, OpenAI, GLM, Kimi) | Defined in `pricing.json` | Normalized model names mapped via `rate_key`. Fall back to base model for effort suffixes. | **Verified** |
| | Tiered Pricing | Handled for `claude-haiku-5-5` (>100k prompt tokens uses `long` tier) | Supported if present in `pricing.json` | Model-specific prompt threshold checks. | **Verified** |
| | Actual Billed Cost | Unavailable (no billing API/invoices connected) | Stored `cost` in OMP is unreliable (inflated 100x); ignored by `tokentrack.py` | Distinguish API-equivalent value from actual billed spend. Show equivalent value; label unbilled. | **Unavailable** (actual billing facts) |
| | Currency & FX | Base USD; INR converted via `per_usd: 95.56` | Base USD; INR converted via `per_usd: 95.56` | Historical dated FX series. Pricing in USD; INR display supported via explicit FX snapshot. | **Verified** (static FX); **Unavailable** (dated series) |
| **Quota** | Subscription Quotas | Unavailable | Unavailable | Official provider quota and limits are absent from local logs. Views must state "Quota unavailable", not infer. | **Unavailable** |
| | Rate Limits / Resets | Unavailable | Unavailable | Cannot be inferred from token counts. Views must report unavailable. | **Unavailable** |
| **Privacy** | Content Bodies | Excluded (legacy parser skips message content except tool blocks) | Excluded (legacy parser skips message content except tool calls) | Default strict privacy: no raw prompt/response bodies, secrets, or file contents persisted or exported. | **Verified** default exclusion |
| | Project Names / Paths | Extracted from `cwd` or directory name; aliased via `PROJECT_ALIASES` | Extracted from `cwd` or directory name; aliased via `PROJECT_ALIASES` | Project paths sanitized to project name or non-secret alias. | **Verified** |
| | Synthetic Secrets Redaction | Not implemented in legacy | Not implemented in legacy | Mandatory redact-before-write/export policy (WP07) for API keys, bearer tokens, passwords. | **Unavailable** in legacy; target requirement |
| | Telemetry / Cloud Calls | None (`tokentrack.py` is local stdlib only) | None (`tokentrack.py` is local stdlib only) | Rebuild runtime MUST remain strictly local loopback (127.0.0.1) with zero external network requests. | **Verified** |

---

## 3. Detailed Dimension Findings and Groundwork Constraints

### 3.1 Identifiers, Accounts, and Origin Scope
- **Claude Code**: Requests are identified by `[d.get("requestId"), msg.get("id")]`. Sessions are keyed by hashing the relative file path under `~/.claude/projects`. Subagents are stored under `<project>/<session>/subagents/<agent>.jsonl` or flagged with `isSidechain: true`.
- **Oh-My-Pi (OMP)**: Requests are identified by `msg.get("responseId")` or event `d.get("id")`. Sessions are keyed by hashing the relative file path under `~/.omp/agent/sessions`. Subagents specify `parentSession` in their `type: "session"` header.
- **Account Scope**: Neither source records account IDs or user emails. The canonical model must scope request IDs to `(provider, account_alias)` where `account_alias` defaults to `default`. IDs from distinct accounts or origins must never be merged.
- **Cross-Source Overlap**: A single LLM request executed through an agent might conceivably be observed in both harnesses if nested. They must not be merged based on timestamp or token equality alone; unresolved overlap must be flagged explicitly.

### 3.2 Token Semantics and Unknown TTL
- **Disjoint Counters**: Normalized usage comprises six mutually exclusive counters:
  1. `fresh_input`: Non-cached prompt tokens.
  2. `cache_read`: Input tokens loaded from cache.
  3. `cache_write_5m`: Cache creation tokens with 5-minute ephemeral TTL.
  4. `cache_write_1h`: Cache creation tokens with 1-hour ephemeral TTL.
  5. `cache_write_unknown_ttl`: Legacy cache writes where TTL was unrecorded.
  6. `output`: Total generated output tokens.
- **Legacy Flaw**: `tokentrack.py:443–445` and `503–505` coerced missing TTL writes directly into `cw5m`. The rebuild canonical model MUST retain `cache_write_unknown_ttl` as a distinct field to avoid falsifying economic break-even analyses.
- **Reasoning Tokens**: When providers report reasoning/thinking tokens (e.g. Gemini 3.8 Flash, Claude extended thinking), these are a subset of `output`, not an addition to it, unless disjointness is proven.

### 3.3 Timing and Speed Semantics
- **Claude Code**: Timestamps exist only at the message envelope level (`d["timestamp"]`). No per-request duration or TTFT is recorded in transcripts. `secs` is set to `None`. Claude Code requests are therefore **untimed**. Untimed requests must NOT be assigned 0 tokens/sec.
- **Oh-My-Pi**: Messages provide `duration` (total ms) and `ttft` (ms to first token). Generation time is calculated as `(duration - ttft) / 1000.0`. Validated if `dur > 0` and `0 < secs <= 600`.
- **Latency Stages**: OMP measures `ttft` and `generation = duration - ttft`. Network queuing, DNS, and client overhead stages are **unavailable**.

### 3.4 Billing, Pricing, and Provenance
- `pricing.json` defines per-1M token rates in USD for model identities.
- Normalization strips OpenRouter prefixes (`anthropic/claude-opus-5.5` -> `claude-opus-5.5`), date stamps (`-20251001`), and effort suffixes (`-low`, `-medium`, `-high`).
- Tiered pricing exists for `claude-haiku-5-5` (prompt tokens > 100,000 use higher tier).
- Provenance metadata (`status`, `source_url`, `verified_on`, `note`, `_verified_rates`) is preserved per model.
- Actual billed spend is unavailable. The system computes API-equivalent value.

### 3.5 Privacy, Retention, and Local Execution
- Transcripts contain user messages, code snippets, and tool responses. Legacy `tokentrack.py` drops all content blocks except tool calls.
- The rebuild must maintain this privacy boundary: zero persistence of prompt or completion text unless explicitly opted-in by the user, and all exports must run through synthetic secret redaction.
- Local loopback only: 0 external CDN, telemetry, or cloud calls.

---

## 4. Synthetic Query, Memory, and Refresh Measurement Procedure

To evaluate performance without inventing arbitrary pass/fail thresholds, the test harness and benchmarks must execute against **synthetic fixture datasets** of known scales and capture empirical baseline metrics.

### 4.1 Measurement Protocol

```
+-------------------------------------------------------------+
|               Synthetic Benchmark Execution                 |
+-------------------------------------------------------------+
   |
   +--> 1. Environment Baseline (ru_maxrss, /proc/self/io, GC)
   |
   +--> 2. Ingestion / Refresh Measurement
   |      - Cold Ingestion (empty database -> parse N synthetic files)
   |      - Incremental Replay (re-parse unchanged files -> verify no-op)
   |      - Append Refresh (append M new records -> measure delta ingest)
   |
   +--> 3. Query Benchmark Suite
   |      - Query Q1: Unified Overview (harness aggregate, 30d window)
   |      - Query Q2: Timeline Breakdown (daily buckets, 6 models)
   |      - Query Q3: Model Distribution & Comparison (6 models)
   |      - Query Q4: Session Explorer (search, filter, pagination)
   |      - Query Q5: Speed & Latency Distribution (percentiles)
   |
   +--> 4. Memory & Resource Recording (Resource rusages, GC objects)
   |
   +--> 5. Output Metric Ledger (Mean, Median, p95, Peak RSS, Throughput)
```

### 4.2 Step-by-Step Procedure

1. **Synthetic Workload Setup:**
   - Generate standard synthetic corpus:
     - `Small`: 10 sessions, 100 requests.
     - `Medium`: 100 sessions, 2,500 requests.
     - `Large`: 500 sessions, 25,000 requests.
   - Use strictly synthetic timestamps, tokens, tool names, and project aliases. Zero live data.

2. **Incremental Refresh & Ingestion Measurement:**
   - **Metrics Captured:**
     - Wall time ($t_{\text{wall}}$ in seconds) and CPU time ($t_{\text{cpu}} = \Delta \text{ru\_utime} + \Delta \text{ru\_stime}$).
     - Ingestion throughput: $\text{messages/s} = N_{\text{records}} / t_{\text{wall}}$ and $\text{MiB/s} = \text{Bytes} / (2^{20} \times t_{\text{wall}})$.
     - Block I/O: Bytes read/written via `/proc/self/io` (Linux) or fallback.
     - Parse cache hits, misses, and hit latency ($\mu\text{s}$).
   - **Procedure:**
     - Run `run_ingest_benchmark(dataset, cold=True)` -> Record cold ingestion baseline.
     - Run `run_ingest_benchmark(dataset, cold=False)` -> Record warm parse cache verification.
     - Append 5% new synthetic lines to 10% of files -> Record incremental refresh latency.

3. **Query Latency Measurement:**
   - Execute each of the canonical query envelopes ($Q_1$ through $Q_5$) 20 times in a warm process.
   - Discard the first 2 warmup iterations.
   - Calculate and report:
     - Minimum latency ($\text{ms}$)
     - Median latency ($\text{ms}$)
     - Mean latency ($\text{ms}$)
     - 95th percentile latency (p95, $\text{ms}$)
     - Maximum latency ($\text{ms}$)

4. **Memory Footprint Measurement:**
   - Record `ru_maxrss` before run ($M_0$) and after complete ingestion/query suite ($M_{\text{peak}}$).
     *(Note: `ru_maxrss` is in KiB on Linux, bytes on macOS).*
   - Run explicit `gc.collect()` and track:
     - Net heap delta.
     - Tracked object count delta via `len(gc.get_objects())`.

5. **Reporting Constraint — No Invented Thresholds:**
   - The measurement harness records and compares numbers against the established synthetic baseline.
   - It MUST NOT fail with arbitrary hardcoded thresholds (e.g., "query must take < 5ms" or "memory must be < 50MB"). Instead, it reports the empirical metrics table and flags relative regressions exceeding $2\times$ baseline variance.
