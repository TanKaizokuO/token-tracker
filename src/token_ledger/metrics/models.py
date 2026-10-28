"""Model profiles, comparisons, task slices, switching timelines, and reliability analytics.

Implements WP13a/b and Rebuild Plan Rows 13, 15, 16, 17, 18, 19:
- Feature 15: Individual Model Analytics Pages (full all-six canonical display identities reachable,
  per-identity requests, tokens, output/reasoning subset, cache, cost, timing, project/session trends,
  deep TTFT/p90/p95 from eligible distributions, sample counts and missingness).
- Feature 16: Side-By-Side Model Comparison (compare >= 2 models under identical filters,
  tokens/request, median output, TPS, median TTFT, retry/error rate, cost/request, outcome rating;
  sparse / unmatched comparisons explicitly labeled with no effectiveness claim).
- Feature 17: Model Switching Timeline (ordered requests within sessions, links switch events to
  requests, explicitly labels unknown timing or cost, descriptive summaries without causal claims).
- Feature 18: Model Performance by Task Type & Output Verbosity (reliable manual labels only,
  effort setting explicitly labeled missing when not recorded, reasoning subset preserved,
  no quality equivalence claims).
- Feature 19: Model Reliability and Failure Analysis (error, interruption, rate limit, timeout rates
  with eligible denominators and missing-status caveats, distinct retry token accounting).
"""

from __future__ import annotations

from collections import defaultdict
import dataclasses
import datetime
import functools
import math
import sqlite3
import statistics
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from token_ledger.pricing import (
    CANONICAL_DISPLAY_IDENTITIES,
    GEMINI_3_8_FLASH,
    HAIKU_5_5,
    LUNA_MAX,
    OPUS_5_5,
    SOL_6_1,
    SONNET_5_5,
    get_default_model_mapping_rules,
)
from token_ledger.query import filtered_request_rows, validate_query

PERCENTILE_METHOD = "nearest_rank"
SPARSE_SAMPLE_THRESHOLD = 5


_PERCENTILE_FRACTIONS = {"p50": 0.50, "p90": 0.90, "p95": 0.95, "p99": 0.99}


def _nearest_rank(sorted_vals: Sequence[float], p: float) -> float:
    """Nearest-rank value at fraction p of non-empty ascending sorted_vals."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = max(0, min(n - 1, math.ceil(p * n) - 1))
    return sorted_vals[idx]


def _exact_mean(values: Sequence[float]) -> float:
    """Returns the same value as statistics.mean(values), with an exact integer fast path."""
    if all(type(v) is int for v in values):
        total = sum(values)
        quotient, remainder = divmod(total, len(values))
        return quotient if remainder == 0 else total / len(values)
    return statistics.mean(values)


def compute_percentiles(
    values: Sequence[float],
    method: str = PERCENTILE_METHOD,
) -> Dict[str, Optional[float]]:
    """Calculates deterministic summary percentiles using nearest-rank method.

    nearest-rank index: max(0, min(N - 1, ceil(P * N) - 1))
    """
    if not values:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "method": method,
        }

    sorted_vals = sorted(values)
    return {
        "count": len(sorted_vals),
        "mean": round(_exact_mean(sorted_vals), 4),
        "min": round(sorted_vals[0], 4),
        "max": round(sorted_vals[-1], 4),
        "p50": round(_nearest_rank(sorted_vals, 0.50), 4),
        "p90": round(_nearest_rank(sorted_vals, 0.90), 4),
        "p95": round(_nearest_rank(sorted_vals, 0.95), 4),
        "p99": round(_nearest_rank(sorted_vals, 0.99), 4),
        "method": method,
    }


def _distribution_value(values: Sequence[float], operation: str) -> Optional[float]:
    """Returns compute_percentiles(values)[operation] without computing the other statistics."""
    if not values:
        return None
    sorted_vals = sorted(values)
    if operation == "mean":
        return round(_exact_mean(sorted_vals), 4)
    return round(_nearest_rank(sorted_vals, _PERCENTILE_FRACTIONS[operation]), 4)


@functools.lru_cache(maxsize=None)
def _mapping_identities() -> Dict[str, Tuple[str, str]]:
    """Maps each default display identity to (canonical_model_id, provider) of its first mapping rule."""
    identities: Dict[str, Tuple[str, str]] = {}
    for rule in get_default_model_mapping_rules():
        identities.setdefault(
            rule.display_name,
            (rule.canonical_model_id or rule.display_name, rule.provider or "unknown"),
        )
    return identities


@dataclasses.dataclass
class _ProfileTotals:
    """Profile-level running totals for one identity during the request pass.

    Folded into the profile dict once after the pass. Float sums accumulate in request order,
    so results match in-place accumulation exactly.
    """

    requests: int = 0
    raw_models: List[str] = dataclasses.field(default_factory=list)
    total_tokens: int = 0
    fresh_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_write_unknown_ttl_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    eligible_prompt_tokens: int = 0
    cache_savings_usd: float = 0.0
    api_equivalent_usd: Optional[float] = None
    api_equivalent_inr: Optional[float] = None
    actual_billed_amount: Optional[float] = None
    priced_requests: int = 0
    unpriced_requests: int = 0
    missing_pricing: int = 0
    timed_requests: int = 0
    untimed_requests: int = 0
    missing_timing: int = 0
    generation_eligible: int = 0
    sum_generation_seconds: float = 0.0
    ttft_eligible: int = 0
    missing_ttft: int = 0
    missing_status: int = 0


def _fold_totals(prof: Dict[str, Any], totals: _ProfileTotals) -> None:
    """Writes raw profile-level sums into an existing profile dict; finalize rounds and derives ratios."""
    prof["requests_count"] = totals.requests
    prof["raw_models_observed"] = totals.raw_models
    tokens = prof["tokens"]
    tokens["total_tokens"] = totals.total_tokens
    tokens["fresh_input_tokens"] = totals.fresh_input_tokens
    tokens["cache_read_tokens"] = totals.cache_read_tokens
    tokens["cache_write_5m_tokens"] = totals.cache_write_5m_tokens
    tokens["cache_write_1h_tokens"] = totals.cache_write_1h_tokens
    tokens["cache_write_unknown_ttl_tokens"] = totals.cache_write_unknown_ttl_tokens
    tokens["output_tokens"] = totals.output_tokens
    tokens["reasoning_tokens"] = totals.reasoning_tokens
    cache = prof["cache"]
    cache["eligible_prompt_tokens"] = totals.eligible_prompt_tokens
    cache["cache_read_tokens"] = totals.cache_read_tokens
    cache["cache_savings_usd"] = totals.cache_savings_usd
    cost = prof["cost"]
    cost["api_equivalent_usd"] = totals.api_equivalent_usd
    cost["api_equivalent_inr"] = totals.api_equivalent_inr
    cost["actual_billed_amount"] = totals.actual_billed_amount
    cost["priced_requests_count"] = totals.priced_requests
    cost["unpriced_requests_count"] = totals.unpriced_requests
    timing = prof["timing"]
    timing["eligible_timing_requests_count"] = totals.timed_requests
    timing["unobserved_timing_requests_count"] = totals.untimed_requests
    timing["sum_generation_seconds"] = totals.sum_generation_seconds
    prof["duration"]["eligible_duration_count"] = totals.timed_requests
    prof["generation_duration"]["eligible_gen_duration_count"] = totals.generation_eligible
    prof["deep_ttft"]["eligible_ttft_count"] = totals.ttft_eligible
    missingness = prof["missingness"]
    missingness["missing_pricing_count"] = totals.missing_pricing
    missingness["missing_timing_count"] = totals.missing_timing
    missingness["missing_ttft_count"] = totals.missing_ttft
    missingness["missing_status_count"] = totals.missing_status


def _empty_model_profile(display_name: str) -> Dict[str, Any]:
    """Returns an initialized empty profile structure for a canonical display identity."""
    # Canonical model ID and provider come from the first default mapping rule for this identity.
    canonical_id, provider = _mapping_identities().get(display_name, ("unknown", "unknown"))

    return {
        "display_name": display_name,
        "canonical_model_id": canonical_id,
        "provider": provider,
        "is_canonical": True,
        "raw_models_observed": [],
        "requests_count": 0,
        "sessions_count": 0,
        "tokens": {
            "total_tokens": 0,
            "fresh_input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "cache_write_unknown_ttl_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "reasoning_pct_of_output": None,
            "output_distribution": compute_percentiles([]),
            "output_missing_count": 0,
            "output_observed_count": 0,
        },
        "cache": {
            "eligible_prompt_tokens": 0,
            "cache_read_tokens": 0,
            "cache_hit_rate": None,
            "cache_savings_usd": 0.0,
        },
        "cost": {
            "api_equivalent_usd": None,
            "api_equivalent_inr": None,
            "actual_billed_amount": None,
            "priced_requests_count": 0,
            "unpriced_requests_count": 0,
            "cost_per_request_usd": None,
        },
        "timing": {
            "eligible_timing_requests_count": 0,
            "unobserved_timing_requests_count": 0,
            "sum_generation_seconds": 0.0,
            "weighted_tps": None,
            "tps_distribution": compute_percentiles([]),
        },
        "deep_ttft": {
            "eligible_ttft_count": 0,
            "distribution_ms": compute_percentiles([]),
        },
        "duration": {
            "eligible_duration_count": 0,
            "distribution_ms": compute_percentiles([]),
        },
        "generation_duration": {
            "eligible_gen_duration_count": 0,
            "distribution_ms": compute_percentiles([]),
        },
        "reliability": {
            "total_requests": 0,
            "known_status_requests": 0,
            "missing_status_requests": 0,
            "ok_count": 0,
            "error_count": 0,
            "interrupted_count": 0,
            "rate_limited_count": 0,
            "timeout_count": 0,
            "abnormal_termination_count": 0,
            "error_rate": None,
            "retry_requests_count": 0,
            "retry_tokens": 0,
            "missing_status_caveat": None,
        },
        "project_mix": [],
        "session_trends": [],
        "session_mix": [],
        "sample_counts": {
            "requests": 0,
            "sessions": 0,
            "priced_requests": 0,
            "unpriced_requests": 0,
            "timed_requests": 0,
            "untimed_requests": 0,
            "ttft_samples": 0,
        },
        "missingness": {
            "missing_pricing_count": 0,
            "missing_timing_count": 0,
            "missing_ttft_count": 0,
            "missing_status_count": 0,
        },
    }


def _fetch_manual_outcomes(
    conn: sqlite3.Connection,
    canonical_ids: Sequence[str],
    session_ids: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """Fetches verified manual task labels and ratings from manual_settings_and_outcomes.

    Returns dict mapping entity_id to outcome record.
    Strictly flags is_inferred so only reliable manual labels are treated as ground truth.
    """
    if not canonical_ids and not session_ids:
        return {}

    cur = conn.cursor()
    outcomes: Dict[str, Dict[str, Any]] = {}

    # Query for requests
    if canonical_ids:
        placeholders = ",".join("?" for _ in canonical_ids)
        cur.execute(
            f"""
            SELECT entity_id, task_label, completion_rating, manual_rating, is_inferred,
                   inference_method, git_commit_link, notes
            FROM manual_settings_and_outcomes
            WHERE entity_type = 'request' AND entity_id IN ({placeholders})
            """,
            list(canonical_ids),
        )
        for row in cur.fetchall():
            outcomes[row[0]] = {
                "entity_type": "request",
                "task_label": row[1],
                "completion_rating": row[2],
                "manual_rating": row[3],
                "is_inferred": bool(row[4]),
                "inference_method": row[5],
                "git_commit_link": row[6],
                "notes": row[7],
            }

    # Query for sessions
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        cur.execute(
            f"""
            SELECT entity_id, task_label, completion_rating, manual_rating, is_inferred,
                   inference_method, git_commit_link, notes
            FROM manual_settings_and_outcomes
            WHERE entity_type = 'session' AND entity_id IN ({placeholders})
            """,
            list(session_ids),
        )
        for row in cur.fetchall():
            # Session outcome stored if request outcome not already present
            if row[0] not in outcomes:
                outcomes[row[0]] = {
                    "entity_type": "session",
                    "task_label": row[1],
                    "completion_rating": row[2],
                    "manual_rating": row[3],
                    "is_inferred": bool(row[4]),
                    "inference_method": row[5],
                    "git_commit_link": row[6],
                    "notes": row[7],
                }

    return outcomes


def _comparison_metrics(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize one same-filter cohort with explicit per-metric eligible bases."""
    metrics = {}
    for name, field, operation, basis in (
        ("tokens_per_request", "total", "mean", "requests with observed token counters"),
        ("median_output", "output", "p50", "requests with observed output tokens"),
        ("median_tps", "tps", "p50", "requests with generation timing and output"),
        ("median_ttft_ms", "ttft", "p50", "requests with reported TTFT"),
        ("p90_ttft_ms", "ttft", "p90", "requests with reported TTFT"),
        ("p95_ttft_ms", "ttft", "p95", "requests with reported TTFT"),
        ("median_duration_ms", "duration", "p50", "requests with observed duration"),
        ("p90_duration_ms", "duration", "p90", "requests with observed duration"),
        ("p95_duration_ms", "duration", "p95", "requests with observed duration"),
        ("retry_error_rate", "failure", "mean", "requests with known status"),
        ("retry_rate", "retry", "mean", "requests with retry metadata"),
        ("cost_per_request_usd", "cost", "mean", "priced requests"),
        ("outcome_rating", "rating", "mean", "distinct manually rated request/session outcomes"),
    ):
        eligible = [s[field] for s in samples if s[field] is not None]
        if field == "rating":
            eligible = list({s["outcome_id"]: s[field] for s in samples if s[field] is not None}.values())
        metrics[name] = {
            "value": _distribution_value(eligible, operation),
            "sample_count": len(eligible),
            "denominator": len(eligible),
            "basis": basis,
            "cohort_requests": len(samples),
            "missing_count": len(samples) - len(eligible),
        }
    cache_samples = [s for s in samples if s["prompt"] is not None]
    prompt = sum(s["prompt"] for s in cache_samples)
    metrics["cache_hit_rate"] = {
        "value": sum(s["cache_read"] for s in cache_samples) / prompt if prompt else None,
        "sample_count": len(cache_samples), "denominator": prompt,
        "basis": "observed fresh input + cache-read tokens", "cohort_requests": len(samples),
        "missing_count": len(samples) - len(cache_samples),
    }
    return metrics


def analyze(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Computes comprehensive model profiles, comparisons, task slices, switching, and reliability.

    Args:
        conn: SQLite connection with Token Ledger schema.
        envelope: Query envelope conforming to query contract.

    Returns:
        Structured response dictionary matching local://token-ledger-model-contract.md.
    """
    norm_query = envelope if "family" in envelope else validate_query(envelope)

    # 1. Fetch filtered canonical rows via shared query engine
    raw_rows = filtered_request_rows(conn, norm_query)

    canonical_ids = [r["canonical_id"] for r in raw_rows]
    session_ids = list({r["session_id"] for r in raw_rows if r.get("session_id")})

    # Fetch manual outcomes; retry and parent lineage are read from the selected rows.
    outcomes = _fetch_manual_outcomes(conn, canonical_ids, session_ids)

    # 2. Initialize all six canonical profiles
    profiles: Dict[str, Dict[str, Any]] = {
        name: _empty_model_profile(name) for name in CANONICAL_DISPLAY_IDENTITIES
    }
    unmatched_profiles: Dict[str, Dict[str, Any]] = {}
    # Profile-level running totals, folded into the profile dicts after the request pass.
    canonical_totals: Dict[str, _ProfileTotals] = {name: _ProfileTotals() for name in profiles}
    unmatched_totals: Dict[str, _ProfileTotals] = {}

    # Aggregation buckets
    model_output_samples: Dict[str, List[float]] = defaultdict(list)
    model_tps_samples: Dict[str, List[float]] = defaultdict(list)
    model_ttft_samples: Dict[str, List[float]] = defaultdict(list)
    model_duration_samples: Dict[str, List[float]] = defaultdict(list)
    model_gen_duration_samples: Dict[str, List[float]] = defaultdict(list)
    model_sessions: Dict[str, Set[str]] = defaultdict(set)
    model_project_stats: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {
        "requests_count": 0, "total_tokens": 0, "cost_usd": 0.0,
    }))
    model_day_stats: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {
        "requests_count": 0, "total_tokens": 0, "cost_usd": 0.0,
        "output_tokens": 0, "cache_reads": 0, "fresh_inputs": 0,
        "sessions": set(), "durations": [],
    }))
    model_session_stats: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(lambda: defaultdict(
        lambda: {"requests_count": 0, "total_tokens": 0}))
    comparison_samples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    matched_samples: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    financial_coverage: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # Reliability tracking
    reliability_data: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "ok": 0, "error": 0, "interrupted": 0, "rate_limited": 0,
        "timeout": 0, "abnormal_termination": 0, "unknown": 0,
        "retry_requests": 0, "retry_tokens": 0,
    })

    # Task slices
    task_groups: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "requests_count": 0, "total_tokens": 0, "output_tokens": 0,
        "fresh_input_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0,
        "durations_ms": [], "tps_samples": [], "ttft_samples": [], "output_samples": [],
        "retries_count": 0, "effort_settings": set(),
        "manual_ratings": [], "is_manual": True,
    })

    # Switching timeline ordered by session and timestamp
    session_ordered_requests: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    total_requests_observed = len(raw_rows)
    total_tokens_observed = 0
    total_cost_observed = 0.0
    unresolved_overlap_count = 0

    # 3. Process each canonical row
    for row in raw_rows:
        raw_model = row["raw_model"] or ""
        norm_model = row["normalized_model"] or ""
        provider = row["provider"] or ""
        cid = row["canonical_id"] or ""
        sess_id = row["session_id"] or "unknown_session"
        proj_id = row["project_id"] or "unassigned"
        ts = row["request_timestamp"] or ""
        status = (row["request_status"] or "unknown").strip().lower()

        if row["is_unresolved_overlap"]:
            unresolved_overlap_count += 1

        # The shared selector already resolved versioned identity for this request.
        display_name = row["display_name"]

        if display_name and display_name in profiles:
            totals = canonical_totals[display_name]
            target_key = display_name
        else:
            # Unmatched or custom model
            target_key = display_name or norm_model or raw_model or "unknown"
            if target_key not in unmatched_profiles:
                unmatched_profiles[target_key] = _empty_model_profile(target_key)
                unmatched_profiles[target_key]["is_canonical"] = False
                unmatched_profiles[target_key]["canonical_model_id"] = norm_model or raw_model or "unknown"
                unmatched_profiles[target_key]["provider"] = provider
                unmatched_totals[target_key] = _ProfileTotals()
            totals = unmatched_totals[target_key]

        if raw_model and raw_model not in totals.raw_models:
            totals.raw_models.append(raw_model)

        totals.requests += 1
        model_sessions[target_key].add(sess_id)
        session_stats = model_session_stats[target_key][sess_id]
        session_stats["requests_count"] += 1

        # Disjoint token counters (raw values kept for the presence checks below)
        raw_fresh = row["fresh_input_tokens"]
        raw_cread = row["cache_read_tokens"]
        raw_total = row["total_tokens"]
        output_observed = row["output_tokens"]
        fresh = raw_fresh or 0
        cread = raw_cread or 0
        cw5m = row["cache_write_5m_tokens"] or 0
        cw1h = row["cache_write_1h_tokens"] or 0
        cwunk = row["cache_write_unknown_ttl_tokens"] or 0
        output = output_observed or 0
        reasoning = row["reasoning_tokens"] or 0
        req_total = raw_total or (fresh + cread + cw5m + cw1h + cwunk + output)

        total_tokens_observed += req_total
        session_stats["total_tokens"] += req_total
        totals.total_tokens += req_total
        totals.fresh_input_tokens += fresh
        totals.cache_read_tokens += cread
        totals.cache_write_5m_tokens += cw5m
        totals.cache_write_1h_tokens += cw1h
        totals.cache_write_unknown_ttl_tokens += cwunk
        totals.output_tokens += output
        totals.reasoning_tokens += reasoning
        if output_observed is not None:
            model_output_samples[target_key].append(float(output_observed))

        # Cache counters
        totals.eligible_prompt_tokens += fresh + cread
        totals.cache_savings_usd += row["cache_savings_usd"] or 0.0

        # Financial facts
        cost_usd = row["api_equivalent_usd"]
        cost_inr_observed = row["api_equivalent_inr"]
        cost_inr = cost_inr_observed or 0.0
        billed_observed = row["actual_billed_amount"]
        billed = billed_observed or 0.0
        pricing_status = row["pricing_status"] or "unpriced"

        if pricing_status == "priced" and cost_usd is not None:
            if totals.api_equivalent_usd is None:
                totals.api_equivalent_usd = 0.0
            totals.api_equivalent_usd += cost_usd
            if cost_inr_observed is not None:
                if totals.api_equivalent_inr is None:
                    totals.api_equivalent_inr = 0.0
                totals.api_equivalent_inr += cost_inr
                financial_coverage[target_key]["inr"] += 1
            totals.priced_requests += 1
            total_cost_observed += cost_usd
        else:
            totals.unpriced_requests += 1
            totals.missing_pricing += 1

        if billed_observed is not None:
            if totals.actual_billed_amount is None:
                totals.actual_billed_amount = 0.0
            totals.actual_billed_amount += float(billed)
            financial_coverage[target_key]["billed"] += 1

        # Timing & Speed
        t_basis = row["timing_basis"] or "none"
        dur_ms = row["duration_ms"]
        ttft_ms = row["ttft_ms"]
        gen_dur_ms = row["generation_duration_ms"]
        tps = row["generation_speed_tps"]

        if t_basis != "none" and dur_ms is not None and dur_ms > 0:
            totals.timed_requests += 1
            model_duration_samples[target_key].append(float(dur_ms))
        else:
            totals.untimed_requests += 1
            totals.missing_timing += 1

        if gen_dur_ms is not None and gen_dur_ms > 0:
            totals.generation_eligible += 1
            model_gen_duration_samples[target_key].append(float(gen_dur_ms))
            gen_sec = gen_dur_ms / 1000.0
            totals.sum_generation_seconds += gen_sec
            if tps is not None:
                model_tps_samples[target_key].append(float(tps))
            else:
                model_tps_samples[target_key].append(float(output) / gen_sec)

        if ttft_ms is not None and ttft_ms > 0:
            totals.ttft_eligible += 1
            model_ttft_samples[target_key].append(float(ttft_ms))
        else:
            totals.missing_ttft += 1

        # Reliability & Retries (retry and parent lineage are projected on the selected row)
        rel = reliability_data[target_key]
        is_retry = False
        if (row["retry_attempt"] or 0) > 0 or ":retry_" in cid or row["parent_request_id"]:
            is_retry = True
            rel["retry_requests"] += 1
            rel["retry_tokens"] += req_total

        if status in ("ok", "success", "completed"):
            rel["ok"] += 1
        elif "rate" in status or status == "429":
            rel["rate_limited"] += 1
        elif "timeout" in status or status in ("504", "408"):
            rel["timeout"] += 1
        elif "interrupt" in status:
            rel["interrupted"] += 1
        elif "error" in status or status in ("failed", "500", "502", "503"):
            rel["error"] += 1
        elif "abnormal" in status:
            rel["abnormal_termination"] += 1
        else:
            rel["unknown"] += 1
            totals.missing_status += 1

        # Project and daily mix
        pstats = model_project_stats[target_key][proj_id]
        pstats["requests_count"] += 1
        pstats["total_tokens"] += req_total
        pstats["priced_requests_count"] = pstats.get("priced_requests_count", 0) + int(cost_usd is not None)
        if cost_usd:
            pstats["cost_usd"] += cost_usd

        day_str = ts[:10] if len(ts) >= 10 else "unknown_date"
        dstats = model_day_stats[target_key][day_str]
        dstats["requests_count"] += 1
        dstats["total_tokens"] += req_total
        dstats["priced_requests_count"] = dstats.get("priced_requests_count", 0) + int(cost_usd is not None)
        if cost_usd:
            dstats["cost_usd"] += cost_usd
        dstats["output_tokens"] += output
        dstats["cache_reads"] += cread
        dstats["fresh_inputs"] += fresh
        dstats["sessions"].add(sess_id)
        if dur_ms is not None and dur_ms > 0:
            dstats["durations"].append(float(dur_ms))

        # Outcomes / Task Label tracking
        outcome = outcomes.get(cid) or outcomes.get(sess_id)
        if outcome and outcome.get("task_label"):
            task_name = outcome["task_label"].strip()
            is_manual_label = not outcome.get("is_inferred", False)
        else:
            task_name = "unlabeled"
            is_manual_label = True

        # Effort tier extraction (from raw_model or payload)
        raw_lower = raw_model.lower()
        if "-low" in raw_lower:
            effort = "low"
        elif "-medium" in raw_lower:
            effort = "medium"
        elif "-high" in raw_lower:
            effort = "high"
        elif "-max" in raw_lower:
            effort = "max"
        else:
            effort = "missing"

        tgroup = task_groups[(target_key, task_name)]
        tgroup["requests_count"] += 1
        tgroup["total_tokens"] += req_total
        tgroup["output_tokens"] += output
        if output_observed is not None:
            tgroup["output_samples"].append(float(output))
        tgroup["fresh_input_tokens"] += fresh
        tgroup["reasoning_tokens"] += reasoning
        if cost_usd:
            tgroup["cost_usd"] += cost_usd
        if dur_ms:
            tgroup["durations_ms"].append(float(dur_ms))
        if tps is not None:
            tgroup["tps_samples"].append(float(tps))
        if ttft_ms is not None:
            tgroup["ttft_samples"].append(float(ttft_ms))
        if is_retry:
            tgroup["retries_count"] += 1
        tgroup["effort_settings"].add(effort)
        if outcome and outcome.get("manual_rating") is not None:
            tgroup["manual_ratings"].append(float(outcome["manual_rating"]))
        if not is_manual_label:
            tgroup["is_manual"] = False
        known_status = status in ("ok", "success", "completed", "429", "504", "408", "failed", "500", "502", "503") or any(
            part in status for part in ("rate", "timeout", "interrupt", "error", "abnormal"))
        sample = {
            "total": req_total if raw_total is not None else None,
            "output": output_observed,
            "tps": float(output) / (gen_dur_ms / 1000) if gen_dur_ms and gen_dur_ms > 0 and output_observed is not None else None,
            "ttft": ttft_ms if ttft_ms is not None and ttft_ms >= 0 else None,
            "duration": dur_ms if t_basis != "none" and dur_ms is not None and dur_ms >= 0 else None,
            "failure": int(status not in ("ok", "success", "completed")) if known_status else None,
            "retry": int(is_retry), "cost": cost_usd if pricing_status == "priced" else None,
            "rating": outcome.get("manual_rating") if outcome and is_manual_label else None,
            "outcome_id": cid if outcome and outcome.get("entity_type") == "request" else sess_id,
            "prompt": fresh + cread if raw_fresh is not None and raw_cread is not None else None,
            "cache_read": cread,
        }
        comparison_samples[target_key].append(sample)
        if is_manual_label and task_name != "unlabeled":
            matched_samples[task_name][target_key].append(sample)

        # Switching timeline ordered insertion
        session_ordered_requests[sess_id].append({
            "canonical_id": cid,
            "request_timestamp": ts,
            "display_name": target_key,
            "total_tokens": req_total,
            "cost_usd": cost_usd,
            "duration_ms": dur_ms,
            "request_status": status,
        })

    for name, totals in canonical_totals.items():
        _fold_totals(profiles[name], totals)
    for name, totals in unmatched_totals.items():
        _fold_totals(unmatched_profiles[name], totals)

    # 4. Finalize distributions and ratios for each profile
    all_profiles: Dict[str, Dict[str, Any]] = {**profiles, **unmatched_profiles}
    for key, prof in all_profiles.items():
        prof["sessions_count"] = len(model_sessions[key])
        prof["sample_counts"]["requests"] = prof["requests_count"]
        prof["sample_counts"]["sessions"] = prof["sessions_count"]
        prof["sample_counts"]["priced_requests"] = prof["cost"]["priced_requests_count"]
        prof["sample_counts"]["unpriced_requests"] = prof["cost"]["unpriced_requests_count"]
        prof["sample_counts"]["timed_requests"] = prof["timing"]["eligible_timing_requests_count"]
        prof["sample_counts"]["untimed_requests"] = prof["timing"]["unobserved_timing_requests_count"]
        prof["sample_counts"]["ttft_samples"] = prof["deep_ttft"]["eligible_ttft_count"]

        # Cache hit rate
        prompt_den = prof["cache"]["eligible_prompt_tokens"]
        if prompt_den > 0:
            prof["cache"]["cache_hit_rate"] = round(prof["cache"]["cache_read_tokens"] / prompt_den, 4)
        prof["cache"]["cache_savings_usd"] = round(prof["cache"]["cache_savings_usd"], 4)

        # Reasoning percentage of output
        out_tokens = prof["tokens"]["output_tokens"]
        if out_tokens > 0:
            prof["tokens"]["reasoning_pct_of_output"] = round(
                (prof["tokens"]["reasoning_tokens"] / out_tokens) * 100.0, 2
            )

        prof["tokens"]["output_distribution"] = compute_percentiles(model_output_samples[key])
        prof["tokens"]["output_missing_count"] = prof["requests_count"] - len(model_output_samples[key])
        prof["tokens"]["output_observed_count"] = len(model_output_samples[key])
        # Cost per request
        priced_cnt = prof["cost"]["priced_requests_count"]
        if priced_cnt > 0:
            prof["cost"]["cost_per_request_usd"] = round(
                prof["cost"]["api_equivalent_usd"] / priced_cnt, 5
            )
        prof["cost"]["api_equivalent_usd"] = round(prof["cost"]["api_equivalent_usd"], 4) if priced_cnt else None
        prof["cost"]["api_equivalent_inr"] = round(prof["cost"]["api_equivalent_inr"], 2) if financial_coverage[key]["inr"] else None
        prof["cost"]["actual_billed_amount"] = round(prof["cost"]["actual_billed_amount"], 4) if financial_coverage[key]["billed"] else None
        prof["cost"]["inr_requests_count"] = financial_coverage[key]["inr"]
        prof["cost"]["billed_requests_count"] = financial_coverage[key]["billed"]

        # Timing distributions
        prof["deep_ttft"]["distribution_ms"] = compute_percentiles(model_ttft_samples[key])
        prof["duration"]["distribution_ms"] = compute_percentiles(model_duration_samples[key])
        prof["generation_duration"]["distribution_ms"] = compute_percentiles(model_gen_duration_samples[key])
        prof["timing"]["tps_distribution"] = compute_percentiles(model_tps_samples[key])

        # Weighted TPS
        gen_sec = prof["timing"]["sum_generation_seconds"]
        if gen_sec > 0:
            prof["timing"]["weighted_tps"] = round(prof["tokens"]["output_tokens"] / gen_sec, 2)

        # Reliability
        rdata = reliability_data[key]
        total_reqs = prof["requests_count"]
        known_reqs = (
            rdata["ok"] + rdata["error"] + rdata["interrupted"] +
            rdata["rate_limited"] + rdata["timeout"] + rdata["abnormal_termination"]
        )
        failures = (
            rdata["error"] + rdata["interrupted"] +
            rdata["rate_limited"] + rdata["timeout"] + rdata["abnormal_termination"]
        )
        caveat = None
        if rdata["unknown"] > 0:
            caveat = f"{rdata['unknown']} request(s) have unrecorded or missing status and are excluded from the reliability denominator."

        prof["reliability"] = {
            "total_requests": total_reqs,
            "known_status_requests": known_reqs,
            "missing_status_requests": rdata["unknown"],
            "ok_count": rdata["ok"],
            "error_count": rdata["error"],
            "interrupted_count": rdata["interrupted"],
            "rate_limited_count": rdata["rate_limited"],
            "timeout_count": rdata["timeout"],
            "abnormal_termination_count": rdata["abnormal_termination"],
            "error_rate": round(failures / known_reqs, 4) if known_reqs > 0 else None,
            "retry_requests_count": rdata["retry_requests"],
            "retry_tokens": rdata["retry_tokens"],
            "missing_status_caveat": caveat,
        }

        # Project mix list
        prof["project_mix"] = [
            {
                "project_id": pid,
                "requests_count": p["requests_count"],
                "total_tokens": p["total_tokens"],
                "cost_usd": round(p["cost_usd"], 4) if p.get("priced_requests_count") else None,
            }
            for pid, p in sorted(
                model_project_stats[key].items(),
                key=lambda x: x[1]["requests_count"],
                reverse=True,
            )
        ]

        prof["session_mix"] = [
            {"session_id": sid, **stats} for sid, stats in sorted(model_session_stats[key].items())
        ]
        # Session trends
        prof["session_trends"] = [
            {
                "date": day,
                "sessions_count": len(d["sessions"]),
                "requests_count": d["requests_count"],
                "total_tokens": d["total_tokens"],
                "cost_usd": round(d["cost_usd"], 4) if d.get("priced_requests_count") else None,
                "output_tokens": d["output_tokens"],
                "median_duration_ms": _distribution_value(d["durations"], "p50"),
                "cache_hit_rate": round(d["cache_reads"] / (d["cache_reads"] + d["fresh_inputs"]), 4)
                if (d["cache_reads"] + d["fresh_inputs"]) > 0 else None,
            }
            for day, d in sorted(model_day_stats[key].items(), key=lambda x: x[0])
        ]

    # 5. Build Side-by-Side Model Comparison (all active models or canonical models)
    comparison_entries: List[Dict[str, Any]] = []
    for model_name, prof in all_profiles.items():
        req_cnt = prof["requests_count"]
        if req_cnt == 0:
            continue

        ttft_p50 = prof["deep_ttft"]["distribution_ms"].get("p50")
        ttft_p90 = prof["deep_ttft"]["distribution_ms"].get("p90")
        ttft_p95 = prof["deep_ttft"]["distribution_ms"].get("p95")
        tps_med = prof["timing"]["tps_distribution"].get("p50")
        weighted_tps = prof["timing"]["weighted_tps"]
        dur_p50 = prof["duration"]["distribution_ms"].get("p50")

        metrics = _comparison_metrics(comparison_samples[model_name])
        metrics["outcome_rating"]["value"] = None

        # Sparse warning
        is_sparse = req_cnt < SPARSE_SAMPLE_THRESHOLD

        comparison_entries.append({
            "model_identity": model_name,
            "provider": prof["provider"],
            "sample_count": req_cnt,
            "tokens_per_request": round(prof["tokens"]["total_tokens"] / req_cnt, 1),
            "output_per_request": round(prof["tokens"]["output_tokens"] / req_cnt, 1),
            "median_output": metrics["median_output"]["value"],
            "metrics": metrics,
            "matched_task": None,
            "effectiveness_eligible": False,
            "weighted_tps": weighted_tps,
            "median_tps": tps_med,
            "median_ttft_ms": ttft_p50,
            "p90_ttft_ms": ttft_p90,
            "p95_ttft_ms": ttft_p95,
            "median_duration_ms": dur_p50,
            "cache_hit_rate": prof["cache"]["cache_hit_rate"],
            "retry_error_rate": prof["reliability"]["error_rate"],
            "cost_per_request_usd": prof["cost"]["cost_per_request_usd"],
            "outcome_rating": None,
            "is_sparse": is_sparse,
            "sparisty_disclaimer": "Sparse sample size (< 5 requests): no comparative effectiveness claim can be made."
            if is_sparse else None,
        })

    # Sort comparison by sample count descending
    comparison_entries.sort(key=lambda x: x["sample_count"], reverse=True)
    task_comparisons = []
    for task, cohorts in sorted(matched_samples.items()):
        adequate = len(cohorts) >= 2 and all(len(samples) >= SPARSE_SAMPLE_THRESHOLD for samples in cohorts.values())
        entries = []
        cohort_metrics = {model: _comparison_metrics(samples) for model, samples in cohorts.items()}
        rating_adequate = adequate and all(
            metrics["outcome_rating"]["sample_count"] >= SPARSE_SAMPLE_THRESHOLD
            for metrics in cohort_metrics.values())
        for model, samples in sorted(cohorts.items()):
            metrics = cohort_metrics[model]
            if not rating_adequate:
                metrics["outcome_rating"]["value"] = None
            entries.append({
                "model_identity": model, "provider": all_profiles[model]["provider"],
                "sample_count": len(samples), "metrics": metrics,
                "is_sparse": len(samples) < SPARSE_SAMPLE_THRESHOLD,
                "matched_task": task, "effectiveness_eligible": rating_adequate,
            })
        task_comparisons.append({"task_label": task, "models": entries, "adequate": adequate})

    # 6. Task Slices (Reliable manual labels only; effort missing explicit)
    task_slice_rows: List[Dict[str, Any]] = []
    for (m_name, t_name), tdata in sorted(task_groups.items(), key=lambda x: x[1]["requests_count"], reverse=True):
        req_c = tdata["requests_count"]
        out_toks = tdata["output_tokens"]
        fresh_toks = tdata["fresh_input_tokens"]
        reas_toks = tdata["reasoning_tokens"]
        tot_toks = tdata["total_tokens"]

        out_in_ratio = round(out_toks / fresh_toks, 3) if fresh_toks > 0 else None
        reas_pct = round((reas_toks / out_toks) * 100.0, 2) if out_toks > 0 else None

        # Format effort setting
        effort_list = sorted(tdata["effort_settings"])
        effort_display = ", ".join(effort_list) if effort_list else "missing"

        # Mean manual rating
        m_ratings = tdata["manual_ratings"]
        avg_rating = round(statistics.mean(m_ratings), 2) if m_ratings else None

        task_slice_rows.append({
            "model_identity": m_name,
            "task_label": t_name,
            "is_reliable_manual_label": tdata["is_manual"] and t_name != "unlabeled",
            "requests_count": req_c,
            "total_tokens": tot_toks,
            "tokens_per_request": round(tot_toks / req_c, 1),
            "output_tokens": out_toks,
            "output_per_request": round(out_toks / req_c, 1),
            "output_distribution": compute_percentiles(tdata["output_samples"]),
            "output_missing_count": req_c - len(tdata["output_samples"]),
            "output_to_input_ratio": out_in_ratio,
            "reasoning_tokens": reas_toks,
            "reasoning_pct_of_output": reas_pct,
            "effort_setting": effort_display,
            "retries_count": tdata["retries_count"],
            "median_duration_ms": _distribution_value(tdata["durations_ms"], "p50"),
            "median_tps": _distribution_value(tdata["tps_samples"], "p50"),
            "median_ttft_ms": _distribution_value(tdata["ttft_samples"], "p50"),
            "manual_outcome_rating": avg_rating,
            "caveat": "Output size alone is not a quality measure. Shorter outputs do not imply superior task completion.",
        })

    # 7. Model Switching Timeline (ordered requests within session)
    switching_events: List[Dict[str, Any]] = []
    total_switches_observed = 0

    for s_id, s_reqs in session_ordered_requests.items():
        # Sort requests chronologically
        s_reqs.sort(key=lambda x: x["request_timestamp"])

        prev_model: Optional[str] = None
        session_switch_idx = 0

        for idx, req in enumerate(s_reqs):
            curr_model = req["display_name"]
            if prev_model is not None and curr_model != prev_model:
                session_switch_idx += 1
                total_switches_observed += 1

                # Prior request context
                prior_req = s_reqs[idx - 1]
                cost_label = prior_req["cost_usd"] if prior_req["cost_usd"] is not None else "unknown"
                dur_label = prior_req["duration_ms"] if prior_req["duration_ms"] is not None else "unknown"

                switching_events.append({
                    "session_id": s_id,
                    "switch_index": session_switch_idx,
                    "from_model": prev_model,
                    "to_model": curr_model,
                    "switch_timestamp": req["request_timestamp"],
                    "triggered_by_canonical_id": req["canonical_id"],
                    "prior_request_canonical_id": prior_req["canonical_id"],
                    "prior_request_status": prior_req["request_status"],
                    "prior_request_tokens": prior_req["total_tokens"],
                    "prior_request_cost_usd": cost_label,
                    "prior_request_duration_ms": dur_label,
                })
            prev_model = curr_model

    # Sort switching events descending by timestamp
    switching_events.sort(key=lambda x: x["switch_timestamp"], reverse=True)

    # 8. High-level summary values
    values = {
        "total_requests": total_requests_observed,
        "total_tokens": total_tokens_observed,
        "total_cost_usd": round(total_cost_observed, 4),
        "canonical_models_configured": len(CANONICAL_DISPLAY_IDENTITIES),
        "canonical_models_observed": sum(1 for p in profiles.values() if p["requests_count"] > 0),
        "total_model_switches": total_switches_observed,
        "unresolved_overlap_requests": unresolved_overlap_count,
        "has_unresolved_overlap": unresolved_overlap_count > 0,
    }

    warnings: List[str] = []
    if unresolved_overlap_count > 0:
        warnings.append(
            f"{unresolved_overlap_count} requests have unresolved candidate cross-source overlaps. Scoped totals may be partial."
        )
    if any(c["is_sparse"] for c in comparison_entries):
        warnings.append(
            "One or more models in comparison have sparse sample sizes (< 5 requests). Comparative effectiveness claims abstain."
        )

    # Flattened rows representation for standard table rendering
    flattened_rows = [
        {
            "display_name": p["display_name"],
            "canonical_model_id": p["canonical_model_id"],
            "provider": p["provider"],
            "is_canonical": p["is_canonical"],
            "requests_count": p["requests_count"],
            "sessions_count": p["sessions_count"],
            "total_tokens": p["tokens"]["total_tokens"],
            "output_tokens": p["tokens"]["output_tokens"],
            "reasoning_tokens": p["tokens"]["reasoning_tokens"],
            "cache_hit_rate": p["cache"]["cache_hit_rate"],
            "cost_usd": p["cost"]["api_equivalent_usd"],
            "cost_per_request_usd": p["cost"]["cost_per_request_usd"],
            "weighted_tps": p["timing"]["weighted_tps"],
            "median_ttft_ms": p["deep_ttft"]["distribution_ms"].get("p50"),
            "p95_ttft_ms": p["deep_ttft"]["distribution_ms"].get("p95"),
            "error_rate": p["reliability"]["error_rate"],
            "retry_tokens": p["reliability"]["retry_tokens"],
        }
        for p in all_profiles.values()
    ]
    # Sort flattened rows by requests_count descending
    flattened_rows.sort(key=lambda x: x["requests_count"], reverse=True)

    return {
        "contract_version": "1",
        "query": norm_query,
        "metric_id": envelope.get("metric_id", "model_profiles"),
        "family": "model_profiles",
        "values": values,
        "rows": flattened_rows,
        "profiles": all_profiles,
        "comparison": {
            "models_compared": comparison_entries,
            "task_cohorts": task_comparisons,
            "matched_filters": norm_query,
            "disclaimer": "Comparison requires identical filters and matched tasks. Sparse samples abstain from effectiveness ranking.",
        },
        "task_slices": {
            "rows": task_slice_rows,
            "note": "Output size alone is not a quality measure. Shorter output does not imply superior work.",
        },
        "switching_timeline": {
            "events": switching_events,
            "total_switches": total_switches_observed,
            "disclaimer": "Model switching patterns are descriptive and do not assert intent or causation.",
        },
        "reliability": {
            "model_breakdown": {k: p["reliability"] for k, p in all_profiles.items() if p["requests_count"] > 0},
            "disclaimer": "Error rate denominator strictly comprises requests with recorded status. Retries tracked via distinct IDs.",
        },
        "eligible": {
            "total_requests": total_requests_observed,
            "priced_requests": sum(p["cost"]["priced_requests_count"] for p in all_profiles.values()),
            "timed_requests": sum(p["timing"]["eligible_timing_requests_count"] for p in all_profiles.values()),
            "ttft_requests": sum(p["deep_ttft"]["eligible_ttft_count"] for p in all_profiles.values()),
        },
        "observed": {
            "canonical_models_present": [k for k, p in profiles.items() if p["requests_count"] > 0],
            "unmatched_models_present": list(unmatched_profiles.keys()),
        },
        "missingness": {
            "unpriced_requests_count": sum(p["cost"]["unpriced_requests_count"] for p in all_profiles.values()),
            "untimed_requests_count": sum(p["timing"]["unobserved_timing_requests_count"] for p in all_profiles.values()),
            "missing_ttft_requests_count": sum(p["missingness"]["missing_ttft_count"] for p in all_profiles.values()),
            "missing_status_requests_count": sum(p["reliability"]["missing_status_requests"] for p in all_profiles.values()),
        },
        "coverage": {
            "harness_scope": norm_query.get("harness_scope", "combined"),
            "has_unresolved_overlap": unresolved_overlap_count > 0,
            "unresolved_overlap_requests_count": unresolved_overlap_count,
        },
        "warnings": warnings,
    }
