"""Token metrics and cache efficiency analytics for Token Ledger.

Implements WP12:
- Feature 7: Complete Token Breakdown
  Disjoint billable counters (fresh_input, cache_read, cache_write_5m, cache_write_1h,
  cache_write_unknown_ttl, output) remain strictly distinct; non-additive reasoning_tokens
  is tracked as a reported subset of output_tokens; source reported total is tracked separately.
  Breakdowns and percentages at model, project, session, and day grains.
- Feature 8: Cache Efficiency Dashboard
  Hit rates strictly name eligible prompt denominator (fresh_input + cache_read).
  Trends, model comparisons, priced vs unpriced no-cache cost estimates without
  conflating unpriced samples with $0.00.
- Feature 9: Cache-Write Economics & Break-Even Analysis
  Per-TTL break-even simulator based on write premium and read discount.
  Scenario inputs validated explicitly; strict disclaimer that simulator is a stated-policy
  projection without claiming observed causality.
- Exported cache_scenario(...) function for API and UI local parameter testing.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from token_ledger.query import filtered_request_rows, validate_query


SIMULATOR_RATE_KEYS = (
    "fresh_rate_per_m",
    "read_rate_per_m",
    "write_5m_rate_per_m",
    "write_1h_rate_per_m",
)

SUPPORTED_GROUP_DIMENSIONS = {"model", "project", "session", "day"}

BILLABLE_COUNTER_FIELDS = (
    "fresh_input_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_write_unknown_ttl_tokens",
    "output_tokens",
)

# The cache-write buckets are alternatives: a source reports the 5m/1h split,
# or only an unknown-TTL total. One filled bucket means cache writes were seen.
CACHE_WRITE_FIELDS = (
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_write_unknown_ttl_tokens",
)
REQUIRED_COUNTER_FIELDS = ("fresh_input_tokens", "cache_read_tokens", "output_tokens")


def _counters_complete(row: Dict[str, Any]) -> bool:
    """True when every billable counter group was observed for the request."""
    return all(row.get(f) is not None for f in REQUIRED_COUNTER_FIELDS) and any(
        row.get(f) is not None for f in CACHE_WRITE_FIELDS
    )




def cache_scenario(
    raw_inputs: Dict[str, Any],
    baseline_tokens: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Evaluates a cache scenario using explicitly supplied hypothetical rates."""
    params = dict(raw_inputs or {})

    try:
        rates = {key: float(params[key]) for key in SIMULATOR_RATE_KEYS if key in params}
        reuse_count = float(params.get("reuse_count", 3.0))
        token_count = int(params.get("token_count", 100_000))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid simulator numeric parameter: {exc}") from exc

    fresh_rate = rates.get("fresh_rate_per_m")
    read_rate = rates.get("read_rate_per_m")
    write_5m_rate = rates.get("write_5m_rate_per_m")
    write_1h_rate = rates.get("write_1h_rate_per_m")

    if fresh_rate is not None and fresh_rate <= 0:
        raise ValueError(f"fresh_rate_per_m must be strictly positive, got {fresh_rate}")
    if read_rate is not None and read_rate < 0:
        raise ValueError(f"read_rate_per_m cannot be negative, got {read_rate}")
    if fresh_rate is not None and read_rate is not None and read_rate > fresh_rate:
        raise ValueError(f"read_rate_per_m ({read_rate}) cannot exceed fresh_rate_per_m ({fresh_rate})")
    if fresh_rate is not None and write_5m_rate is not None and write_5m_rate < fresh_rate:
        raise ValueError(f"write_5m_rate_per_m ({write_5m_rate}) cannot be less than fresh_rate_per_m ({fresh_rate})")
    if fresh_rate is not None and write_1h_rate is not None and write_1h_rate < fresh_rate:
        raise ValueError(f"write_1h_rate_per_m ({write_1h_rate}) cannot be less than fresh_rate_per_m ({fresh_rate})")
    if reuse_count < 0:
        raise ValueError(f"reuse_count cannot be negative, got {reuse_count}")
    if token_count < 0:
        raise ValueError(f"token_count cannot be negative, got {token_count}")

    missing_rates = [key for key in SIMULATOR_RATE_KEYS if key not in rates]
    if missing_rates:
        return {
            "status": "unavailable",
            "reason": "Supply all four hypothetical rate assumptions to calculate break-even costs.",
            "missing_rate_assumptions": missing_rates,
            "parameters": {
                **{key: rates.get(key) for key in SIMULATOR_RATE_KEYS},
                "reuse_count": reuse_count,
                "token_count": token_count,
            },
            "break_even_reuses": {"ttl_5m": None, "ttl_1h": None, "ttl_unknown": None},
            "single_block_projection": {
                "status": "unavailable",
                "token_count": token_count,
                "reuse_count": reuse_count,
            },
            "baseline_evaluation": None,
            "policy_assumptions": {
                "causality_claim": False,
                "rate_basis": "user-supplied hypothetical rates required",
                "disclaimer": (
                    "No break-even costs are calculated until all four hypothetical rates are supplied. "
                    "Reuse count and token count are scenario assumptions, not observed activity."
                ),
                "unit": "USD per 1,000,000 tokens",
            },
        }

    assert fresh_rate is not None
    assert read_rate is not None
    assert write_5m_rate is not None
    assert write_1h_rate is not None

    read_discount = fresh_rate - read_rate
    write_5m_premium = write_5m_rate - fresh_rate
    write_1h_premium = write_1h_rate - fresh_rate
    be_reuses_5m = (write_5m_premium / read_discount) if read_discount > 0 else float("inf")
    be_reuses_1h = (write_1h_premium / read_discount) if read_discount > 0 else float("inf")

    units_m = token_count / 1_000_000.0
    no_cache_cost = units_m * fresh_rate * (1.0 + reuse_count)
    cost_5m = units_m * (write_5m_rate + (reuse_count * read_rate))
    savings_5m = no_cache_cost - cost_5m
    cost_1h = units_m * (write_1h_rate + (reuse_count * read_rate))
    savings_1h = no_cache_cost - cost_1h

    baseline_eval = None
    if baseline_tokens:
        b_fresh = baseline_tokens.get("fresh_input", 0) or 0
        b_read = baseline_tokens.get("cache_read", 0) or 0
        b_write_5m = baseline_tokens.get("cache_write_5m", 0) or 0
        b_write_1h = baseline_tokens.get("cache_write_1h", 0) or 0
        b_write_unk = baseline_tokens.get("cache_write_unknown_ttl", 0) or 0
        # ASSUMPTION: unknown TTL is priced as the 5m default.
        curr_sim_cost = (
            (b_fresh / 1e6) * fresh_rate
            + (b_read / 1e6) * read_rate
            + ((b_write_5m + b_write_unk) / 1e6) * write_5m_rate
            + (b_write_1h / 1e6) * write_1h_rate
        )
        no_cache_sim_cost = ((b_fresh + b_read) / 1e6) * fresh_rate
        baseline_eval = {
            "fresh_tokens": b_fresh,
            "cache_read_tokens": b_read,
            "cache_write_5m_tokens": b_write_5m,
            "cache_write_1h_tokens": b_write_1h,
            "cache_write_unknown_ttl_tokens": b_write_unk,
            "simulated_with_cache_cost_usd": round(curr_sim_cost, 4),
            "simulated_no_cache_cost_usd": round(no_cache_sim_cost, 4),
            "simulated_net_savings_usd": round(no_cache_sim_cost - curr_sim_cost, 4),
            "unknown_ttl_note": "Unknown TTL tokens are priced at the 5m write rate in simulation (assumption: unknown TTL is the 5m default)." if b_write_unk > 0 else None,
        }

    return {
        "status": "available",
        "rate_basis": "user-supplied hypothetical rates",
        "missing_rate_assumptions": [],
        "parameters": {**rates, "reuse_count": reuse_count, "token_count": token_count},
        "break_even_reuses": {
            "ttl_5m": round(be_reuses_5m, 4) if not math.isinf(be_reuses_5m) else None,
            "ttl_1h": round(be_reuses_1h, 4) if not math.isinf(be_reuses_1h) else None,
            "ttl_unknown": None,
        },
        "single_block_projection": {
            "token_count": token_count,
            "reuse_count": reuse_count,
            "no_cache_cost_usd": round(no_cache_cost, 4),
            "ttl_5m": {
                "with_cache_cost_usd": round(cost_5m, 4),
                "net_savings_usd": round(savings_5m, 4),
                "is_profitable": savings_5m > 0,
            },
            "ttl_1h": {
                "with_cache_cost_usd": round(cost_1h, 4),
                "net_savings_usd": round(savings_1h, 4),
                "is_profitable": savings_1h > 0,
            },
            "ttl_unknown": {
                "status": "unavailable",
                "reason": "TTL duration unmeasured in source; write rate cannot be determined.",
            },
        },
        "baseline_evaluation": baseline_eval,
        "policy_assumptions": {
            "causality_claim": False,
            "rate_basis": "user-supplied hypothetical rates",
            "disclaimer": (
                "Mathematical break-even projection based on the supplied hypothetical rate schedule. "
                "Does not assert or prove historical cache eviction causality or prompt reuse behavior."
            ),
            "unit": "USD per 1,000,000 tokens",
        },
    }


def analyze(conn: sqlite3.Connection, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Computes comprehensive token breakdown and cache efficiency analytics.

    Follows the shared query contract and analytics conventions:
    1. Retrieves canonical joined rows via `filtered_request_rows(conn, envelope)`.
    2. Aggregates disjoint billable tokens, percentages, reasoning subset, and reported total.
    3. Evaluates cache efficiency, hit rates, trends, and no-cache cost comparisons.
    4. Computes per-TTL break-even economics and runs the cache scenario simulator.
    5. Preserves null counters, handles unpriced samples cleanly, and flags unresolved overlaps.
    """
    # 1. Validate the shared query envelope, including token-specific assumptions.
    norm_envelope = validate_query(dict(envelope or {}))
    # 2. Retrieve fully filtered 1-to-1 canonical rows
    rows = filtered_request_rows(conn, norm_envelope)

    # Determine requested grouping
    requested_groups = norm_envelope.get("group_by") or []
    group_by = None
    for g in requested_groups:
        if g in SUPPORTED_GROUP_DIMENSIONS:
            group_by = g
            break

    # Initialize aggregators
    total_fresh_input = 0
    total_cache_read = 0
    total_cache_write_5m = 0
    total_cache_write_1h = 0
    total_cache_write_unk = 0
    total_output = 0
    total_reasoning = 0
    total_reported_source = 0

    has_any_fresh = False
    has_any_read = False
    has_any_write_5m = False
    has_any_write_1h = False
    has_any_write_unk = False
    has_any_output = False
    has_any_reasoning = False
    has_any_reported_source = False

    total_requests = len(rows)
    distinct_sessions = set()
    unresolved_overlap_count = 0
    null_counter_requests_count = 0
    partial_counter_requests_count = 0
    has_unobserved_billable_counters = False

    priced_requests_count = 0
    unpriced_requests_count = 0
    total_api_equivalent_usd = 0.0
    total_cache_savings_usd = 0.0
    no_cache_estimated_usd = 0.0
    net_cache_benefit_usd = 0.0
    api_equivalent_count = 0
    cache_savings_count = 0
    no_cache_estimate_count = 0
    net_cache_benefit_count = 0
    has_any_api_equivalent = False
    has_any_cache_savings = False
    has_any_no_cache_estimate = False
    # For grouping
    groups: Dict[str, Dict[str, Any]] = {}
    # For daily trend
    daily_trends: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        cid = row["canonical_id"]
        sess_id = row.get("session_id")
        if sess_id:
            distinct_sessions.add(sess_id)

        if row.get("is_unresolved_overlap"):
            unresolved_overlap_count += 1

        f_in = row.get("fresh_input_tokens")
        c_read = row.get("cache_read_tokens")
        w_5m = row.get("cache_write_5m_tokens")
        w_1h = row.get("cache_write_1h_tokens")
        w_unk = row.get("cache_write_unknown_ttl_tokens")
        out_t = row.get("output_tokens")
        reas_t = row.get("reasoning_tokens")
        rep_tot = row.get("reported_total_tokens")
        observed_counter_count = sum(
            1 for field_name in BILLABLE_COUNTER_FIELDS if row.get(field_name) is not None
        )
        counters_complete = _counters_complete(row)
        if observed_counter_count == 0:
            null_counter_requests_count += 1
        elif not counters_complete:
            partial_counter_requests_count += 1
        if not counters_complete:
            has_unobserved_billable_counters = True

        # Track null vs measured 0
        if f_in is not None:
            has_any_fresh = True
            total_fresh_input += f_in
        if c_read is not None:
            has_any_read = True
            total_cache_read += c_read
        if w_5m is not None:
            has_any_write_5m = True
            total_cache_write_5m += w_5m
        if w_1h is not None:
            has_any_write_1h = True
            total_cache_write_1h += w_1h
        if w_unk is not None:
            has_any_write_unk = True
            total_cache_write_unk += w_unk
        if out_t is not None:
            has_any_output = True
            total_output += out_t
        if reas_t is not None:
            has_any_reasoning = True
            total_reasoning += reas_t
        if rep_tot is not None:
            has_any_reported_source = True
            total_reported_source += rep_tot

        # Pricing facts
        p_status = row.get("pricing_status")
        api_usd = row.get("api_equivalent_usd")
        c_savings = row.get("cache_savings_usd")

        if p_status == "priced":
            priced_requests_count += 1
        else:
            unpriced_requests_count += 1
        if api_usd is not None:
            total_api_equivalent_usd += api_usd
            has_any_api_equivalent = True
            api_equivalent_count += 1
        if c_savings is not None:
            total_cache_savings_usd += c_savings
            has_any_cache_savings = True
            cache_savings_count += 1

        # A no-cache estimate requires both source cost components.
        if api_usd is not None and c_savings is not None:
            no_cache_estimated_usd += api_usd + c_savings
            net_cache_benefit_usd += c_savings
            net_cache_benefit_count += 1
            has_any_no_cache_estimate = True
            no_cache_estimate_count += 1
        # Determine group key
        req_ts = row.get("request_timestamp") or ""
        day_key = req_ts[:10] if len(req_ts) >= 10 else "unknown_day"

        group_key = "all"
        if group_by == "model":
            group_key = row.get("normalized_model") or "unknown_model"
        elif group_by == "project":
            group_key = row.get("project_id") or "unassigned_project"
        elif group_by == "session":
            group_key = sess_id or "unassigned_session"
        elif group_by == "day":
            group_key = day_key

        # Cache-write buckets are alternatives: when this request observed any bucket,
        # its null buckets mean 0 for this request, not missing.
        cw_seen = any(v is not None for v in (w_5m, w_1h, w_unk))
        eff_w5m = 0 if w_5m is None and cw_seen else w_5m
        eff_w1h = 0 if w_1h is None and cw_seen else w_1h
        eff_wunk = 0 if w_unk is None and cw_seen else w_unk

        if group_key not in groups:
            groups[group_key] = {
                "key": group_key,
                "dimension": group_by or "aggregate",
                "requests_count": 0,
                "sessions_set": set(),
                "fresh_input_tokens": 0 if f_in is not None else None,
                "cache_read_tokens": 0 if c_read is not None else None,
                "cache_write_5m_tokens": 0 if eff_w5m is not None else None,
                "cache_write_1h_tokens": 0 if eff_w1h is not None else None,
                "cache_write_unknown_ttl_tokens": 0 if eff_wunk is not None else None,
                "output_tokens": 0 if out_t is not None else None,
                "reasoning_tokens": 0 if reas_t is not None else None,
                "reported_total_tokens": 0 if rep_tot is not None else None,
                "priced_requests": 0,
                "unpriced_requests": 0,
                "api_equivalent_usd": 0.0,
                "cache_savings_usd": 0.0,
                "has_api_equivalent": False,
                "has_cache_savings": False,
                "api_equivalent_count": 0,
                "cache_savings_count": 0,
                "missing_token_fields": set(),
                "has_incomplete_counters": False,
                "has_unresolved_overlap": False,
            }

        g_item = groups[group_key]
        g_item["requests_count"] += 1
        g_item["has_incomplete_counters"] = (
            g_item["has_incomplete_counters"]
            or not counters_complete
        )
        g_item["has_unresolved_overlap"] = (
            g_item["has_unresolved_overlap"] or bool(row.get("is_unresolved_overlap"))
        )
        if sess_id:
            g_item["sessions_set"].add(sess_id)

        for field_name, val in [
            ("fresh_input_tokens", f_in),
            ("cache_read_tokens", c_read),
            ("cache_write_5m_tokens", eff_w5m),
            ("cache_write_1h_tokens", eff_w1h),
            ("cache_write_unknown_ttl_tokens", eff_wunk),
            ("output_tokens", out_t),
            ("reasoning_tokens", reas_t),
            ("reported_total_tokens", rep_tot),
        ]:
            if val is None:
                g_item["missing_token_fields"].add(field_name)
            else:
                if g_item[field_name] is None:
                    g_item[field_name] = 0
                g_item[field_name] += val

        if p_status == "priced":
            g_item["priced_requests"] += 1
        else:
            g_item["unpriced_requests"] += 1
        if api_usd is not None:
            g_item["api_equivalent_usd"] += api_usd
            g_item["has_api_equivalent"] = True
            g_item["api_equivalent_count"] += 1
        if c_savings is not None:
            g_item["cache_savings_usd"] += c_savings
            g_item["has_cache_savings"] = True
            g_item["cache_savings_count"] += 1

        # Daily trends aggregation
        if day_key not in daily_trends:
            daily_trends[day_key] = {
                "day": day_key,
                "requests_count": 0,
                "fresh_input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "cache_write_unknown_ttl_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "eligible_prompt_tokens": 0,
                "cache_hit_rate": 0.0,
            }
        d_item = daily_trends[day_key]
        d_item["requests_count"] += 1
        for field_name, value in (
            ("fresh_input_tokens", f_in),
            ("cache_read_tokens", c_read),
            ("cache_write_5m_tokens", w_5m),
            ("cache_write_1h_tokens", w_1h),
            ("cache_write_unknown_ttl_tokens", w_unk),
            ("output_tokens", out_t),
            ("reasoning_tokens", reas_t),
        ):
            if value is None:
                d_item[field_name] = None
            elif d_item[field_name] is not None:
                d_item[field_name] += value
        if f_in is None or c_read is None:
            d_item["eligible_prompt_tokens"] = None
        elif d_item["eligible_prompt_tokens"] is not None:
            d_item["eligible_prompt_tokens"] += f_in + c_read

    # Disjoint billable total
    # If no counters were observed across any request, overall total is None
    all_counters_missing = not (
        has_any_fresh or has_any_read or has_any_write_5m or has_any_write_1h or has_any_write_unk or has_any_output
    )

    if all_counters_missing:
        total_tokens = None
        total_tokens_status = "unavailable"
    else:
        total_tokens = (
            total_fresh_input
            + total_cache_read
            + total_cache_write_5m
            + total_cache_write_1h
            + total_cache_write_unk
            + total_output
        )
        total_tokens_status = (
            "partial"
            if unresolved_overlap_count > 0 or has_unobserved_billable_counters
            else "exact"
        )

    # Cache hit rate: denominator is strictly fresh_input + cache_read
    eligible_prompt_tokens = total_fresh_input + total_cache_read
    if eligible_prompt_tokens > 0:
        overall_cache_hit_rate = round(total_cache_read / float(eligible_prompt_tokens), 4)
    else:
        overall_cache_hit_rate = 0.0 if (has_any_fresh or has_any_read) else None

    # Calculate percentages helper
    def _calc_percentages(f, r, w5, w1, wu, o, tot) -> Dict[str, Optional[float]]:
        if not tot or tot <= 0:
            return {
                "fresh_input": None,
                "cache_read": None,
                "cache_write_5m": None,
                "cache_write_1h": None,
                "cache_write_unknown_ttl": None,
                "output": None,
            }
        return {
            "fresh_input": round(((f or 0) / float(tot)) * 100.0, 2),
            "cache_read": round(((r or 0) / float(tot)) * 100.0, 2),
            "cache_write_5m": round(((w5 or 0) / float(tot)) * 100.0, 2),
            "cache_write_1h": round(((w1 or 0) / float(tot)) * 100.0, 2),
            "cache_write_unknown_ttl": round(((wu or 0) / float(tot)) * 100.0, 2),
            "output": round(((o or 0) / float(tot)) * 100.0, 2),
        }

    overall_percentages = _calc_percentages(
        total_fresh_input if has_any_fresh else 0,
        total_cache_read if has_any_read else 0,
        total_cache_write_5m if has_any_write_5m else 0,
        total_cache_write_1h if has_any_write_1h else 0,
        total_cache_write_unk if has_any_write_unk else 0,
        total_output if has_any_output else 0,
        total_tokens,
    )

    # Reasoning percentage of output
    reasoning_pct_of_output = None
    if total_output > 0 and has_any_reasoning:
        reasoning_pct_of_output = round((total_reasoning / float(total_output)) * 100.0, 2)

    # Format group rows
    formatted_rows: List[Dict[str, Any]] = []
    for g_key in sorted(groups.keys()):
        g_data = groups[g_key]
        gf = g_data["fresh_input_tokens"]
        gr = g_data["cache_read_tokens"]
        gw5 = g_data["cache_write_5m_tokens"]
        gw1 = g_data["cache_write_1h_tokens"]
        gwu = g_data["cache_write_unknown_ttl_tokens"]
        go = g_data["output_tokens"]
        greas = g_data["reasoning_tokens"]
        grep = g_data["reported_total_tokens"]

        billable_values = [gf, gr, gw5, gw1, gwu, go]
        observed_billable = [value for value in billable_values if value is not None]
        g_total = sum(observed_billable) if observed_billable else None
        if g_total is None:
            g_total_status = "partial" if g_data["has_unresolved_overlap"] else "unavailable"
        else:
            g_total_status = (
                "partial"
                if g_data["has_incomplete_counters"] or g_data["has_unresolved_overlap"]
                else "exact"
            )

        token_fields = {
            "fresh_input_tokens": gf,
            "cache_read_tokens": gr,
            "cache_write_5m_tokens": gw5,
            "cache_write_1h_tokens": gw1,
            "cache_write_unknown_ttl_tokens": gwu,
            "output_tokens": go,
            "reasoning_tokens": greas,
            "reported_total_tokens": grep,
        }
        token_field_status = {
            field: (
                "unavailable"
                if value is None
                else "partial" if field in g_data["missing_token_fields"] else "exact"
            )
            for field, value in token_fields.items()
        }

        g_eligible = gf + gr if gf is not None and gr is not None else None
        g_hit_rate = (
            None
            if g_eligible is None
            else round(gr / float(g_eligible), 4) if g_eligible > 0 else 0.0
        )

        g_row = {
            "group_key": g_key,
            "dimension": g_data["dimension"],
            "requests_count": g_data["requests_count"],
            "sessions_count": len(g_data["sessions_set"]),
            "fresh_input_tokens": gf,
            "cache_read_tokens": gr,
            "cache_write_5m_tokens": gw5,
            "cache_write_1h_tokens": gw1,
            "cache_write_unknown_ttl_tokens": gwu,
            "output_tokens": go,
            "total_tokens": g_total,
            "total_tokens_status": g_total_status,
            "token_fields_status": token_field_status,
            "reasoning_tokens": greas,
            "reported_total_tokens": grep,
            "percentages": _calc_percentages(gf, gr, gw5, gw1, gwu, go, g_total),
            "reasoning_percentage_of_output": round((greas / float(go)) * 100.0, 2) if (greas and go) else None,
            "cache_hit_rate": g_hit_rate,
            "eligible_prompt_tokens": g_eligible,
            "priced_requests_count": g_data["priced_requests"],
            "unpriced_requests_count": g_data["unpriced_requests"],
            "api_equivalent_usd": (
                round(g_data["api_equivalent_usd"], 4) if g_data["has_api_equivalent"] else None
            ),
            "api_equivalent_usd_status": (
                "unavailable"
                if not g_data["has_api_equivalent"]
                else "exact"
                if g_data["api_equivalent_count"] == g_data["requests_count"]
                else "partial"
            ),
            "cache_savings_usd": (
                round(g_data["cache_savings_usd"], 4) if g_data["has_cache_savings"] else None
            ),
            "cache_savings_usd_status": (
                "unavailable"
                if not g_data["has_cache_savings"]
                else "exact"
                if g_data["cache_savings_count"] == g_data["requests_count"]
                else "partial"
            ),
        }
        formatted_rows.append(g_row)

    # Format daily trends
    formatted_trends: List[Dict[str, Any]] = []
    for d_key in sorted(daily_trends.keys()):
        d_val = daily_trends[d_key]
        el = d_val["eligible_prompt_tokens"]
        d_val["cache_hit_rate"] = (
            None
            if el is None
            else round(d_val["cache_read_tokens"] / float(el), 4) if el > 0 else 0.0
        )
        formatted_trends.append(d_val)

    # Baseline evaluation only uses a complete observed counter set; never treat
    # missing source counters as measured zero.
    baseline_volume = None
    if total_requests and not has_unobserved_billable_counters:
        baseline_volume = {
            "fresh_input": total_fresh_input,
            "cache_read": total_cache_read,
            "cache_write_5m": total_cache_write_5m,
            "cache_write_1h": total_cache_write_1h,
            "cache_write_unknown_ttl": total_cache_write_unk,
        }
    sim_result = cache_scenario(
        raw_inputs=norm_envelope.get("simulator_parameters") or {},
        baseline_tokens=baseline_volume,
    )

    # Assemble warnings
    warnings: List[str] = []
    if unresolved_overlap_count > 0:
        warnings.append(
            f"Scope contains {unresolved_overlap_count} unresolved cross-source overlap requests; "
            "token totals are marked partial."
        )
    if unpriced_requests_count > 0:
        warnings.append(
            f"{unpriced_requests_count} request(s) lack pricing rate cards or complete pricing facts; "
            "available financial facts are retained and cost aggregates report their observed coverage."
        )
    if total_cache_write_unk > 0:
        warnings.append(
            f"{total_cache_write_unk} cache write token(s) have unknown TTL; they are priced at the 5m write rate (assumption: unknown TTL is the 5m default)."
        )

    response: Dict[str, Any] = {
        "contract_version": "1",
        "query": norm_envelope,
        "metric_id": norm_envelope.get("metric_id", "tokens"),
        "family": "tokens",
        "values": {
            "total_tokens": total_tokens,
            "total_tokens_status": total_tokens_status,
            "fresh_input_tokens": total_fresh_input if has_any_fresh else None,
            "cache_read_tokens": total_cache_read if has_any_read else None,
            "cache_write_5m_tokens": total_cache_write_5m if has_any_write_5m else None,
            "cache_write_1h_tokens": total_cache_write_1h if has_any_write_1h else None,
            "cache_write_unknown_ttl_tokens": total_cache_write_unk if has_any_write_unk else None,
            "output_tokens": total_output if has_any_output else None,
            "reasoning_tokens": total_reasoning if has_any_reasoning else None,
            "reported_total_tokens": total_reported_source if has_any_reported_source else None,
            "requests_count": total_requests,
            "sessions_count": len(distinct_sessions),
            "cache_hit_rate": overall_cache_hit_rate,
            "eligible_prompt_tokens": eligible_prompt_tokens,
            "percentages": overall_percentages,
            "reasoning_percentage_of_output": reasoning_pct_of_output,
            "priced_requests_count": priced_requests_count,
            "unpriced_requests_count": unpriced_requests_count,
            "api_equivalent_usd": round(total_api_equivalent_usd, 4) if has_any_api_equivalent else None,
            "api_equivalent_usd_status": (
                "unavailable" if api_equivalent_count == 0
                else "exact" if api_equivalent_count == total_requests
                else "partial"
            ),
            "cache_savings_usd": round(total_cache_savings_usd, 4) if has_any_cache_savings else None,
            "cache_savings_usd_status": (
                "unavailable" if cache_savings_count == 0
                else "exact" if cache_savings_count == total_requests
                else "partial"
            ),
            "no_cache_estimated_usd": round(no_cache_estimated_usd, 4) if has_any_no_cache_estimate else None,
            "no_cache_estimated_usd_status": (
                "unavailable" if no_cache_estimate_count == 0
                else "exact" if no_cache_estimate_count == total_requests
                else "partial"
            ),
            "no_cache_cost_difference_usd": (
                round(net_cache_benefit_usd, 4) if net_cache_benefit_count else None
            ),
            "no_cache_cost_difference_usd_status": (
                "unavailable" if net_cache_benefit_count == 0
                else "exact" if net_cache_benefit_count == total_requests
                else "partial"
            ),
        },
        "rows": formatted_rows,
        "trends": formatted_trends,
        "cache_efficiency": {
            "eligible_denominator_formula": "fresh_input_tokens + cache_read_tokens",
            "eligible_prompt_tokens": eligible_prompt_tokens,
            "cache_read_tokens": total_cache_read,
            "cache_hit_rate": overall_cache_hit_rate,
            "priced_requests": priced_requests_count,
            "unpriced_requests": unpriced_requests_count,
            "measured_cost_usd": round(total_api_equivalent_usd, 4) if has_any_api_equivalent else None,
            "measured_cost_usd_status": (
                "unavailable" if api_equivalent_count == 0
                else "exact" if api_equivalent_count == total_requests
                else "partial"
            ),
            "no_cache_estimated_usd": round(no_cache_estimated_usd, 4) if has_any_no_cache_estimate else None,
            "no_cache_estimated_usd_status": (
                "unavailable" if no_cache_estimate_count == 0
                else "exact" if no_cache_estimate_count == total_requests
                else "partial"
            ),
            "net_cache_benefit_usd": (
                round(net_cache_benefit_usd, 4) if net_cache_benefit_count else None
            ),
            "net_cache_benefit_usd_status": (
                "unavailable" if net_cache_benefit_count == 0
                else "exact" if net_cache_benefit_count == total_requests
                else "partial"
            ),
            "assumptions": (
                "No-cache cost uses requests with both source cost components; unpriced requests are excluded."
            ),
        },
        "break_even": sim_result,
        "units": {
            "total_tokens": "tokens",
            "fresh_input_tokens": "tokens",
            "cache_read_tokens": "tokens",
            "cache_write_5m_tokens": "tokens",
            "cache_write_1h_tokens": "tokens",
            "cache_write_unknown_ttl_tokens": "tokens",
            "output_tokens": "tokens",
            "reasoning_tokens": "tokens",
            "reported_total_tokens": "tokens",
            "cache_hit_rate": "ratio",
            "api_equivalent_usd": "USD",
            "cache_savings_usd": "USD",
            "no_cache_estimated_usd": "USD",
        },
        "metric_definition": {
            "id": "tokens",
            "family": "tokens",
            "version": "1.0.0",
            "formula": (
                "Disjoint sum of 6 billable counters (fresh_input, cache_read, cache_write_5m, "
                "cache_write_1h, cache_write_unknown_ttl, output). Reasoning is a reported subset of output. "
                "Cache hit rate denominator is strictly fresh_input + cache_read."
            ),
            "denominator": "fresh_input_tokens + cache_read_tokens for cache hit rate",
        },
        "eligible": {
            "total_requests": total_requests,
            "prompt_eligible_requests": sum(1 for r in rows if (r.get("fresh_input_tokens") or r.get("cache_read_tokens"))),
            "priced_requests": priced_requests_count,
        },
        "observed": {
            "measured_fresh_input": has_any_fresh,
            "measured_cache_read": has_any_read,
            "measured_cache_write_5m": has_any_write_5m,
            "measured_cache_write_1h": has_any_write_1h,
            "measured_cache_write_unknown": has_any_write_unk,
            "measured_output": has_any_output,
            "measured_reasoning": has_any_reasoning,
            "measured_reported_source": has_any_reported_source,
        },
        "missingness": {
            "unpriced_requests_count": unpriced_requests_count,
            "priced_requests_count": priced_requests_count,
            "null_counters_requests_count": null_counter_requests_count,
            "partial_counters_requests_count": partial_counter_requests_count,
            "unknown_ttl_write_requests_count": sum(1 for r in rows if (r.get("cache_write_unknown_ttl_tokens") or 0) > 0),
        },
        "coverage": {
            "harness_scope": norm_envelope.get("harness_scope", "combined"),
            "unresolved_overlap_requests_count": unresolved_overlap_count,
            "has_unresolved_overlap": unresolved_overlap_count > 0,
        },
        "warnings": warnings,
    }

    return response
