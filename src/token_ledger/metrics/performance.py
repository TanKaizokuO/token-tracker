"""Performance and speed metrics calculation (WP14a/b).

Computes:
- Weighted TPS: sum(output_tokens) / sum(generation_seconds) across eligible requests.
- Per-request TPS: arithmetic mean and median.
- TTFT, generation duration, total duration, and queue stage latencies.
- Deterministic percentiles (p50, p90, p95, p99) via stated nearest-rank method.
- Independent sample sets for each latency stage (never conflated).
- Explicit timing basis separation (measured vs log_span_proxy never pooled).
- Multi-dimensional slicing (model, provider, route, task, context size, output size, time).
- Request-linked speed anomaly detection (TPS drops, TTFT latency spikes).
- Full counts and denominators for every slice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import sqlite3
import statistics
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

from token_ledger.query import filtered_request_rows, validate_query

PERCENTILE_METHOD = "nearest_rank"
BASELINE_DAYS = 30
MIN_BASELINE_SAMPLES = 5
MIN_TASK_SAMPLES = 5


def compute_percentiles(
    values: Sequence[float],
    method: str = PERCENTILE_METHOD,
    sorted_vals: Optional[Sequence[float]] = None,
) -> Dict[str, Optional[float]]:
    """Calculates deterministic summary percentiles and distribution statistics.

    Supported methods:
        - "nearest_rank": Standard ordinal ranking index ceil(P * N) - 1.
    """
    if not values and not sorted_vals:
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

    if sorted_vals is None:
        sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _rank_val(p: float) -> float:
        # p in [0.0, 1.0]
        if n == 1:
            return sorted_vals[0]
        # nearest rank index
        idx = max(0, min(n - 1, math.ceil(p * n) - 1))
        return sorted_vals[idx]

    return {
        "count": n,
        "mean": round(math.fsum(sorted_vals) / n, 4),
        "min": round(sorted_vals[0], 4),
        "max": round(sorted_vals[-1], 4),
        "p50": round(_rank_val(0.50), 4),
        "p90": round(_rank_val(0.90), 4),
        "p95": round(_rank_val(0.95), 4),
        "p99": round(_rank_val(0.99), 4),
        "method": method,
    }


_EMPTY_SLICE_METRICS: Dict[str, Any] = {
    "counts": {
        "total_requests": 0,
        "eligible_generation_requests": 0,
        "eligible_zero_output_requests": 0,
        "eligible_ttft_requests": 0,
        "eligible_total_duration_requests": 0,
        "eligible_queue_requests": 0,
        "unobserved_timing_requests": 0,
    },
    "denominators": {
        "sum_generation_seconds": 0.0,
        "sum_output_tokens": 0,
        "ttft_sample_count": 0,
        "total_duration_sample_count": 0,
    },
    "tps": {
        "weighted": None,
        "mean": None,
        "median": None,
        "p90": None,
        "p95": None,
        "min": None,
        "max": None,
    },
    "distributions": {
        "tps": {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "method": PERCENTILE_METHOD,
        },
        "ttft_ms": {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "method": PERCENTILE_METHOD,
        },
        "generation_duration_ms": {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "method": PERCENTILE_METHOD,
        },
        "duration_ms": {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "method": PERCENTILE_METHOD,
        },
        "queue_duration_ms": {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "status": "unavailable",
        },
    },
}


def _copy_slice_metrics(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "counts": dict(m["counts"]),
        "denominators": dict(m["denominators"]),
        "tps": dict(m["tps"]),
        "distributions": {k: dict(v) for k, v in m["distributions"].items()},
    }

def _classify_context_size(fresh_input: Optional[int], cache_read: Optional[int]) -> str:
    """Classifies context size into explicit discrete buckets, or unavailable."""
    if fresh_input is None or cache_read is None:
        return "unavailable"
    total_input = (fresh_input or 0) + (cache_read or 0)
    if total_input < 1000:
        return "0-1k"
    elif total_input < 8000:
        return "1k-8k"
    elif total_input < 32000:
        return "8k-32k"
    elif total_input < 128000:
        return "32k-128k"
    else:
        return "128k+"


def _classify_output_size(output_tokens: Optional[int]) -> str:
    """Classifies output token volume into discrete size tiers."""
    if output_tokens is None:
        return "unavailable"
    if output_tokens == 0:
        return "0"
    elif output_tokens <= 100:
        return "1-100"
    elif output_tokens <= 500:
        return "101-500"
    elif output_tokens <= 1000:
        return "501-1k"
    elif output_tokens <= 2000:
        return "1k-2k"
    else:
        return "2k+"


def _fetch_task_labels(
    conn: Optional[sqlite3.Connection],
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Tuple[str, bool, Optional[str], Optional[float]]]:
    """Retrieves explicit task labels for requests and sessions from database if available."""
    labels: Dict[str, Tuple[str, bool, Optional[str], Optional[float]]] = {}
    if conn is None:
        return labels

    req_ids = [r["canonical_id"] for r in rows if r.get("canonical_id")]
    sess_ids = [r["session_id"] for r in rows if r.get("session_id")]

    if not req_ids and not sess_ids:
        return labels

    try:
        cur = conn.cursor()
        # Request-level labels take precedence
        if req_ids:
            placeholders = ",".join("?" for _ in req_ids)
            cur.execute(
                f"SELECT entity_id, task_label, is_inferred, inference_method, inference_confidence FROM manual_settings_and_outcomes "
                f"WHERE entity_type = 'request' AND entity_id IN ({placeholders}) "
                f"AND task_label IS NOT NULL",
                req_ids,
            )
            for eid, tlabel, inferred, method, confidence in cur.fetchall():
                if tlabel:
                    labels[f"req:{eid}"] = (tlabel, bool(inferred), method, confidence)

        # Session-level labels fallback
        if sess_ids:
            placeholders = ",".join("?" for _ in sess_ids)
            cur.execute(
                f"SELECT entity_id, task_label, is_inferred, inference_method, inference_confidence FROM manual_settings_and_outcomes "
                f"WHERE entity_type = 'session' AND entity_id IN ({placeholders}) "
                f"AND task_label IS NOT NULL",
                sess_ids,
            )
            for eid, tlabel, inferred, method, confidence in cur.fetchall():
                if tlabel:
                    labels[f"sess:{eid}"] = (tlabel, bool(inferred), method, confidence)
    except Exception:
        # Table might not exist in isolated mock tests
        pass

    return labels


def calculate_slice_metrics(
    rows: Sequence[Dict[str, Any]],
    slice_cache: Optional[Dict[Tuple[int, ...], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Calculates throughput, latency stage distributions, and denominators for a slice."""
    if not rows:
        return _copy_slice_metrics(_EMPTY_SLICE_METRICS)

    cache_key: Optional[Tuple[int, ...]] = None
    if slice_cache is not None:
        cache_key = tuple(id(r) for r in rows)
        cached = slice_cache.get(cache_key)
        if cached is not None:
            return _copy_slice_metrics(cached)

    gen_eligible_count = 0
    per_req_tps_list: List[float] = []
    sum_out_tokens = 0
    sum_gen_ms = 0.0
    zero_output_count = 0
    ttft_list: List[float] = []
    duration_list: List[float] = []
    gen_dur_list: List[float] = []
    queue_list: List[float] = []
    unobserved_timing_count = 0

    for r in rows:
        tb = r.get("timing_basis")
        if tb == "none" or not tb:
            unobserved_timing_count += 1

        gen_ms = r.get("generation_duration_ms")
        out_tokens = r.get("output_tokens")
        if tb != "none" and gen_ms is not None and gen_ms > 0 and out_tokens is not None:
            gen_eligible_count += 1
            gen_ms_f = float(gen_ms)
            if out_tokens == 0:
                zero_output_count += 1
            sum_out_tokens += out_tokens
            sum_gen_ms += gen_ms_f
            gen_sec = gen_ms_f / 1000.0
            per_req_tps_list.append(out_tokens / gen_sec if gen_sec > 0 else 0.0)

        ttft = r.get("ttft_ms")
        if ttft is not None and ttft >= 0:
            ttft_list.append(float(ttft))

        dur = r.get("duration_ms")
        if dur is not None and dur >= 0:
            duration_list.append(float(dur))

        if gen_ms is not None and gen_ms > 0:
            gen_dur_list.append(float(gen_ms))

        q_ms = r.get("queue_ms")
        if q_ms is not None and q_ms >= 0:
            queue_list.append(float(q_ms))

    weighted_tps = None
    if sum_gen_ms > 0:
        weighted_tps = round(sum_out_tokens / (sum_gen_ms / 1000.0), 4)

    sorted_tps = sorted(per_req_tps_list)
    tps_dist = compute_percentiles(per_req_tps_list, sorted_vals=sorted_tps)

    sorted_ttft = sorted(ttft_list)
    ttft_dist = compute_percentiles(ttft_list, sorted_vals=sorted_ttft)

    sorted_dur = sorted(duration_list)
    duration_dist = compute_percentiles(duration_list, sorted_vals=sorted_dur)

    sorted_gen_dur = sorted(gen_dur_list)
    gen_dur_dist = compute_percentiles(gen_dur_list, sorted_vals=sorted_gen_dur)

    if queue_list:
        sorted_queue = sorted(queue_list)
        queue_dist = compute_percentiles(queue_list, sorted_vals=sorted_queue)
    else:
        queue_dist = {
            "count": 0,
            "mean": None,
            "min": None,
            "max": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "status": "unavailable",
        }

    res = {
        "counts": {
            "total_requests": len(rows),
            "eligible_generation_requests": gen_eligible_count,
            "eligible_zero_output_requests": zero_output_count,
            "eligible_ttft_requests": len(ttft_list),
            "eligible_total_duration_requests": len(duration_list),
            "eligible_queue_requests": len(queue_list),
            "unobserved_timing_requests": unobserved_timing_count,
        },
        "denominators": {
            "sum_generation_seconds": round(sum_gen_ms / 1000.0, 4),
            "sum_output_tokens": sum_out_tokens,
            "ttft_sample_count": len(ttft_list),
            "total_duration_sample_count": len(duration_list),
        },
        "tps": {
            "weighted": weighted_tps,
            "mean": tps_dist["mean"],
            "median": tps_dist["p50"],
            "p90": tps_dist["p90"],
            "p95": tps_dist["p95"],
            "min": tps_dist["min"],
            "max": tps_dist["max"],
        },
        "distributions": {
            "tps": tps_dist,
            "ttft_ms": ttft_dist,
            "generation_duration_ms": gen_dur_dist,
            "duration_ms": duration_dist,
            "queue_duration_ms": queue_dist,
        },
    }

    if slice_cache is not None and cache_key is not None:
        slice_cache[cache_key] = res
        return _copy_slice_metrics(res)
    return res


def _baseline_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    return tuple(row.get(key) for key in (
        "normalized_model", "provider", "account_alias", "access_route",
        "origin_harness", "timing_basis", "task_label",
    ))


def detect_speed_anomalies(
    rows: Sequence[Dict[str, Any]],
    history: Sequence[Dict[str, Any]],
    window: Dict[str, Any],
    slice_cache: Optional[Dict[Tuple[int, ...], Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Compare current requests to independent, identity-matched historical samples."""
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for row in history:
        groups.setdefault(_baseline_key(row), []).append(row)
    assessments: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    anomalies: List[Dict[str, Any]] = []
    for row in rows:
        key = _baseline_key(row)
        if key not in assessments:
            metrics = calculate_slice_metrics(groups.get(key, []), slice_cache=slice_cache)
            assessments[key] = {
                "model": key[0], "provider": key[1], "account_alias": key[2],
                "access_route": key[3], "origin_harness": key[4], "timing_basis": key[5],
                "task_label": key[6], "window": window, "method": PERCENTILE_METHOD,
                "minimum_samples": MIN_BASELINE_SAMPLES,
                "tps_samples": metrics["counts"]["eligible_generation_requests"],
                "ttft_samples": metrics["counts"]["eligible_ttft_requests"],
                "median_tps": metrics["tps"]["median"],
                "median_ttft_ms": metrics["distributions"]["ttft_ms"]["p50"],
            }
        baseline = assessments[key]
        evidence = {
            "canonical_id": row.get("canonical_id"), "session_id": row.get("session_id"),
            "request_timestamp": row.get("request_timestamp"), "model": key[0],
            "provider": key[1], "account_alias": key[2], "access_route": key[3],
            "origin_harness": key[4], "timing_basis": key[5], "task_label": key[6],
            "window": window, "method": PERCENTILE_METHOD,
            "minimum_samples": MIN_BASELINE_SAMPLES,
        }
        out = row.get("output_tokens")
        generation_ms = row.get("generation_duration_ms")
        base_tps = baseline["median_tps"]
        if (baseline["tps_samples"] >= MIN_BASELINE_SAMPLES and base_tps is not None
                and base_tps > 5 and out is not None and out >= 20
                and generation_ms is not None and generation_ms > 0):
            observed = out * 1000 / generation_ms
            if observed < base_tps * 0.5:
                anomalies.append({**evidence, "anomaly_type": "severe_tps_drop",
                    "observed_value": round(observed, 4), "baseline_value": base_tps,
                    "sample_count": baseline["tps_samples"], "threshold": base_tps * 0.5,
                    "output_tokens": out, "generation_duration_ms": generation_ms})
        ttft = row.get("ttft_ms")
        base_ttft = baseline["median_ttft_ms"]
        if (baseline["ttft_samples"] >= MIN_BASELINE_SAMPLES and base_ttft is not None
                and base_ttft > 0 and ttft is not None and ttft > 1000 and ttft > base_ttft * 2.5):
            anomalies.append({**evidence, "anomaly_type": "ttft_spike",
                "observed_value": ttft, "baseline_value": base_ttft,
                "sample_count": baseline["ttft_samples"], "threshold": max(1000, base_ttft * 2.5)})
    return anomalies, list(assessments.values())


def _baseline_history_start(start: str) -> str:
    """UTC start of the BASELINE_DAYS history window that ends at the selected range start."""
    end_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return (end_dt - timedelta(days=BASELINE_DAYS)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _history_query(norm_query: Dict[str, Any], history_start: str, start: str) -> Dict[str, Any]:
    tr = norm_query.get("time_range", {})
    return {**norm_query, "time_range": {
        **tr, "start": history_start, "end": start,
        "raw_start": history_start, "raw_end": start,
    }}


def _history_superset_query(norm_query: Dict[str, Any], history_start: str) -> Dict[str, Any]:
    """Single scan over [history_start, end); keeps the original end and raw end bounds."""
    tr = norm_query.get("time_range", {})
    return {**norm_query, "time_range": {**tr, "start": history_start, "raw_start": history_start}}


def _split_at_start(
    rows: List[Dict[str, Any]],
    start: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Splits a superset scan at the range start using the same text comparison as the SQL bound.

    Returns (rows in [start, end), rows in [history_start, start)), preserving scan order.
    """
    current: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    for row in rows:
        (history if row["request_timestamp"] < start else current).append(row)
    return current, history



def analyze(
    conn: Optional[sqlite3.Connection],
    envelope: Dict[str, Any],
    rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Main entry point for performance and speed analytics (WP14a/b).

    Conforms to the shared query engine envelope contract.
    Never pools measured vs log_span_proxy timing basis.
    """
    norm_query = envelope if "family" in envelope else validate_query(envelope)

    tr = norm_query.get("time_range", {})
    start = tr.get("start")
    history_start = _baseline_history_start(start) if conn is not None and start else None
    history: List[Dict[str, Any]] = []
    if rows is None:
        if conn is None:
            raise ValueError("Either an open sqlite3.Connection or pre-filtered rows must be provided.")
        if history_start is None:
            rows = filtered_request_rows(conn, norm_query)
        else:
            # One scan over [history_start, end) split at start returns the same rows as the
            # separate [start, end) and [history_start, start) scans.
            rows, history = _split_at_start(
                filtered_request_rows(conn, _history_superset_query(norm_query, history_start)), start)
    elif history_start is not None:
        history = filtered_request_rows(conn, _history_query(norm_query, history_start, start))

    warnings: List[str] = []

    # Check DST warning
    if tr.get("dst_transition"):
        warnings.append(
            f"Timezone transition (DST) occurs within requested range in {tr.get('timezone', 'UTC')}."
        )

    slice_cache: Dict[Tuple[int, ...], Dict[str, Any]] = {}

    # Separate timing basis: NEVER pool 'measured' with 'log_span_proxy' or 'estimated'
    measured_rows: List[Dict[str, Any]] = []
    proxy_rows: List[Dict[str, Any]] = []
    untimed_rows: List[Dict[str, Any]] = []
    for r in rows:
        tb = r.get("timing_basis")
        if tb == "measured":
            measured_rows.append(r)
        elif tb in ("log_span_proxy", "log_span", "estimated"):
            proxy_rows.append(r)
        else:
            untimed_rows.append(r)

    if proxy_rows and measured_rows:
        warnings.append(
            f"Dataset contains both measured ({len(measured_rows)}) and log_span_proxy ({len(proxy_rows)}) "
            "timings. These are strictly separated and never pooled."
        )

    # Primary analysis focuses on measured rows (or all if only proxy exist)
    primary_rows = measured_rows if measured_rows else (proxy_rows if proxy_rows else rows)
    primary_basis = "measured" if measured_rows else ("log_span_proxy" if proxy_rows else "none")

    overall_metrics = calculate_slice_metrics(primary_rows, slice_cache=slice_cache)

    window: Dict[str, Any] = {"start": None, "end": start, "days": BASELINE_DAYS}
    if history_start is not None:
        window["start"] = history_start
    else:
        warnings.append("Speed anomaly baseline unavailable without a database and selected range start.")

    # Apply the same explicit request/session task labels to both periods.
    combined_rows = list(rows) + history if history else rows
    db_labels = _fetch_task_labels(conn, combined_rows)
    if db_labels:
        for r in combined_rows:
            cid = r.get("canonical_id")
            sid = r.get("session_id")
            if not r.get("task_label") and not r.get("task"):
                label = db_labels.get(f"req:{cid}") or db_labels.get(f"sess:{sid}")
                if label:
                    r["task_label"], r["task_inferred"], r["task_inference_method"], r["task_inference_confidence"] = label

    # Dimension Slicing Helper
    def _slice_by(
        dataset: Sequence[Dict[str, Any]],
        key_fn: Callable[[Dict[str, Any]], str],
    ) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in dataset:
            k = key_fn(r)
            groups.setdefault(k, []).append(r)
        return {k: calculate_slice_metrics(g_rows, slice_cache=slice_cache) for k, g_rows in sorted(groups.items())}, groups

    # Slices across specified dimensions
    # 1. By Timing Basis (Explicit Separation)
    slices_by_timing_basis = {
        "measured": calculate_slice_metrics(measured_rows, slice_cache=slice_cache),
        "log_span_proxy": calculate_slice_metrics(proxy_rows, slice_cache=slice_cache),
        "untimed": calculate_slice_metrics(untimed_rows, slice_cache=slice_cache),
    }

    # Retain groups with no primary-basis timing and their full filtered
    # denominators; timing statistics still use the selected basis only.
    primary_ids = {id(r) for r in primary_rows}
    all_primary = len(primary_rows) == len(rows)
    def _timing_slices(key_fn: Callable[[Dict[str, Any]], Any]) -> Dict[Any, Any]:
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(key_fn(row), []).append(row)
        result = {}
        for key, group in sorted(groups.items()):
            selected = group if all_primary else [r for r in group if id(r) in primary_ids]
            metrics = calculate_slice_metrics(selected, slice_cache=slice_cache)
            metrics["counts"]["primary_basis_requests"] = metrics["counts"]["total_requests"]
            metrics["counts"]["total_requests"] = len(group)
            result[key] = metrics
        return result

    slices_by_model = _timing_slices(lambda r: r.get("normalized_model") or "unknown")
    slices_by_provider, _ = _slice_by(primary_rows, lambda r: r.get("provider") or "unknown")
    slices_by_route = _timing_slices(lambda r: r.get("access_route") or "unknown")
    slices_by_model_route: Dict[str, Dict[str, Any]] = {}
    for (model, route), metrics in _timing_slices(
        lambda r: (r.get("normalized_model") or "unknown", r.get("access_route") or "unknown")
    ).items():
        slices_by_model_route.setdefault(model, {})[route] = metrics

    # 5. By Task (explicit labels; unknown/unlabeled stays "unavailable", no inference occupancy)
    slices_by_task, task_groups = _slice_by(
        primary_rows,
        lambda r: r.get("task_label") or r.get("task") or "unavailable",
    )

    # 6. By Context Size
    slices_by_context, _ = _slice_by(
        primary_rows,
        lambda r: _classify_context_size(r.get("fresh_input_tokens"), r.get("cache_read_tokens")),
    )

    # 7. By Output Size
    slices_by_output_size, _ = _slice_by(
        primary_rows,
        lambda r: _classify_output_size(r.get("output_tokens")),
    )
    for task, metrics in slices_by_task.items():
        matching = task_groups.get(task, [])
        metrics["inferred_label_count"] = sum(bool(r.get("task_inferred")) for r in matching)

    # Model comparisons retain the full filtered denominator while timing metrics
    # use only the primary basis. Eligibility is independent for each metric.
    task_models: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("task_label") or row.get("task") or "unavailable",
               row.get("normalized_model") or "unknown")
        grouped.setdefault(key, []).append(row)
    for (task, model), group in sorted(grouped.items()):
        selected = group if all_primary else [r for r in group if id(r) in primary_ids]
        metrics = calculate_slice_metrics(selected, slice_cache=slice_cache)
        metrics["counts"]["total_requests"] = len(group)
        metrics["inferred_label_count"] = sum(bool(r.get("task_inferred")) for r in group)
        metrics["minimum_samples"] = MIN_TASK_SAMPLES
        valid_identity = task != "unavailable" and model != "unknown"
        metrics["comparison_status"] = ("eligible" if valid_identity and
            metrics["counts"]["eligible_generation_requests"] >= MIN_TASK_SAMPLES else "insufficient_samples")
        metrics["ttft_comparison_status"] = ("eligible" if valid_identity and
            metrics["counts"]["eligible_ttft_requests"] >= MIN_TASK_SAMPLES else "insufficient_samples")
        task_models.setdefault(task, {})[model] = metrics

    # Each task-level chart also respects the independent TTFT sample gate.
    for task, metrics in slices_by_task.items():
        metrics["ttft_comparison_status"] = ("eligible" if task != "unavailable" and
            metrics["counts"]["eligible_ttft_requests"] >= MIN_TASK_SAMPLES else "insufficient_samples")

    grain = next((g for g in norm_query.get("group_by", []) if g in ("hour", "day", "week", "month")), "day")
    zone = ZoneInfo(tr.get("timezone", "UTC"))
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not row.get("request_timestamp"):
            continue
        local = datetime.fromisoformat(row["request_timestamp"].replace("Z", "+00:00")).astimezone(zone)
        if grain == "hour":
            bucket_start = local.replace(minute=0, second=0, microsecond=0)
            bucket_end = (bucket_start.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(zone)
        else:
            bucket_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            if grain == "week":
                bucket_start -= timedelta(days=bucket_start.weekday())
            elif grain == "month":
                bucket_start = bucket_start.replace(day=1)
            if grain == "month":
                bucket_end = (bucket_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            else:
                bucket_end = bucket_start + timedelta(days=7 if grain == "week" else 1)
        key = bucket_start.isoformat()
        bucket = buckets.setdefault(key, {"start": key, "end": bucket_end.isoformat(), "rows": []})
        bucket["rows"].append(row)
    timeline = []
    for bucket in sorted(buckets.values(), key=lambda b: datetime.fromisoformat(b["start"]).timestamp()):
        if timeline and datetime.fromisoformat(timeline[-1]["end"]).timestamp() < datetime.fromisoformat(bucket["start"]).timestamp():
            timeline.append({"start": timeline[-1]["end"], "end": bucket["start"],
                             "weighted_tps": None, "eligible_requests": 0, "total_requests": 0})
        bucket_rows = bucket.pop("rows")
        selected = bucket_rows if all_primary else [r for r in bucket_rows if id(r) in primary_ids]
        metrics = calculate_slice_metrics(selected, slice_cache=slice_cache)
        timeline.append({**bucket, "weighted_tps": metrics["tps"]["weighted"],
                         "eligible_requests": metrics["counts"]["eligible_generation_requests"],
                         "total_requests": len(bucket_rows)})

    points = []
    for row in primary_rows:
        output, duration = row.get("output_tokens"), row.get("generation_duration_ms")
        tps = round(output * 1000 / duration, 4) if output is not None and duration is not None and duration > 0 and row.get("timing_basis") not in (None, "none") else None
        ttft = row.get("ttft_ms")
        if tps is None and (ttft is None or ttft < 0):
            continue
        fresh, cached = row.get("fresh_input_tokens"), row.get("cache_read_tokens")
        points.append({
            "canonical_id": row.get("canonical_id"), "session_id": row.get("session_id"),
            "request_timestamp": row.get("request_timestamp"), "model": row.get("normalized_model"),
            "provider": row.get("provider"), "access_route": row.get("access_route"),
            "origin_harness": row.get("origin_harness"), "timing_basis": row.get("timing_basis"),
            "output_tokens": output, "input_volume_proxy": fresh + cached if fresh is not None and cached is not None else None,
            "tps": tps, "ttft_ms": ttft if ttft is not None and ttft >= 0 else None,
            "api_equivalent_usd": row.get("api_equivalent_usd"), "pricing_status": row.get("pricing_status"),
        })

    anomalies, baselines = detect_speed_anomalies(primary_rows, history, window, slice_cache=slice_cache)
    for task, metrics in slices_by_task.items():
        metrics["minimum_samples"] = MIN_TASK_SAMPLES
        metrics["comparison_status"] = ("eligible" if task != "unavailable"
            and metrics["counts"]["eligible_generation_requests"] >= MIN_TASK_SAMPLES else "insufficient_samples")

    # Build response structure conforming to versioned envelope
    metric_id = norm_query.get("metric_id", "generation_speed_tps")
    return {
        "contract_version": "1",
        "query": norm_query,
        "metric_id": metric_id,
        "family": "speed",
        "primary_timing_basis": primary_basis,
        "values": {
            "generation_speed_tps_weighted": overall_metrics["tps"]["weighted"],
            "generation_speed_tps_mean": overall_metrics["tps"]["mean"],
            "generation_speed_tps_median": overall_metrics["tps"]["median"],
            "generation_speed_tps_p90": overall_metrics["tps"]["p90"],
            "generation_speed_tps_p95": overall_metrics["tps"]["p95"],
            "ttft_ms_mean": overall_metrics["distributions"]["ttft_ms"]["mean"],
            "ttft_ms_median": overall_metrics["distributions"]["ttft_ms"]["p50"],
            "ttft_ms_p90": overall_metrics["distributions"]["ttft_ms"]["p90"],
            "ttft_ms_p95": overall_metrics["distributions"]["ttft_ms"]["p95"],
            "duration_ms_mean": overall_metrics["distributions"]["duration_ms"]["mean"],
            "duration_ms_median": overall_metrics["distributions"]["duration_ms"]["p50"],
            "generation_duration_ms_mean": overall_metrics["distributions"]["generation_duration_ms"]["mean"],
            "generation_duration_ms_median": overall_metrics["distributions"]["generation_duration_ms"]["p50"],
            "queue_duration_ms": None,
            "queue_duration_status": "unavailable",
            "percentile_method": PERCENTILE_METHOD,
        },
        "units": {
            "generation_speed_tps_weighted": "tokens/sec",
            "generation_speed_tps_mean": "tokens/sec",
            "generation_speed_tps_median": "tokens/sec",
            "generation_speed_tps_p90": "tokens/sec",
            "generation_speed_tps_p95": "tokens/sec",
            "ttft_ms_mean": "milliseconds",
            "ttft_ms_median": "milliseconds",
            "ttft_ms_p90": "milliseconds",
            "ttft_ms_p95": "milliseconds",
            "duration_ms_mean": "milliseconds",
            "generation_duration_ms_mean": "milliseconds",
        },
        "distributions": overall_metrics["distributions"],
        "counts": overall_metrics["counts"],
        "denominators": overall_metrics["denominators"],
        "slices": {
            "timing_basis": slices_by_timing_basis,
            "model": slices_by_model,
            "provider": slices_by_provider,
            "route": slices_by_route,
            "model_route": slices_by_model_route,
            "task": slices_by_task,
            "context_size": slices_by_context,
            "output_size": slices_by_output_size,
        },
        "task_models": task_models,
        "timeline": {"grain": grain, "buckets": timeline},
        "points": points,
        "anomaly_baseline": {"window": window, "method": PERCENTILE_METHOD,
                             "minimum_samples": MIN_BASELINE_SAMPLES, "groups": baselines},
        "task_minimum_samples": MIN_TASK_SAMPLES,
        "anomalies": anomalies,
        "eligible": {
            "total_requests": len(rows),
            "timed_generation_requests": overall_metrics["counts"]["eligible_generation_requests"],
            "timed_ttft_requests": overall_metrics["counts"]["eligible_ttft_requests"],
            "timed_duration_requests": overall_metrics["counts"]["eligible_total_duration_requests"],
            "eligible_zero_output_requests": overall_metrics["counts"]["eligible_zero_output_requests"],
            "timing_basis_used": primary_basis,
        },
        "observed": {
            "measured_timing": len(measured_rows),
            "log_span_proxy_timing": len(proxy_rows),
            "unobserved_timing": len(untimed_rows),
        },
        "missingness": {
            "unobserved_timing_count": len(untimed_rows),
            "missing_ttft_count": len(rows) - overall_metrics["counts"]["eligible_ttft_requests"],
            "missing_generation_count": len(rows) - overall_metrics["counts"]["eligible_generation_requests"],
            "unlabeled_task_count": sum(1 for r in primary_rows if not r.get("task_label") and not r.get("task")),
        },
        "coverage": {
            "harness_scope": norm_query.get("harness_scope", "combined"),
            "measured_fraction": round(len(measured_rows) / len(rows), 4) if rows else 0.0,
            "timing_coverage_fraction": round((len(measured_rows) + len(proxy_rows)) / len(rows), 4) if rows else 0.0,
        },
        "warnings": warnings,
    }
