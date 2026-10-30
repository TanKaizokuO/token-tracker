import React, { useState, useMemo } from 'react';

// ============================================================================
// Central Metric Registry & Transparent Definitions (WP08 / Plan Row 53)
// ============================================================================

export interface MetricDefinition {
  metric_id: string;
  name: string;
  family: string;
  category: 'reported' | 'calculated' | 'inferred' | 'financial' | 'timing' | string;
  version: string;
  units: string;
  formula: string;
  denominator: string | null;
  counter_semantics: string;
  cost_basis: string | null;
  timing_basis: string | null;
  description: string;
  provenance: string;
}

export const CENTRAL_METRIC_REGISTRY: Record<string, MetricDefinition> = {};

export const METRIC_DEFINITIONS: MetricDefinition[] = [
  {
    "metric_id": "fresh_input_tokens",
    "name": "Fresh Input Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(fresh_input_tokens)",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Newly processed prompt tokens billed at standard base rate.",
    "provenance": "normalized_usage.fresh_input_tokens"
  },
  {
    "metric_id": "cache_read_tokens",
    "name": "Cache Read Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(cache_read_tokens)",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Prompt tokens retrieved from provider cache at discounted read rates.",
    "provenance": "normalized_usage.cache_read_tokens"
  },
  {
    "metric_id": "cache_write_5m_tokens",
    "name": "Cache Write (5m TTL) Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(cache_write_5m_tokens)",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Prompt tokens written to 5-minute ephemeral cache.",
    "provenance": "normalized_usage.cache_write_5m_tokens"
  },
  {
    "metric_id": "cache_write_1h_tokens",
    "name": "Cache Write (1h TTL) Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(cache_write_1h_tokens)",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Prompt tokens written to 1-hour extended cache.",
    "provenance": "normalized_usage.cache_write_1h_tokens"
  },
  {
    "metric_id": "cache_write_unknown_ttl_tokens",
    "name": "Cache Write (Unknown TTL) Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(cache_write_unknown_ttl_tokens)",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Cache write tokens where TTL was not provided by harness; unpriced.",
    "provenance": "normalized_usage.cache_write_unknown_ttl_tokens"
  },
  {
    "metric_id": "output_tokens",
    "name": "Output Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(output_tokens)",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Model output tokens billed at output rate; includes reasoning subset.",
    "provenance": "normalized_usage.output_tokens"
  },
  {
    "metric_id": "reasoning_tokens",
    "name": "Reasoning / Thinking Tokens",
    "family": "summary",
    "category": "reported",
    "version": "1.0",
    "units": "tokens",
    "formula": "sum(reasoning_tokens)",
    "denominator": "output_tokens",
    "counter_semantics": "subset",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Non-additive reported reasoning subset contained within output_tokens.",
    "provenance": "normalized_usage.reasoning_tokens"
  },
  {
    "metric_id": "total_tokens",
    "name": "Total Disjoint Billable Tokens",
    "family": "summary",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "fresh_input_tokens + cache_read_tokens + cache_write_5m_tokens + cache_write_1h_tokens + cache_write_unknown_ttl_tokens + output_tokens",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Strict sum of six mutually exclusive billable token counters.",
    "provenance": "normalized_usage"
  },
  {
    "metric_id": "requests_count",
    "name": "Canonical Requests Count",
    "family": "summary",
    "category": "calculated",
    "version": "1.0",
    "units": "count",
    "formula": "count(canonical_id)",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Number of canonical deduplicated API requests.",
    "provenance": "canonical_requests.canonical_id"
  },
  {
    "metric_id": "sessions_count",
    "name": "Unique Sessions Count",
    "family": "summary",
    "category": "calculated",
    "version": "1.0",
    "units": "count",
    "formula": "count(distinct session_id)",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Count of distinct root or subagent sessions without double-counting.",
    "provenance": "canonical_requests.session_id"
  },
  {
    "metric_id": "cache_hit_rate",
    "name": "Cache Hit Rate",
    "family": "summary",
    "category": "calculated",
    "version": "1.0",
    "units": "percentage",
    "formula": "cache_read_tokens / (fresh_input_tokens + cache_read_tokens)",
    "denominator": "fresh_input_tokens + cache_read_tokens",
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Proportion of eligible prompt tokens satisfied from cache. Denominator strictly fresh_input + cache_read.",
    "provenance": "normalized_usage"
  },
  {
    "metric_id": "api_equivalent_usd",
    "name": "API Equivalent Cost (USD)",
    "family": "summary",
    "category": "financial",
    "version": "1.0",
    "units": "USD",
    "formula": "sum(api_equivalent_usd)",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": "dated_rate_cards",
    "timing_basis": null,
    "description": "Estimated standard API catalog valuation using effective dated rate schedule.",
    "provenance": "financial_facts.api_equivalent_usd"
  },
  {
    "metric_id": "api_equivalent_inr",
    "name": "API Equivalent Cost (INR)",
    "family": "summary",
    "category": "financial",
    "version": "1.0",
    "units": "INR",
    "formula": "sum(api_equivalent_inr)",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": "dated_rate_cards_and_historical_fx",
    "timing_basis": null,
    "description": "API equivalent cost converted to INR using effective historical FX rate.",
    "provenance": "financial_facts.api_equivalent_inr"
  },
  {
    "metric_id": "actual_billed_amount",
    "name": "Actual Billed Amount",
    "family": "summary",
    "category": "financial",
    "version": "1.0",
    "units": "USD",
    "formula": "sum(actual_billed_amount)",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": "invoice_records",
    "timing_basis": null,
    "description": "Direct measured billed financial amount where verified billing records exist.",
    "provenance": "financial_facts.actual_billed_amount"
  },
  {
    "metric_id": "cache_savings_usd",
    "name": "Cache Savings (USD)",
    "family": "summary",
    "category": "financial",
    "version": "1.0",
    "units": "USD",
    "formula": "sum(cache_savings_usd)",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": "dated_rate_cards",
    "timing_basis": null,
    "description": "Estimated dollar savings from cache read discounts compared to full fresh input rate.",
    "provenance": "financial_facts.cache_savings_usd"
  },
  {
    "metric_id": "generation_speed_tps",
    "name": "Generation Speed (TPS)",
    "family": "summary",
    "category": "timing",
    "version": "1.0",
    "units": "tps",
    "formula": "sum(output_tokens) / (sum(generation_duration_ms) / 1000.0) [on eligible requests]",
    "denominator": "sum(generation_duration_ms) / 1000.0",
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": "measured_generation_excluding_ttft",
    "description": "Average generation throughput across eligible timed requests. Untimed requests are strictly excluded from denominator.",
    "provenance": "canonical_requests.generation_speed_tps"
  },
  {
    "metric_id": "ttft_ms",
    "name": "Time to First Token (TTFT)",
    "family": "summary",
    "category": "timing",
    "version": "1.0",
    "units": "ms",
    "formula": "avg(ttft_ms) [on eligible requests]",
    "denominator": "count(eligible requests with ttft_ms)",
    "counter_semantics": "aggregate",
    "cost_basis": null,
    "timing_basis": "submission_to_first_token",
    "description": "Mean latency to first output chunk. Absent when not instrumented.",
    "provenance": "canonical_requests.ttft_ms"
  },
  {
    "metric_id": "session_duration_ms",
    "name": "Session Duration",
    "family": "sessions",
    "category": "timing",
    "version": "1.0",
    "units": "ms",
    "formula": "avg(duration_ms)",
    "denominator": "count(sessions with duration_ms)",
    "counter_semantics": "aggregate",
    "cost_basis": null,
    "timing_basis": "session_start_to_end",
    "description": "Average total duration of completed sessions.",
    "provenance": "sessions.duration_ms"
  },
  {
    "metric_id": "timeline",
    "name": "Interactive Timeline Aggregation",
    "family": "timeline",
    "category": "calculated",
    "version": "1.0",
    "units": "aggregate",
    "formula": "Grouped by time bucket (hour, day, week, month) in target timezone",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Time-series token consumption and estimated cost grouped into half-open instant buckets.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "models",
    "name": "Model Usage Distribution",
    "family": "models",
    "category": "calculated",
    "version": "1.0",
    "units": "aggregate",
    "formula": "Grouped by normalized_model / canonical display identity",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "measured_generation",
    "description": "Ranked model distribution covering tokens, requests, sessions, and API-equivalent cost.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "projects",
    "name": "Project Usage Explorer",
    "family": "projects",
    "category": "calculated",
    "version": "1.0",
    "units": "aggregate",
    "formula": "Grouped by project_id / project_name",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Project workspace rollup of tokens, cost, requests, and sessions.",
    "provenance": "projects, canonical_requests, normalized_usage"
  },
  {
    "metric_id": "sessions",
    "name": "Session Explorer",
    "family": "sessions",
    "category": "calculated",
    "version": "1.0",
    "units": "table",
    "formula": "Per-session rollup with sort, pagination, and text search",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "session_wall_clock",
    "description": "Searchable, paginated session table with token sums, duration, and model list.",
    "provenance": "sessions, canonical_requests"
  },
  {
    "metric_id": "requests",
    "name": "Individual Request Inspector",
    "family": "requests",
    "category": "reported",
    "version": "1.0",
    "units": "table",
    "formula": "Canonical requests joined with disjoint usage and financial facts",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "request_level_timing",
    "description": "Individual deduplicated canonical API requests with full token breakdowns and timing basis.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "events",
    "name": "Trace Events",
    "family": "events",
    "category": "reported",
    "version": "1.0",
    "units": "table",
    "formula": "Ordered trace events (tool calls, edits, switches, compactions)",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": "event_duration",
    "description": "Execution trace events linked to sessions and canonical requests.",
    "provenance": "events"
  },
  {
    "metric_id": "coverage",
    "name": "Historical Coverage & Completeness",
    "family": "coverage",
    "category": "calculated",
    "version": "1.0",
    "units": "aggregate",
    "formula": "Live requests vs archive partition spans and source scan checkpoints",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Coverage audit distinguishing full request detail from aggregate-only archive history.",
    "provenance": "archive_partitions, source_checkpoints, canonical_requests"
  },
  {
    "metric_id": "tokens",
    "name": "Token Breakdown & Cache Efficiency",
    "family": "tokens",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Disjoint billable counters + reasoning subset + prompt cache efficiency",
    "denominator": "fresh_input_tokens + cache_read_tokens",
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Complete token breakdown, reasoning subset separation, cache hit rates, and economics.",
    "provenance": "normalized_usage, financial_facts"
  },
  {
    "metric_id": "cache",
    "name": "Cache Efficiency Analysis",
    "family": "tokens",
    "category": "calculated",
    "version": "1.0",
    "units": "percentage",
    "formula": "cache_read_tokens / (fresh_input_tokens + cache_read_tokens)",
    "denominator": "fresh_input_tokens + cache_read_tokens",
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Prompt cache hit rates, savings, and per-TTL break-even economics.",
    "provenance": "normalized_usage, financial_facts"
  },
  {
    "metric_id": "speed",
    "name": "Generation Speed & Latency Analysis",
    "family": "speed",
    "category": "timing",
    "version": "1.0",
    "units": "tps",
    "formula": "sum(output_tokens) / (sum(generation_duration_ms) / 1000.0) [eligible requests]",
    "denominator": "sum(generation_duration_ms) / 1000.0",
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": "measured_generation",
    "description": "Deep timing distributions, weighted vs per-request TPS, TTFT, and anomalies.",
    "provenance": "canonical_requests"
  },
  {
    "metric_id": "performance",
    "name": "Performance & Speed Breakdown",
    "family": "speed",
    "category": "timing",
    "version": "1.0",
    "units": "tps",
    "formula": "Multi-dimensional speed and latency slicing across models, routes, and tasks",
    "denominator": "sum(generation_duration_ms) / 1000.0",
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": "measured_generation",
    "description": "Performance distributions, TTFT, queue duration, and anomaly detection.",
    "provenance": "canonical_requests"
  },
  {
    "metric_id": "traces",
    "name": "Trace & Attribution Analysis",
    "family": "traces",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Hierarchical attribution across sessions, subagents, tools, and prompts",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Execution trace attribution, context occupancy, compaction, retry loops, and repetition.",
    "provenance": "canonical_requests, events, sessions"
  },
  {
    "metric_id": "attribution",
    "name": "Attribution Hierarchy",
    "family": "traces",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Exclusive vs inclusive session tree rollups",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Hierarchical session and prompt attribution without double counting.",
    "provenance": "canonical_requests, events, sessions"
  },
  {
    "metric_id": "providers",
    "name": "Provider & Account Breakdown",
    "family": "providers",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Grouped by provider, account_alias, and access_route",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Provider-level usage, spend, route separation, and quota utilization.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "providers_breakdown",
    "name": "Provider Breakdown",
    "family": "providers",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Grouped by provider, account_alias, and access_route",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Provider-level usage, spend, route separation, and quota utilization.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "quotas",
    "name": "Provider Quotas & Limits",
    "family": "quotas",
    "category": "reported",
    "version": "1.0",
    "units": "percentage",
    "formula": "Official subscription quota cards, reset schedules, and limits",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Provider quota tracking without synthetic inference from local tokens.",
    "provenance": "subscription_quotas"
  },
  {
    "metric_id": "provider_quotas",
    "name": "Provider Quota Tracking",
    "family": "quotas",
    "category": "reported",
    "version": "1.0",
    "units": "percentage",
    "formula": "Official subscription quota cards, reset schedules, and limits",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Provider quota tracking without synthetic inference from local tokens.",
    "provenance": "subscription_quotas"
  },
  {
    "metric_id": "burn",
    "name": "Token Burn Rate",
    "family": "burn",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Tokens consumed over declared time intervals (TPM, TPH)",
    "denominator": "time_window_minutes",
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Windowed token burn velocity and high-burn active session alerts.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "token_burn_rate",
    "name": "Token Burn Rate Analysis",
    "family": "burn",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Tokens consumed over declared time intervals (TPM, TPH)",
    "denominator": "time_window_minutes",
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": null,
    "description": "Windowed token burn velocity and high-burn active session alerts.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "live",
    "name": "Live Active Sessions",
    "family": "live",
    "category": "timing",
    "version": "1.0",
    "units": "count",
    "formula": "Active sessions with heuristic liveness and recent activity timestamps",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": "wall_clock",
    "description": "Real-time session monitoring with separated observed activity vs inferred liveness.",
    "provenance": "canonical_requests, sessions"
  },
  {
    "metric_id": "live_sessions",
    "name": "Live Sessions Dashboard",
    "family": "live",
    "category": "timing",
    "version": "1.0",
    "units": "count",
    "formula": "Active sessions with heuristic liveness and recent activity timestamps",
    "denominator": null,
    "counter_semantics": "count",
    "cost_basis": null,
    "timing_basis": "wall_clock",
    "description": "Real-time session monitoring with separated observed activity vs inferred liveness.",
    "provenance": "canonical_requests, sessions"
  },
  {
    "metric_id": "budgets",
    "name": "Usage Budgets & Thresholds",
    "family": "budgets",
    "category": "financial",
    "version": "1.0",
    "units": "USD",
    "formula": "User-configured token and financial thresholds compared with actual consumption",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Local budget evaluations and threshold alerts without remote telemetry.",
    "provenance": "alerts_and_reports, financial_facts, normalized_usage"
  },
  {
    "metric_id": "usage_budgets",
    "name": "Usage Budget Rules",
    "family": "budgets",
    "category": "financial",
    "version": "1.0",
    "units": "USD",
    "formula": "User-configured token and financial thresholds compared with actual consumption",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Local budget evaluations and threshold alerts without remote telemetry.",
    "provenance": "alerts_and_reports, financial_facts, normalized_usage"
  },
  {
    "metric_id": "model_profiles",
    "name": "Model Profiles & Comparison",
    "family": "model_profiles",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Seven canonical display identities, side-by-side comparison, switching timeline, and reliability",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "measured_generation",
    "description": "Deep canonical model profiles, switching timeline, task slices, and reliability.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "outcomes",
    "name": "Task Outcomes & Effectiveness",
    "family": "outcomes",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Tokens, time, and cost per completed task with sample adequacy enforcement",
    "denominator": "count(completed_tasks)",
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "session_wall_clock",
    "description": "Task efficiency scoring, user ratings, completion rates, and sample adequacy abstention.",
    "provenance": "manual_settings_and_outcomes, canonical_requests, financial_facts"
  },
  {
    "metric_id": "task_outcomes",
    "name": "Task Outcome Tracking",
    "family": "outcomes",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Tokens, time, and cost per completed task with sample adequacy enforcement",
    "denominator": "count(completed_tasks)",
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "session_wall_clock",
    "description": "Task efficiency scoring, user ratings, completion rates, and sample adequacy abstention.",
    "provenance": "manual_settings_and_outcomes, canonical_requests, financial_facts"
  },
  {
    "metric_id": "efficiency",
    "name": "Efficiency & Optimization Insights",
    "family": "efficiency",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Cache efficiency scenarios, prompt optimization opportunities, and potential waste detection",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": null,
    "description": "Optimization analysis, cache efficiency scenarios, and potential waste detection.",
    "provenance": "normalized_usage, financial_facts"
  },
  {
    "metric_id": "forecasts",
    "name": "Usage Forecast",
    "family": "forecasts",
    "category": "inferred",
    "version": "1.0",
    "units": "tokens",
    "formula": "Forecasts derived from observed request history",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": "observed_request_timestamp",
    "description": "Projected usage and uncertainty from the selected request history.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "forecast",
    "name": "Usage Forecast",
    "family": "forecasts",
    "category": "inferred",
    "version": "1.0",
    "units": "tokens",
    "formula": "Forecasts derived from observed request history",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": "observed_request_timestamp",
    "description": "Projected usage and uncertainty from the selected request history.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "usage_forecast",
    "name": "Usage Forecast",
    "family": "forecasts",
    "category": "inferred",
    "version": "1.0",
    "units": "tokens",
    "formula": "Forecasts derived from observed request history",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": null,
    "timing_basis": "observed_request_timestamp",
    "description": "Projected usage and uncertainty from the selected request history.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "anomalies",
    "name": "Usage and Speed Anomalies",
    "family": "anomalies",
    "category": "inferred",
    "version": "1.0",
    "units": "count",
    "formula": "Robust outlier detection on observed usage and speed metrics",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": null,
    "timing_basis": "measured_generation",
    "description": "Detected anomalous usage and speed observations with supporting evidence.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "usage_anomalies",
    "name": "Usage Anomalies",
    "family": "anomalies",
    "category": "inferred",
    "version": "1.0",
    "units": "count",
    "formula": "Robust outlier detection on observed usage metrics",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": null,
    "timing_basis": "observed_request_timestamp",
    "description": "Detected anomalous usage observations with supporting evidence.",
    "provenance": "canonical_requests, normalized_usage"
  },
  {
    "metric_id": "speed_anomalies",
    "name": "Speed Anomalies",
    "family": "anomalies",
    "category": "inferred",
    "version": "1.0",
    "units": "count",
    "formula": "Robust outlier detection on measured speed metrics",
    "denominator": null,
    "counter_semantics": "aggregate",
    "cost_basis": null,
    "timing_basis": "measured_generation",
    "description": "Detected anomalous speed observations with supporting evidence.",
    "provenance": "canonical_requests"
  },
  {
    "metric_id": "comparisons",
    "name": "Before-and-After Comparison",
    "family": "comparisons",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Metric deltas between two observed time periods",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "measured_generation",
    "description": "Descriptive comparison of observed usage, cost, speed, and retry metrics.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  },
  {
    "metric_id": "before_after_comparison",
    "name": "Before-and-After Comparison",
    "family": "comparisons",
    "category": "calculated",
    "version": "1.0",
    "units": "tokens",
    "formula": "Metric deltas between two observed time periods",
    "denominator": null,
    "counter_semantics": "disjoint_billable",
    "cost_basis": "effective_dated_rate_cards",
    "timing_basis": "measured_generation",
    "description": "Descriptive comparison of observed usage, cost, speed, and retry metrics.",
    "provenance": "canonical_requests, normalized_usage, financial_facts"
  }
];

for (const def of METRIC_DEFINITIONS) {
  CENTRAL_METRIC_REGISTRY[def.metric_id] = def;
}

export function getMetricDefinition(metricId: string): MetricDefinition | undefined {
  return CENTRAL_METRIC_REGISTRY[metricId];
}

export function getFamilyMetrics(family: string): MetricDefinition[] {
  const lowerFamily = family.toLowerCase();
  const subFamilies = lowerFamily.split('/').map((f) => f.trim());
  return METRIC_DEFINITIONS.filter((def) => {
    const defFamily = def.family.toLowerCase();
    return subFamilies.includes(defFamily) || lowerFamily.includes(defFamily);
  });
}

export const CATEGORY_DESCRIPTIONS: Record<string, string> = {
  reported: 'Reported: Direct measurement or counter provided by API response telemetry.',
  calculated: 'Calculated: Deterministic algebraic combination of canonical counters.',
  inferred: 'Inferred: Statistical estimation or heuristic label; not directly observed.',
  financial: 'Financial: Monetary fact or valuation calculated from effective rate cards or billed receipts.',
  timing: 'Timing: Duration or throughput measure dependent on specific timing basis.',
};

export interface MetricDefinitionBadgeProps {
  category: string;
  className?: string;
}

export const MetricDefinitionBadge: React.FC<MetricDefinitionBadgeProps> = ({ category, className = '' }) => {
  const tooltip = CATEGORY_DESCRIPTIONS[category] || `Category: ${category}`;
  return (
    <span
      className={`metric-category-badge badge-${category} ${className}`}
      title={tooltip}
    >
      {category}
    </span>
  );
};

export interface MetricDefinitionCardProps {
  definition: MetricDefinition;
  compact?: boolean;
  expandedDefault?: boolean;
}

export const MetricDefinitionCard: React.FC<MetricDefinitionCardProps> = ({
  definition,
  compact = false,
  expandedDefault = false,
}) => {
  const [expanded, setExpanded] = useState(expandedDefault);

  return (
    <article className="metric-definition-card" data-metric-id={definition.metric_id}>
      <header className="metric-definition-header">
        <div className="metric-definition-title-row">
          <h4 className="metric-definition-name">{definition.name}</h4>
          <div className="metric-definition-badges">
            <MetricDefinitionBadge category={definition.category} />
            <span className="metric-family-badge">{definition.family}</span>
            <span className="metric-unit-badge">{definition.units}</span>
          </div>
        </div>
        <div className="metric-definition-meta">
          <code className="metric-definition-id">{definition.metric_id}</code>
          <span className="metric-version">v{definition.version}</span>
        </div>
      </header>

      <p className="metric-definition-description">{definition.description}</p>

      {(!compact || expanded) && (
        <div className="metric-definition-details">
          <div className="metric-detail-row">
            <span className="metric-detail-label">Formula:</span>
            <code className="metric-detail-code">{definition.formula}</code>
          </div>

          {definition.denominator && (
            <div className="metric-detail-row">
              <span className="metric-detail-label">Denominator:</span>
              <span className="metric-detail-val">{definition.denominator}</span>
            </div>
          )}

          <div className="metric-detail-row">
            <span className="metric-detail-label">Semantics:</span>
            <span className="metric-detail-val">{definition.counter_semantics.replace(/_/g, ' ')}</span>
          </div>

          {definition.cost_basis && (
            <div className="metric-detail-row">
              <span className="metric-detail-label">Cost Basis:</span>
              <span className="metric-detail-val">{definition.cost_basis.replace(/_/g, ' ')}</span>
            </div>
          )}

          {definition.timing_basis && (
            <div className="metric-detail-row">
              <span className="metric-detail-label">Timing Basis:</span>
              <span className="metric-detail-val">{definition.timing_basis.replace(/_/g, ' ')}</span>
            </div>
          )}

          <div className="metric-detail-row">
            <span className="metric-detail-label">Provenance:</span>
            <code className="metric-detail-code">{definition.provenance}</code>
          </div>
        </div>
      )}

      {compact && (
        <button
          type="button"
          className="metric-toggle-btn"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          {expanded ? 'Show less' : 'View formula & basis'}
        </button>
      )}
    </article>
  );
};

export interface MetricDefinitionsPanelProps {
  activeFamily?: string;
  pageTitle?: string;
  onClose?: () => void;
  className?: string;
}

export const MetricDefinitionsPanel: React.FC<MetricDefinitionsPanelProps> = ({
  activeFamily,
  pageTitle,
  onClose,
  className = '',
}) => {
  const [selectedFamily, setSelectedFamily] = useState<string>(activeFamily || 'all');
  const [selectedCategory, setSelectedCategory] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');

  React.useEffect(() => {
    if (activeFamily) {
      setSelectedFamily(activeFamily);
    }
  }, [activeFamily]);

  const families = useMemo(() => {
    const set = new Set<string>();
    for (const def of METRIC_DEFINITIONS) {
      set.add(def.family);
    }
    return Array.from(set).sort();
  }, []);

  const categories = ['all', 'reported', 'calculated', 'inferred', 'financial', 'timing'];

  const filteredMetrics = useMemo(() => {
    return METRIC_DEFINITIONS.filter((def) => {
      if (selectedFamily !== 'all') {
        const targetFamilies = selectedFamily.toLowerCase().split('/').map((f) => f.trim());
        const matchFamily = targetFamilies.some(
          (tf) => def.family.toLowerCase() === tf || tf.includes(def.family.toLowerCase())
        );
        if (!matchFamily) return false;
      }
      if (selectedCategory !== 'all' && def.category !== selectedCategory) {
        return false;
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchId = def.metric_id.toLowerCase().includes(q);
        const matchName = def.name.toLowerCase().includes(q);
        const matchDesc = def.description.toLowerCase().includes(q);
        const matchFormula = def.formula.toLowerCase().includes(q);
        if (!matchId && !matchName && !matchDesc && !matchFormula) return false;
      }
      return true;
    });
  }, [selectedFamily, selectedCategory, searchQuery]);

  return (
    <aside
      className={`metric-definitions-panel ${className}`}
      aria-label="Central Metric Registry Definitions"
    >
      <div className="metric-definitions-header">
        <div className="metric-definitions-title-wrap">
          <div className="metric-registry-indicator" aria-hidden="true" />
          <h3 className="metric-definitions-title">Metric Definitions</h3>
          <span className="metric-definitions-count">
            {filteredMetrics.length} / {METRIC_DEFINITIONS.length}
          </span>
        </div>
        {onClose && (
          <button
            type="button"
            className="btn-secondary metric-definitions-close"
            onClick={onClose}
            aria-label="Close metric definitions"
            title="Close definitions panel"
          >
            ✕
          </button>
        )}
      </div>

      <div className="metric-definitions-controls">
        <div className="metric-search-wrap">
          <input
            type="text"
            className="metric-search-input"
            placeholder="Search metric name, ID, formula…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {searchQuery && (
            <button
              type="button"
              className="metric-search-clear"
              onClick={() => setSearchQuery('')}
              aria-label="Clear search"
            >
              ✕
            </button>
          )}
        </div>

        <div className="metric-filter-row">
          <div className="metric-filter-group">
            <label htmlFor="metric-family-filter">Family:</label>
            <select
              id="metric-family-filter"
              value={selectedFamily}
              onChange={(e) => setSelectedFamily(e.target.value)}
            >
              <option value="all">All Families</option>
              {families.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </div>

          <div className="metric-category-chips" role="radiogroup" aria-label="Filter by category">
            {categories.map((cat) => (
              <button
                key={cat}
                type="button"
                role="radio"
                aria-checked={selectedCategory === cat}
                className={`category-chip ${selectedCategory === cat ? 'active' : ''} ${
                  cat !== 'all' ? `chip-${cat}` : ''
                }`}
                onClick={() => setSelectedCategory(cat)}
              >
                {cat}
              </button>
            ))}
          </div>
        </div>

        {activeFamily && selectedFamily === activeFamily && (
          <div className="metric-context-hint">
            <span>Showing definitions for <strong>{pageTitle || activeFamily}</strong></span>
            <button
              type="button"
              className="metric-reset-filter-link"
              onClick={() => setSelectedFamily('all')}
            >
              Show all
            </button>
          </div>
        )}
      </div>

      <div className="metric-definitions-list" role="feed" aria-busy="false">
        {filteredMetrics.length === 0 ? (
          <div className="metric-definitions-empty">
            <p>No metric definitions match your search and filter criteria.</p>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setSelectedFamily('all');
                setSelectedCategory('all');
                setSearchQuery('');
              }}
            >
              Reset Filters
            </button>
          </div>
        ) : (
          filteredMetrics.map((def) => (
            <MetricDefinitionCard key={def.metric_id} definition={def} compact />
          ))
        )}
      </div>
    </aside>
  );
};

export default MetricDefinitionsPanel;
