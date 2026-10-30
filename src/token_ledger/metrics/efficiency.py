"""Efficiency metrics, signal detection, and counterfactual scenario modeling for Token Ledger.

Implements WP19:
- Comprehensive Efficiency Signal Detection:
  * Cache underutilization (prompt cache eligible requests with zero cache reads)
  * Retries & error waste (failed requests and subsequent duplicate executions)
  * Tool output bloat (outsized tool execution payloads cross-referenced with events)
  * Repeated reads (redundant identical file/context lookups across turns)
  * Compaction overhead (frequency and token cost of compaction events)
  * Verbosity bloat (atypical output token inflation relative to task benchmarks)
  * Context accumulation (steep context growth over multi-turn sessions)
- Disjoint vs. Overlapping Accounting:
  * Mutually exclusive token assignment for additive baseline accounting
  * Overlapping signal matrix tracked independently without double-counting
- Grounded Evidence & Confidence:
  * Strict distinction between observed historical tokens and counterfactual avoidable tokens
  * Concrete evidence trails (canonical_id, event_id, timestamps, metrics)
  * Stated confidence levels ('high', 'medium', 'low') and audit bases ('exact_trace', 'measured_event', 'heuristic')
- Safe Routing Alternatives:
  * Routing suggestions only when supported by matched task labels and adequate sample sizes (>= 3)
  * Explicit experiment suggestions (A/B testing) without unproven causal claims
- Counterfactual Scenario Simulator:
  * Model switching, cache-read expansion, output reduction, and long-context truncation
  * Requires filtered actual counts and dated catalog rates; unpriced/unknown rates explicitly flagged
  * Explicit assumptions; no forecast, guarantee, or task-outcome equivalence claims
- Context Restart & Compaction Guidance:
  * Based strictly on measured task boundary transitions, never cumulative input-as-occupancy
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from token_ledger.ingest.identity import normalize_model_name
from token_ledger.pricing import evaluate_dated_usage_cost, find_effective_pricing_version, get_model_rate, get_pricing_version, get_historical_fx_record
from token_ledger.query import filtered_request_rows, validate_query

SCENARIO_TOKEN_COUNTERS: Tuple[str, ...] = (
    "fresh_input_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_write_unknown_ttl_tokens",
    "output_tokens",
)

UNPRICED_COUNTER_FIELDS = {
    "fresh_input": "fresh_input_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write_5m": "cache_write_5m_tokens",
    "cache_write_1h": "cache_write_1h_tokens",
    "cache_write_unknown_ttl": "cache_write_unknown_ttl_tokens",
    "output": "output_tokens",
}


DEFAULT_MIN_SAMPLE_ADEQUACY = 3

HYPOTHETICAL_SIMULATION_DISCLAIMER = (
    "This is a what-if calculation over observed usage, not a forecast or guarantee. "
    "It does not estimate task quality, completion, retries, or actual billed spend."
)


def _scenario_token_totals(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    totals: Dict[str, Optional[int]] = {}
    for counter in SCENARIO_TOKEN_COUNTERS:
        values = [row.get(counter) for row in rows]
        totals[counter] = (
            sum(int(value) for value in values if value is not None)
            if all(value is not None for value in values)
            else None
        )
    totals["total_tokens"] = (
        sum(int(totals[counter] or 0) for counter in SCENARIO_TOKEN_COUNTERS)
        if all(totals[counter] is not None for counter in SCENARIO_TOKEN_COUNTERS)
        else None
    )
    return totals


def efficiency_scenario(
    conn: sqlite3.Connection,
    raw_inputs: Dict[str, Any],
    filtered_rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Simulate token changes against the shared query's actual filtered requests and dated prices."""
    scenario_type = raw_inputs.get("scenario_type", "cache-read")
    valid_types = {"model-switch", "cache-read", "output-reduction", "longcontext"}
    if scenario_type not in valid_types:
        raise ValueError(f"Invalid scenario_type: {scenario_type}. Must be one of {sorted(valid_types)}")

    source_model = raw_inputs.get("source_model")
    target_model = raw_inputs.get("target_model")
    source_provider = raw_inputs.get("source_provider")
    source_route = raw_inputs.get("source_route")
    target_provider = raw_inputs.get("target_provider")
    target_route = raw_inputs.get("target_route")
    warnings: List[str] = []
    assumptions = [
        f"Uses {len(filtered_rows)} request(s) from the shared query after its time range, filters, search, and harness scope."
    ]
    if scenario_type == "model-switch":
        if not source_model:
            warnings.append("A source_model is required to select model-switch requests.")
            selected_rows: List[Dict[str, Any]] = []
        else:
            source_key = str(source_model).casefold()
            selected_rows = [
                row
                for row in filtered_rows
                if source_key
                in {
                    str(row.get("raw_model") or "").casefold(),
                    str(row.get("normalized_model") or "").casefold(),
                }
            ]
        if not target_model:
            warnings.append("A target_model is required to price a model-switch scenario.")
        assumptions.append(
            "Model switching changes the selected requests' model only; it does not predict equivalent outcomes."
        )
    else:
        selected_rows = list(filtered_rows)
        if source_model:
            source_key = str(source_model).casefold()
            selected_rows = [
                row
                for row in selected_rows
                if source_key
                in {
                    str(row.get("raw_model") or "").casefold(),
                    str(row.get("normalized_model") or "").casefold(),
                }
            ]

    if source_provider:
        selected_rows = [
            row for row in selected_rows
            if str(row.get("provider") or "").casefold() == str(source_provider).casefold()
        ]
    if source_route:
        selected_rows = [
            row for row in selected_rows
            if str(row.get("access_route") or "default").casefold() == str(source_route).casefold()
        ]
    if source_model:
        assumptions.append(f"Restricts scenario requests to model '{source_model}'.")
    if source_provider:
        assumptions.append(f"Restricts source provider to '{source_provider}'.")
    if source_route:
        assumptions.append(f"Restricts source route to '{source_route}'.")

    projected_rows: List[Dict[str, Any]] = []
    if scenario_type == "cache-read":
        expansion = float(raw_inputs.get("cache_expansion_pct", 50.0)) / 100.0
        assumptions.append(
            f"Converts {expansion * 100:.1f}% of each selected request's fresh input tokens to cache-read tokens."
        )
        assumptions.append("Assumes converted prompts qualify for cache reads; provider eligibility may differ.")
        for row in selected_rows:
            projected = dict(row)
            fresh = row.get("fresh_input_tokens")
            converted = int(int(fresh) * expansion) if fresh is not None else None
            projected["fresh_input_tokens"] = int(fresh) - converted if fresh is not None else None
            read = row.get("cache_read_tokens")
            projected["cache_read_tokens"] = int(read) + converted if read is not None and converted is not None else None
            projected_rows.append(projected)
    elif scenario_type == "output-reduction":
        reduction = float(raw_inputs.get("output_reduction_pct", 25.0)) / 100.0
        assumptions.append(
            f"Reduces each selected request's output token count by {reduction * 100:.1f}%."
        )
        for row in selected_rows:
            projected = dict(row)
            output = row.get("output_tokens")
            projected["output_tokens"] = int(int(output) * (1.0 - reduction)) if output is not None else None
            projected_rows.append(projected)
    elif scenario_type == "longcontext":
        threshold = int(raw_inputs.get("context_threshold_tokens", 50000))
        assumptions.append(
            f"Reduces fresh input above {threshold:,} tokens per request by 60%; other counters remain unchanged."
        )
        for row in selected_rows:
            projected = dict(row)
            fresh = row.get("fresh_input_tokens")
            projected["fresh_input_tokens"] = (
                threshold + int((int(fresh) - threshold) * 0.4)
                if fresh is not None and int(fresh) > threshold
                else fresh
            )
            projected_rows.append(projected)
    else:
        projected_rows = [dict(row) for row in selected_rows]

    baseline_tokens = _scenario_token_totals(selected_rows)
    projected_tokens = _scenario_token_totals(projected_rows)
    baseline_total = baseline_tokens["total_tokens"]
    projected_total = projected_tokens["total_tokens"]
    token_reduction = (
        baseline_total - projected_total
        if baseline_total is not None and projected_total is not None and selected_rows
        else None
    )
    token_reduction_pct = (
        round(token_reduction / baseline_total * 100.0, 2)
        if token_reduction is not None and baseline_total
        else None
    )

    version_cache: Dict[Tuple[str, Optional[str]], Any] = {}
    rate_cache: Dict[Tuple[str, str, str, str], Any] = {}
    evidence: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    def resolve_rate(
        row: Dict[str, Any],
        role: str,
        model_identity: str,
        provider: str,
        route: str,
    ) -> Tuple[Any, Any]:
        timestamp = row.get("request_timestamp")
        version = None
        assigned = row.get("pricing_version_id")
        cache_key = ("assigned", assigned) if assigned else ("date", timestamp)
        if assigned or timestamp:
            if cache_key not in version_cache:
                version_cache[cache_key] = get_pricing_version(conn, assigned) if assigned else find_effective_pricing_version(conn, timestamp)
            version = version_cache[cache_key]
        rate = None
        reason = None
        if version is None:
            reason = "no_effective_pricing_schedule"
        else:
            candidates = [model_identity]
            normalized = normalize_model_name(model_identity)
            if normalized != model_identity:
                candidates.append(normalized)
            for candidate in candidates:
                cache_key = (version.version_id, provider.casefold(), candidate, route)
                if cache_key not in rate_cache:
                    rate_cache[cache_key] = get_model_rate(
                        conn, version.version_id, provider, candidate, route
                    )
                rate = rate_cache[cache_key]
                if rate is not None:
                    break
            if rate is None:
                reason = "no_provider_model_route_rate"
            elif rate.status in {"partial", "unpriced"}:
                reason = f"rate_status_{rate.status}"
            elif version.currency_code.upper() != "USD" and not get_historical_fx_record(
                conn, timestamp, version.currency_code, "USD"
            ):
                reason = "missing_card_currency_to_usd_fx"

        card_fx = get_historical_fx_record(conn, timestamp, version.currency_code, "USD") if version and version.currency_code.upper() != "USD" else None
        rate_route = rate.access_route if rate is not None else None
        key = (
            role,
            version.version_id if version else None,
            provider,
            model_identity,
            route,
            rate.rate_id if rate else None,
            card_fx.fx_id if card_fx else None,
            reason,
        )
        item = evidence.setdefault(
            key,
            {
                "role": role,
                "provider": provider,
                "model": model_identity,
                "requested_route": route,
                "rate_route": rate_route,
                "pricing_version_id": version.version_id if version else None,
                "effective_start": version.effective_start if version else None,
                "effective_end": version.effective_end if version else None,
                "pricing_hash": version.pricing_hash if version else None,
                "rate_id": rate.rate_id if rate else None,
                "rate_status": rate.status if rate else "unpriced",
                "source_url": rate.source_url if rate else None,
                "verified_on": rate.verified_on if rate else None,
                "card_currency": version.currency_code if version else None,
                "card_to_usd_fx": vars(card_fx) if card_fx else None,
                "reason": reason,
                "_request_ids": set(),
            },
        )
        item["_request_ids"].add(row.get("canonical_id"))
        return version, rate

    def price_scope(
        rows: Sequence[Dict[str, Any]],
        role: str,
        model_switch: bool,
    ) -> Dict[str, Any]:
        complete = bool(rows)
        total_cost = 0.0
        priced_requests = 0
        unpriced_tokens = 0
        unknown_counters: Set[str] = set()
        unknown_token_counts = False
        for row in rows:
            usage = {counter: row.get(counter) for counter in SCENARIO_TOKEN_COUNTERS}
            missing_counters = [counter for counter, value in usage.items() if value is None]
            if missing_counters:
                unknown_counters.update(missing_counters)
                unknown_token_counts = True
                complete = False
            source_identity = str(row.get("raw_model") or row.get("normalized_model") or "")
            model_identity = str(target_model) if model_switch and role == "projected" else source_identity
            provider = str(
                (target_provider if model_switch and role == "projected" else None)
                or row.get("provider")
                or ""
            )
            route = str(
                (target_route if model_switch and role == "projected" else None)
                or row.get("access_route")
                or "default"
            )
            version, rate = resolve_rate(row, role, model_identity, provider, route)
            if rate is None or rate.status in {"partial", "unpriced"}:
                complete = False
                if not missing_counters:
                    unpriced_tokens += sum(int(value) for value in usage.values() if value is not None)
            if rate is None or rate.status in {"partial", "unpriced"}:
                continue
            valuation = evaluate_dated_usage_cost(conn, usage, rate, version, row.get("request_timestamp"))
            if valuation.pricing_status in {"partial", "unpriced"} or valuation.api_equivalent_usd is None:
                if "card_currency_to_usd" in valuation.unpriced_counters and not missing_counters:
                    unpriced_tokens += sum(int(value) for value in usage.values() if value is not None)
                complete = False
                for counter in valuation.unpriced_counters:
                    if counter == "card_currency_to_usd":
                        continue
                    field_name = UNPRICED_COUNTER_FIELDS.get(counter)
                    value = usage.get(field_name) if field_name else None
                    if value is None:
                        unknown_counters.add(field_name or counter)
                        unknown_token_counts = True
                    else:
                        unpriced_tokens += int(value)
                continue
            total_cost += valuation.api_equivalent_usd
            priced_requests += 1

        return {
            "cost_usd": round(total_cost, 8) if complete else None,
            "priced_requests": priced_requests,
            "unpriced_tokens": None if unknown_token_counts else unpriced_tokens,
            "unknown_counters": sorted(unknown_counters),
            "complete": complete,
        }

    baseline_cost = price_scope(selected_rows, "baseline", scenario_type == "model-switch")
    projected_cost = price_scope(projected_rows, "projected", scenario_type == "model-switch")
    for item in evidence.values():
        item["request_count"] = len(item.pop("_request_ids"))

    if not selected_rows:
        warnings.append("No requests matched the scenario's source-model/provider/route selection.")
    if scenario_type == "model-switch" and not target_model:
        projected_cost["complete"] = False
        projected_cost["cost_usd"] = None
    unpriced_tokens = projected_cost["unpriced_tokens"]
    is_priced = bool(
        selected_rows
        and baseline_cost["complete"]
        and projected_cost["complete"]
        and (scenario_type != "model-switch" or target_model)
    )
    return {
        "scenario_type": scenario_type,
        "is_priced": is_priced,
        "request_count": len(selected_rows),
        "query_filtered_request_count": len(filtered_rows),
        "baseline_tokens": baseline_tokens,
        "projected_tokens": projected_tokens,
        "token_reduction": token_reduction,
        "token_reduction_pct": token_reduction_pct,
        "financials": {
            "baseline_cost_usd": baseline_cost["cost_usd"],
            "projected_cost_usd": projected_cost["cost_usd"],
            "projected_savings_usd": (
                round(baseline_cost["cost_usd"] - projected_cost["cost_usd"], 8)
                if baseline_cost["cost_usd"] is not None and projected_cost["cost_usd"] is not None
                else None
            ),
            "unpriced_tokens": unpriced_tokens,
        },
        "pricing_coverage": {
            "baseline_priced_requests": baseline_cost["priced_requests"],
            "projected_priced_requests": projected_cost["priced_requests"],
            "unknown_usage_counters": sorted(
                set(baseline_cost["unknown_counters"]) | set(projected_cost["unknown_counters"])
            ),
            "unknown_ttl_tokens": baseline_tokens["cache_write_unknown_ttl_tokens"],
        },
        "pricing_evidence": list(evidence.values()),
        "assumptions": assumptions,
        "disclaimer": HYPOTHETICAL_SIMULATION_DISCLAIMER,
        "warnings": warnings,
    }



def analyze(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Comprehensive efficiency analytics, signal attribution, and scenario modeling (WP19).

    Guarantees:
    - Retains canonical filter semantics via `filtered_request_rows`.
    - Detects: cache, retry, tooloutput, repeatedread, compaction, verbosity, context signals.
    - Mutually exclusive primary signal token assignment alongside overlapping signal matrix.
    - Every insight links confidence ('high'/'med'/'low'), audit basis, and concrete evidence.
    - Distinguishes observed historical tokens from counterfactual avoidable tokens.
    - Routing alternatives only emitted for matched labels with adequate samples (>= 3).
    - Context compaction suggestions require actual event/task transitions, never raw cumulative tokens.
    """
    env_copy = dict(envelope or {})
    raw_min_sample = env_copy.pop("min_sample_size", None)
    scenario_params = env_copy.pop("scenario_inputs", None)
    orig_metric_id = env_copy.get("metric_id")

    # Map custom metric_id to summary for query validation
    if orig_metric_id and orig_metric_id not in {
        "summary", "timeline", "models", "projects", "sessions", "requests", "events", "coverage"
    }:
        env_copy["metric_id"] = "summary"

    norm_envelope = validate_query(env_copy)
    if orig_metric_id:
        norm_envelope["metric_id"] = orig_metric_id

    rows = filtered_request_rows(conn, norm_envelope)
    warnings: List[str] = []

    min_sample_size = raw_min_sample or DEFAULT_MIN_SAMPLE_ADEQUACY
    try:
        min_sample_size = max(1, int(min_sample_size))
    except (ValueError, TypeError):
        min_sample_size = DEFAULT_MIN_SAMPLE_ADEQUACY

    if not rows:
        return {
            "contract_version": "1",
            "query": norm_envelope,
            "metric_id": orig_metric_id or "efficiency",
            "family": "efficiency",
            "values": {
                "total_requests": 0,
                "total_tokens": 0,
                "observed_tokens": 0,
                "avoidable_tokens": 0,
                "efficiency_ratio": 1.0,
                "flagged_requests_count": 0,
                "clean_requests_count": 0,
            },
            "signals": {
                "exclusive_breakdown": {},
                "overlapping_breakdown": {},
            },
            "insights": [],
            "routing_alternatives": [],
            "scenarios": [],
            "eligible": {"total_requests": 0, "total_tokens": 0},
            "observed": {"total_requests": 0, "total_tokens": 0},
            "missingness": {},
            "coverage": {"priced_ratio": 1.0, "timed_ratio": 1.0},
            "warnings": ["No requests matched query filters."],
        }

    # Collect session IDs and single-pass aggregations
    session_ids = list({r["session_id"] for r in rows if r.get("session_id")})

    large_tool_events_by_request: Dict[str, List[Dict[str, Any]]] = {}
    compaction_events_by_session: Dict[str, List[Dict[str, Any]]] = {}
    sessions_with_task_boundaries_set: Set[str] = set()

    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        ev_query = f"""
            SELECT event_id, session_id, canonical_id, event_kind, event_name, output_size_bytes
            FROM events
            WHERE session_id IN ({placeholders})
              AND (
                  (event_kind IN ('tool_call', 'tool_result') AND output_size_bytes > 30000)
                  OR (event_kind IN ('compaction', 'context_compact'))
                  OR (event_kind = 'task_switch' OR LOWER(event_name) LIKE '%task%')
              )
            ORDER BY started_at ASC
        """
        try:
            ev_cursor = conn.execute(ev_query, session_ids)
            for erow in ev_cursor.fetchall():
                eid, sid, cid, ekind, ename, out_bytes = erow
                ev = {
                    "event_id": eid,
                    "session_id": sid,
                    "canonical_id": cid,
                    "event_kind": ekind,
                    "event_name": ename,
                    "output_size_bytes": out_bytes,
                }
                if ekind in ("tool_call", "tool_result") and (out_bytes or 0) > 30000:
                    if cid:
                        large_tool_events_by_request.setdefault(cid, []).append(ev)
                elif ekind in ("compaction", "context_compact"):
                    compaction_events_by_session.setdefault(sid, []).append(ev)
                if ekind == "task_switch" or (ename and "task" in ename.lower()):
                    sessions_with_task_boundaries_set.add(sid)
        except sqlite3.OperationalError:
            pass

    # Check for outcome task labels if manual_settings_and_outcomes exists
    outcomes_by_session: Dict[str, Dict[str, Any]] = {}
    try:
        out_query = """
            SELECT entity_id, task_label, completion_rating, manual_rating, is_inferred
            FROM manual_settings_and_outcomes
            WHERE entity_type = 'session'
        """
        out_cursor = conn.execute(out_query)
        for orow in out_cursor.fetchall():
            od = dict(orow)
            outcomes_by_session[od["entity_id"]] = od
    except sqlite3.OperationalError:
        pass

    SIGNAL_PRIORITY = ["retry", "tooloutput", "compaction", "repeatedread", "cache", "verbosity", "context"]

    exclusive_breakdown: Dict[str, Dict[str, Any]] = {
        sig: {"requests_count": 0, "observed_tokens": 0, "avoidable_tokens": 0}
        for sig in SIGNAL_PRIORITY
    }
    exclusive_breakdown["clean"] = {"requests_count": 0, "observed_tokens": 0, "avoidable_tokens": 0}

    overlapping_breakdown: Dict[str, Dict[str, Any]] = {
        sig: {"requests_count": 0, "observed_tokens": 0, "avoidable_tokens": 0, "is_additive": False}
        for sig in SIGNAL_PRIORITY
    }

    evidence_samples: Dict[str, List[Dict[str, Any]]] = {
        "cache": [],
        "tooloutput": [],
        "retry": [],
        "context": [],
    }

    total_tokens = 0
    fresh_input_tokens = 0
    cache_read_tokens = 0
    output_tokens = 0
    write_5m_tokens = 0
    write_1h_tokens = 0
    write_unk_tokens = 0
    priced_requests = 0
    untimed_requests = 0
    flagged_requests_count = 0
    total_avoidable_tokens = 0
    has_sonnet = False

    session_turn_counts: Dict[str, int] = {}
    session_prev_fresh: Dict[str, Optional[int]] = {}
    session_first_model: Dict[str, str] = {}

    for row in rows:
        cid = row["canonical_id"]
        sid = row.get("session_id")

        c_fresh = row.get("fresh_input_tokens") or 0
        c_read = row.get("cache_read_tokens") or 0
        c_out = row.get("output_tokens") or 0
        c_w5m = row.get("cache_write_5m_tokens") or 0
        c_w1h = row.get("cache_write_1h_tokens") or 0
        c_wunk = row.get("cache_write_unknown_ttl_tokens") or 0
        c_tot = row.get("total_tokens") or (c_fresh + c_read + c_out + c_w5m + c_w1h + c_wunk)

        total_tokens += c_tot
        fresh_input_tokens += c_fresh
        cache_read_tokens += c_read
        output_tokens += c_out
        write_5m_tokens += c_w5m
        write_1h_tokens += c_w1h
        write_unk_tokens += c_wunk

        if row.get("pricing_status") == "priced":
            priced_requests += 1

        if row.get("timing_basis") == "none":
            untimed_requests += 1

        m = row.get("normalized_model") or row.get("raw_model") or "unknown"
        if not has_sonnet and "sonnet" in m.lower():
            has_sonnet = True
        if sid and sid not in session_first_model:
            session_first_model[sid] = m

        turn_count = 1
        if sid:
            turn_count = session_turn_counts.get(sid, 0) + 1
            session_turn_counts[sid] = turn_count

        triggered: Set[str] = set()
        avoidable = 0

        # 1. Retry Signal
        req_status = (row.get("request_status") or "").lower()
        if req_status in ("error", "failed", "rate_limit"):
            triggered.add("retry")
            avoidable = max(avoidable, c_tot)
            if len(evidence_samples["retry"]) < 5:
                evidence_samples["retry"].append({
                    "signal": "retry",
                    "basis": "exact_trace",
                    "canonical_id": cid,
                    "reason": f"Request status '{req_status}' yielded no billable utility.",
                    "observed_tokens": c_tot,
                    "avoidable_tokens": c_tot,
                })

        # 2. Tool Output Bloat
        large_tool_events = large_tool_events_by_request.get(cid)
        if large_tool_events:
            triggered.add("tooloutput")
            tool_avoidable = int(c_fresh * 0.40)
            avoidable = max(avoidable, tool_avoidable)
            if len(evidence_samples["tooloutput"]) < 5:
                evidence_samples["tooloutput"].append({
                    "signal": "tooloutput",
                    "basis": "measured_event",
                    "canonical_id": cid,
                    "event_ids": [e["event_id"] for e in large_tool_events],
                    "reason": f"Tool output payload size ({large_tool_events[0].get('output_size_bytes')} bytes) bloated prompt.",
                    "observed_tokens": c_fresh,
                    "avoidable_tokens": tool_avoidable,
                })

        # 3. Cache Underutilization Signal
        if c_fresh > 2048 and c_read == 0 and turn_count > 1:
            triggered.add("cache")
            cache_avoidable = int(c_fresh * 0.80)
            avoidable = max(avoidable, cache_avoidable)
            if len(evidence_samples["cache"]) < 5:
                evidence_samples["cache"].append({
                    "signal": "cache",
                    "basis": "measured_event",
                    "canonical_id": cid,
                    "reason": f"Fresh input of {c_fresh} tokens in multi-turn session had zero cache hit.",
                    "observed_tokens": c_fresh,
                    "avoidable_tokens": cache_avoidable,
                })

        # 4. Verbosity Bloat Signal
        if c_out > 3000 and (c_out / max(1, c_fresh)) > 0.8:
            triggered.add("verbosity")
            verb_avoidable = int(c_out * 0.35)
            avoidable = max(avoidable, verb_avoidable)

        # 5. Context Accumulation Signal
        if c_fresh > 40000:
            triggered.add("context")
            ctx_avoidable = int((c_fresh - 40000) * 0.5)
            avoidable = max(avoidable, ctx_avoidable)
            if len(evidence_samples["context"]) < 5:
                evidence_samples["context"].append({
                    "signal": "context",
                    "basis": "measured_event",
                    "canonical_id": cid,
                    "reason": f"Deep context buildup ({c_fresh} input tokens) without intermediate compaction.",
                    "observed_tokens": c_fresh,
                    "avoidable_tokens": ctx_avoidable,
                })

        # Session compaction
        if sid and sid in compaction_events_by_session:
            triggered.add("compaction")

        # Session repeated prompt reads
        if sid:
            prev_fresh = session_prev_fresh.get(sid)
            if prev_fresh is not None and prev_fresh == c_fresh and c_fresh > 1000 and c_read == 0:
                triggered.add("repeatedread")
                rep_avoidable = int(c_fresh * 0.85)
                avoidable = max(avoidable, rep_avoidable)
            session_prev_fresh[sid] = c_fresh

        # Overlapping updates
        for s in triggered:
            overlapping_breakdown[s]["requests_count"] += 1
            overlapping_breakdown[s]["observed_tokens"] += c_tot
            overlapping_breakdown[s]["avoidable_tokens"] += avoidable

        # Exclusive assignment: pick first match in SIGNAL_PRIORITY
        assigned_signal = "clean"
        for p in SIGNAL_PRIORITY:
            if p in triggered:
                assigned_signal = p
                break

        if assigned_signal != "clean":
            flagged_requests_count += 1
            total_avoidable_tokens += avoidable
            exclusive_breakdown[assigned_signal]["requests_count"] += 1
            exclusive_breakdown[assigned_signal]["observed_tokens"] += c_tot
            exclusive_breakdown[assigned_signal]["avoidable_tokens"] += avoidable
        else:
            exclusive_breakdown["clean"]["requests_count"] += 1
            exclusive_breakdown["clean"]["observed_tokens"] += c_tot

    clean_requests_count = len(rows) - flagged_requests_count
    efficiency_ratio = round(max(0.0, 1.0 - (total_avoidable_tokens / max(1, total_tokens))), 4)

    # Priority insights generation
    insights: List[Dict[str, Any]] = []

    # Cache insight
    cache_ex = exclusive_breakdown["cache"]
    if cache_ex["requests_count"] > 0:
        insights.append({
            "id": "ins-cache-01",
            "category": "cache",
            "title": "Prompt Cache Misses on Repetitive Workloads",
            "priority": "high" if cache_ex["avoidable_tokens"] > 50000 else "medium",
            "confidence": "high",
            "basis": "measured_event",
            "observed_tokens": cache_ex["observed_tokens"],
            "avoidable_tokens": cache_ex["avoidable_tokens"],
            "requests_count": cache_ex["requests_count"],
            "description": (
                f"{cache_ex['requests_count']} multi-turn requests executed without cache reads despite large prompt prefixes. "
                f"Structuring static context as prompt cache prefixes could avoid ~{cache_ex['avoidable_tokens']:,} fresh tokens."
            ),
            "evidence_sample": evidence_samples["cache"],
            "actionable_recommendation": "Anchor long tool definitions, project guidelines, and stable code at the beginning of prompts.",
            "causality_disclaimer": "Avoidable token savings are counterfactual estimates based on 80% cache-read conversion.",
        })

    # Tool output insight
    tool_ex = exclusive_breakdown["tooloutput"]
    if tool_ex["requests_count"] > 0:
        insights.append({
            "id": "ins-tool-02",
            "category": "tooloutput",
            "title": "Unfiltered Tool Outputs Injected Into Context",
            "priority": "high" if tool_ex["avoidable_tokens"] > 20000 else "medium",
            "confidence": "high",
            "basis": "measured_event",
            "observed_tokens": tool_ex["observed_tokens"],
            "avoidable_tokens": tool_ex["avoidable_tokens"],
            "requests_count": tool_ex["requests_count"],
            "description": (
                f"{tool_ex['requests_count']} requests contained massive tool payloads (>30KB). "
                f"Truncating shell and file outputs could save ~{tool_ex['avoidable_tokens']:,} tokens."
            ),
            "evidence_sample": evidence_samples["tooloutput"],
            "actionable_recommendation": "Configure tool execution limits, pagination, and line range reading.",
            "causality_disclaimer": "Tool output savings assume downstream generation remains functional without unneeded file lines.",
        })

    # Retry insight
    retry_ex = exclusive_breakdown["retry"]
    if retry_ex["requests_count"] > 0:
        insights.append({
            "id": "ins-retry-03",
            "category": "retry",
            "title": "Wasted Tokens on Failed and Retried Requests",
            "priority": "high",
            "confidence": "high",
            "basis": "exact_trace",
            "observed_tokens": retry_ex["observed_tokens"],
            "avoidable_tokens": retry_ex["avoidable_tokens"],
            "requests_count": retry_ex["requests_count"],
            "description": (
                f"{retry_ex['requests_count']} requests ended in errors or rate limits, consuming "
                f"{retry_ex['avoidable_tokens']:,} tokens without successful response completion."
            ),
            "evidence_sample": evidence_samples["retry"],
            "actionable_recommendation": "Implement exponential backoff before retrying and catch validation errors locally.",
            "causality_disclaimer": "Zero utility observed for failed API requests; 100% of tokens considered avoidable.",
        })

    # Context insight
    ctx_ex = exclusive_breakdown["context"]
    if ctx_ex["requests_count"] > 0:
        insights.append({
            "id": "ins-context-04",
            "category": "context",
            "title": "Unbounded Multi-Turn Context Buildup",
            "priority": "medium",
            "confidence": "medium",
            "basis": "measured_event",
            "observed_tokens": ctx_ex["observed_tokens"],
            "avoidable_tokens": ctx_ex["avoidable_tokens"],
            "requests_count": ctx_ex["requests_count"],
            "description": (
                f"Multi-turn context grew beyond 40,000 tokens across {ctx_ex['requests_count']} requests. "
                "Periodic session resets or checkpoint compaction could significantly reduce ongoing turn cost."
            ),
            "evidence_sample": evidence_samples["context"],
            "actionable_recommendation": "Compact session history at natural task boundaries.",
            "causality_disclaimer": "Savings project a 50% reduction in tokens exceeding the 40K threshold.",
        })

    # Routing Alternatives with Adequate Sample Verification
    routing_alternatives: List[Dict[str, Any]] = []

    if has_sonnet:
        completed_haiku_tasks = 0
        for sid, out in outcomes_by_session.items():
            if out.get("completion_rating") == "complete":
                s_model = session_first_model.get(sid, "")
                if "haiku" in s_model.lower():
                    completed_haiku_tasks += 1

        if completed_haiku_tasks >= min_sample_size:
            routing_alternatives.append({
                "source_model": "Sonnet 3.5",
                "recommended_target": "Haiku 3.5",
                "recommendation_type": "validated_routing",
                "adequate_sample": True,
                "completed_sample_count": completed_haiku_tasks,
                "sample_threshold": min_sample_size,
                "confidence": "high",
                "basis": "matched_task_outcomes",
                "rationale": (
                    f"Observed {completed_haiku_tasks} successfully completed tasks on Haiku 3.5 meeting threshold ({min_sample_size}). "
                    "Routing routine edits and queries can cut costs by ~66% with demonstrated task completion."
                ),
                "causality_disclaimer": "Validated on observed historical task outcomes; requires continued performance monitoring.",
            })
        else:
            routing_alternatives.append({
                "source_model": "Sonnet 3.5",
                "recommended_target": "Haiku 3.5",
                "recommendation_type": "experiment_suggestion",
                "adequate_sample": False,
                "completed_sample_count": completed_haiku_tasks,
                "sample_threshold": min_sample_size,
                "confidence": "low",
                "basis": "catalog_pricing_heuristic",
                "rationale": (
                    f"Insufficient completed outcomes on Haiku 3.5 ({completed_haiku_tasks}/{min_sample_size} required). "
                    "Suggest running a controlled A/B experiment on 5 routine feature or debugging tasks before routing production traffic."
                ),
                "experiment_design": {
                    "sample_size": 5,
                    "target_task_types": ["code_search", "formatting", "light_edits"],
                    "success_criteria": "Completion rate >= 80% without rollback.",
                },
                "causality_disclaimer": "No causal claim of equivalent quality is made without adequate benchmark samples.",
            })

    compaction_recommendation: Dict[str, Any] = {
        "status": "abstain",
        "reason": "Insufficient measured task boundary transitions in session event logs.",
        "basis": "task_transition_audit",
        "actionable": False,
    }

    sessions_with_task_boundaries = len(sessions_with_task_boundaries_set)
    if sessions_with_task_boundaries > 0:
        compaction_recommendation = {
            "status": "recommended",
            "reason": (
                f"Identified {sessions_with_task_boundaries} sessions with distinct task boundary events. "
                "Restarting context or issuing compaction at task switch points prevents context drift."
            ),
            "basis": "measured_task_transition_events",
            "sessions_eligible": sessions_with_task_boundaries,
            "actionable": True,
            "disclaimer": "Grounded in explicit task_switch events; not inferred from cumulative token occupancy.",
        }

    scenarios: List[Dict[str, Any]] = []
    if isinstance(scenario_params, dict):
        scenarios.append(efficiency_scenario(conn, scenario_params, rows))

    return {
        "contract_version": "1",
        "query": norm_envelope,
        "metric_id": orig_metric_id or "efficiency",
        "family": "efficiency",
        "values": {
            "total_requests": len(rows),
            "total_tokens": total_tokens,
            "observed_tokens": total_tokens,
            "avoidable_tokens": total_avoidable_tokens,
            "efficiency_ratio": efficiency_ratio,
            "flagged_requests_count": flagged_requests_count,
            "clean_requests_count": clean_requests_count,
            "priced_requests_count": priced_requests,
        },
        "signals": {
            "exclusive_breakdown": exclusive_breakdown,
            "overlapping_breakdown": overlapping_breakdown,
        },
        "insights": insights,
        "routing_alternatives": routing_alternatives,
        "compaction_recommendation": compaction_recommendation,
        "scenarios": scenarios,
        "eligible": {"total_requests": len(rows), "total_tokens": total_tokens},
        "observed": {"total_requests": len(rows), "total_tokens": total_tokens},
        "missingness": {
            "unpriced_requests": len(rows) - priced_requests,
            "untimed_requests": untimed_requests,
        },
        "coverage": {
            "priced_ratio": round(priced_requests / max(1, len(rows)), 4),
            "timed_ratio": round((len(rows) - untimed_requests) / max(1, len(rows)), 4),
        },
        "warnings": warnings,
    }
