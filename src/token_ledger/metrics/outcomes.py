"""Outcome tracking, task efficiency, and effectiveness scoring metrics (WP18).

Implements:
1. Reuses `filtered_request_rows` to operate strictly on canonical requests.
2. Matches canonical requests/sessions to manual outcomes and task labels in `manual_settings_and_outcomes`.
3. Strict sample adequacy policy:
   - Configurable threshold (default: 3 completed tasks).
   - Abstains with values=None and clear disclaimers when sample size is below threshold.
   - Never invents adequacy or estimates per-completion metrics on sparse data.
4. Calculates:
   - tokens_per_completed_task
   - time_per_completed_task_seconds
   - requests_per_completed_task
   - cost_per_success_usd / cost_per_success_inr
   - completion_rate, correction_rate, restart_rate
5. Separates manual user ratings from automated inference confidence.
6. Multi-dimensional slices: by task type, model effectiveness, harness efficiency, and completion status.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import sqlite3
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from token_ledger.outcomes import (
    DEFAULT_MIN_SAMPLE_ADEQUACY,
    OUTCOMES_POLICY_VERSION,
    OutcomeRecord,
    get_outcome,
    list_outcomes,
)
from token_ledger.query import filtered_request_rows, validate_query


def _safe_median(values: Sequence[float]) -> Optional[float]:
    """Computes median safely for empty or single item lists."""
    if not values:
        return None
    return round(statistics.median(values), 4)


def _safe_mean(values: Sequence[float]) -> Optional[float]:
    """Computes arithmetic mean safely."""
    if not values:
        return None
    return round(statistics.mean(values), 4)


def _complete_token_sum(values: Iterable[Optional[int]]) -> Optional[int]:
    """Return a total only when every contributing token value is known."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _load_outcomes_for_entities(
    conn: sqlite3.Connection,
    session_ids: Sequence[str],
    canonical_ids: Sequence[str],
) -> Tuple[Dict[str, OutcomeRecord], Dict[str, OutcomeRecord]]:
    """Loads outcomes matching session and request IDs.

    Returns:
        (session_outcomes, request_outcomes) maps.
    """
    session_outcomes: Dict[str, OutcomeRecord] = {}
    request_outcomes: Dict[str, OutcomeRecord] = {}

    cur = conn.cursor()
    if session_ids:
        # Batch in chunks of 500 to honor SQLite host parameter limit
        for i in range(0, len(session_ids), 500):
            chunk = list(session_ids[i : i + 500])
            placeholders = ",".join("?" for _ in chunk)
            sql = f"""
                SELECT outcome_id, entity_type, entity_id, account_alias,
                       task_label, completion_rating, manual_rating, is_inferred,
                       inference_method, inference_confidence, git_commit_link,
                       notes, created_at, updated_at
                FROM manual_settings_and_outcomes
                WHERE entity_type = 'session' AND entity_id IN ({placeholders})
                ORDER BY updated_at ASC;
            """
            cur.execute(sql, chunk)
            for r in cur.fetchall():
                rec = OutcomeRecord.from_row(r)
                session_outcomes[rec.entity_id] = rec

    if canonical_ids:
        for i in range(0, len(canonical_ids), 500):
            chunk = list(canonical_ids[i : i + 500])
            placeholders = ",".join("?" for _ in chunk)
            sql = f"""
                SELECT outcome_id, entity_type, entity_id, account_alias,
                       task_label, completion_rating, manual_rating, is_inferred,
                       inference_method, inference_confidence, git_commit_link,
                       notes, created_at, updated_at
                FROM manual_settings_and_outcomes
                WHERE entity_type = 'request' AND entity_id IN ({placeholders})
                ORDER BY updated_at ASC;
            """
            cur.execute(sql, chunk)
            for r in cur.fetchall():
                rec = OutcomeRecord.from_row(r)
                request_outcomes[rec.entity_id] = rec

    return session_outcomes, request_outcomes


_ERROR_REQUEST_STATUSES = frozenset(("error", "interrupted", "rate_limited", "timeout"))


def _request_tokens(r: Dict[str, Any]) -> Optional[int]:
    """Returns a request's total tokens, or None when any component is unknown."""
    tt = r.get("total_tokens")
    if tt is not None:
        return int(tt)
    return _complete_token_sum((
        r.get("fresh_input_tokens"),
        r.get("cache_read_tokens"),
        r.get("cache_write_5m_tokens"),
        r.get("cache_write_1h_tokens"),
        r.get("cache_write_unknown_ttl_tokens"),
        r.get("output_tokens"),
    ))


def _build_task_units(
    rows: Sequence[Dict[str, Any]],
    session_outcomes: Dict[str, OutcomeRecord],
    request_outcomes: Dict[str, OutcomeRecord],
) -> List[Dict[str, Any]]:
    """Groups filtered requests into task units attributed to sessions and outcomes.

    Each session's requests are aggregated in a single pass.
    """
    # Group rows by session_id (or canonical_id if no session)
    sessions_map: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        sid = r.get("session_id") or f"unassigned_{r.get('canonical_id')}"
        sessions_map.setdefault(sid, []).append(r)

    task_units: List[Dict[str, Any]] = []

    for sid, s_rows in sessions_map.items():
        # Match outcome: check session outcome first, then the first request outcome in session
        outcome: Optional[OutcomeRecord] = session_outcomes.get(sid)
        request_task_label: Optional[str] = None
        request_tokens_total = 0
        tokens_complete = True
        output_values: List[Optional[int]] = []
        fresh_values: List[Optional[int]] = []
        cache_read_values: List[Optional[int]] = []
        durations: List[Optional[float]] = []
        retries: List[Optional[int]] = []
        priced_count = 0
        cost_usd_values: List[float] = []
        cost_inr_values: List[float] = []
        models: List[str] = []
        primary_harness: Optional[str] = None
        primary_provider: Optional[str] = None
        primary_route: Optional[str] = None
        has_error = False

        for r in s_rows:
            if outcome is None:
                cid = r.get("canonical_id")
                if cid and cid in request_outcomes:
                    outcome = request_outcomes[cid]
            # Fallback to request-level task_label: first truthy value
            if request_task_label is None and r.get("task_label"):
                request_task_label = r["task_label"]
            # Rollup request usage; stops at the first unknown request token total
            if tokens_complete:
                row_tokens = _request_tokens(r)
                if row_tokens is None:
                    tokens_complete = False
                else:
                    request_tokens_total += row_tokens
            output_values.append(r.get("output_tokens") or 0)
            fresh_values.append(r.get("fresh_input_tokens") or 0)
            cache_read_values.append(r.get("cache_read_tokens") or 0)
            durations.append(r.get("duration_ms"))
            retries.append(r.get("retry_attempt"))
            # Financial facts: pricing coverage across all requests in task
            if r.get("pricing_status") == "priced" and r.get("api_equivalent_usd") is not None:
                priced_count += 1
            cost_usd_values.append(r.get("api_equivalent_usd") or 0.0)
            cost_inr_values.append(r.get("api_equivalent_inr") or 0.0)
            model = r.get("normalized_model") or r.get("raw_model")
            if model:
                models.append(model)
            # Dominant harness/provider/route: first truthy value in session order
            if primary_harness is None and r.get("origin_harness"):
                primary_harness = r["origin_harness"]
            if primary_provider is None and r.get("provider"):
                primary_provider = r["provider"]
            if primary_route is None and r.get("access_route"):
                primary_route = r["access_route"]
            if r.get("request_status") in _ERROR_REQUEST_STATUSES:
                has_error = True

        task_label = (outcome.task_label if outcome and outcome.task_label else None) or request_task_label
        completion_status = outcome.completion_rating if outcome and outcome.completion_rating else "unlabeled"
        manual_rating = outcome.manual_rating if outcome and not outcome.is_inferred else None
        is_inferred = outcome.is_inferred if outcome else False
        inference_method = outcome.inference_method if outcome else None
        inference_confidence = outcome.inference_confidence if outcome else None
        was_corrected = outcome.was_corrected if outcome else False
        was_restarted = outcome.was_restarted if outcome else False
        user_ownership = outcome.user_ownership if outcome else ("inferred" if is_inferred else "unlabeled")
        git_commit_link = outcome.git_commit_link if outcome else None
        git_metadata = outcome.git_metadata if outcome else None

        request_count = len(s_rows)
        total_tokens = request_tokens_total if tokens_complete else None
        output_tokens = sum(output_values)
        fresh_input_tokens = sum(fresh_values)
        cache_read_tokens = sum(cache_read_values)
        duration_ms = sum(durations) if all(value is not None and value >= 0 for value in durations) else None
        retry_requests = sum(value > 0 for value in retries) if all(value is not None for value in retries) else None

        is_fully_priced = (priced_count == request_count) and (request_count > 0)
        cost_usd = sum(cost_usd_values) if is_fully_priced else None
        cost_inr = sum(cost_inr_values) if is_fully_priced else None

        # Dominant model: ties resolve in set iteration order, as before
        model_counts = Counter(models)
        primary_model = max(set(models), key=model_counts.__getitem__) if models else "unknown"
        primary_harness = primary_harness or "unknown"
        primary_provider = primary_provider or "unknown"
        primary_route = primary_route or "unknown"

        task_units.append({
            "task_id": sid,
            "session_id": sid,
            "task_label": task_label or "unlabeled",
            "completion_status": completion_status,
            "manual_rating": manual_rating,
            "is_inferred": is_inferred,
            "inference_method": inference_method,
            "inference_confidence": inference_confidence,
            "was_corrected": bool(was_corrected),
            "was_restarted": bool(was_restarted),
            "user_ownership": user_ownership,
            "git_commit_link": git_commit_link,
            "git_metadata": git_metadata,
            "total_tokens": total_tokens,
            "fresh_input_tokens": fresh_input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "retry_requests": retry_requests,
            "request_count": request_count,
            "is_fully_priced": is_fully_priced,
            "cost_usd": cost_usd,
            "cost_inr": cost_inr,
            "primary_model": primary_model,
            "primary_harness": primary_harness,
            "primary_provider": primary_provider,
            "primary_route": primary_route,
            "has_error": has_error,
        })

    return task_units


def _compute_slice_metrics(
    tasks: Sequence[Dict[str, Any]],
    min_sample_size: int,
) -> Dict[str, Any]:
    """Computes aggregate and per-completion metrics with explicit sample adequacy enforcement."""
    total_tasks = len(tasks)
    completed_tasks = [t for t in tasks if t["completion_status"] == "complete"]
    partial_tasks = [t for t in tasks if t["completion_status"] == "partial"]
    unsuccessful_tasks = [t for t in tasks if t["completion_status"] == "unsuccessful"]
    abandoned_tasks = [t for t in tasks if t["completion_status"] == "abandoned"]
    unlabeled_tasks = [t for t in tasks if t["completion_status"] == "unlabeled"]

    completed_count = len(completed_tasks)
    adequate_sample = completed_count >= min_sample_size

    # User ratings strictly on manual user-owned outcomes
    user_ratings = [t["manual_rating"] for t in tasks if t["manual_rating"] is not None and not t["is_inferred"]]
    # Inference confidence strictly on inferred outcomes
    inferred_confidences = [t["inference_confidence"] for t in tasks if t["inference_confidence"] is not None and t["is_inferred"]]

    total_corrections = sum(1 for t in tasks if t["was_corrected"])
    total_restarts = sum(1 for t in tasks if t["was_restarted"])

    total_tokens = _complete_token_sum(t["total_tokens"] for t in tasks)
    total_requests = sum(t["request_count"] for t in tasks)

    completion_rate = round(completed_count / total_tasks, 4) if total_tasks > 0 else None
    correction_rate = round(total_corrections / total_tasks, 4) if total_tasks > 0 else None
    restart_rate = round(total_restarts / total_tasks, 4) if total_tasks > 0 else None

    # Per-completion metrics: ONLY when sample adequacy threshold is satisfied
    if adequate_sample:
        completed_tokens = _complete_token_sum(t["total_tokens"] for t in completed_tasks)
        completed_requests = sum(t["request_count"] for t in completed_tasks)
        timed_completed = [t for t in completed_tasks if t["duration_ms"] is not None]
        completed_duration_ms = sum(t["duration_ms"] for t in timed_completed)

        tokens_per_completed_task = round(completed_tokens / completed_count, 2) if completed_tokens is not None else None
        requests_per_completed_task = round(completed_requests / completed_count, 2)
        time_per_completed_task_seconds = round(completed_duration_ms / (completed_count * 1000.0), 3) if len(timed_completed) == completed_count else None

        # Cost per success: requires priced coverage
        priced_completed = [t for t in completed_tasks if t["is_fully_priced"] and t["cost_usd"] is not None]
        if len(priced_completed) == completed_count:
            cost_per_success_usd = round(sum(t["cost_usd"] for t in priced_completed) / completed_count, 4)
            cost_per_success_inr = round(sum(t["cost_inr"] for t in priced_completed) / completed_count, 4)
        else:
            cost_per_success_usd = None
            cost_per_success_inr = None

        abstention_reason = None
    else:
        tokens_per_completed_task = None
        requests_per_completed_task = None
        time_per_completed_task_seconds = None
        cost_per_success_usd = None
        cost_per_success_inr = None
        abstention_reason = (
            f"Sparse sample size ({completed_count} completed tasks) is below declared adequacy threshold ({min_sample_size}). "
            "Comparative effectiveness and completion metrics are withheld."
        )

    return {
        "total_tasks": total_tasks,
        "completed_tasks": completed_count,
        "partial_tasks": len(partial_tasks),
        "unsuccessful_tasks": len(unsuccessful_tasks),
        "abandoned_tasks": len(abandoned_tasks),
        "unlabeled_tasks": len(unlabeled_tasks),
        "completion_rate": completion_rate,
        "adequate_sample": adequate_sample,
        "adequacy_threshold": min_sample_size,
        "abstention_reason": abstention_reason,
        "tokens_per_completed_task": tokens_per_completed_task,
        "requests_per_completed_task": requests_per_completed_task,
        "time_per_completed_task_seconds": time_per_completed_task_seconds,
        "timed_completed_tasks": sum(t["duration_ms"] is not None for t in completed_tasks),
        "retry_covered_completed_tasks": sum(t.get("retry_requests") is not None for t in completed_tasks),
        "retry_requests_per_completed_task": (
            sum(t["retry_requests"] for t in completed_tasks) / completed_count
            if adequate_sample and all(t.get("retry_requests") is not None for t in completed_tasks) else None
        ),
        "cost_per_success_usd": cost_per_success_usd,
        "cost_per_success_inr": cost_per_success_inr,
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "user_ratings_count": len(user_ratings),
        "user_ratings_mean": _safe_mean(user_ratings),
        "user_ratings_median": _safe_median(user_ratings),
        "user_ratings_min": min(user_ratings) if user_ratings else None,
        "user_ratings_max": max(user_ratings) if user_ratings else None,
        "inferred_outcomes_count": sum(1 for t in tasks if t["is_inferred"]),
        "user_owned_outcomes_count": sum(1 for t in tasks if not t["is_inferred"] and t["completion_status"] != "unlabeled"),
        "inference_confidence_mean": _safe_mean(inferred_confidences),
        "inference_confidence_median": _safe_median(inferred_confidences),
        "total_corrections": total_corrections,
        "correction_rate": correction_rate,
        "total_restarts": total_restarts,
        "restart_rate": restart_rate,
    }


def _request_reliability(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Explicit canonical request statuses only; tool failures are independent evidence."""
    statuses = ("ok", "error", "rate_limited", "timeout", "interrupted")
    counts = dict.fromkeys(statuses, 0)
    retry_covered = 0
    retries = 0
    for row in rows:
        status = row.get("request_status")
        if status in counts:
            counts[status] += 1
        retry_attempt = row.get("retry_attempt")
        if retry_attempt is not None:
            retry_covered += 1
        if (retry_attempt or 0) > 0:
            retries += 1
    covered = sum(counts.values())
    return {
        "eligible_requests": len(rows),
        "status_covered_requests": covered,
        "unknown_status_requests": len(rows) - covered,
        "coverage": "exact" if covered == len(rows) and rows else "partial" if covered else "unavailable",
        "rate_denominator": "status_covered_requests",
        "statuses": {status: {"count": count, "rate": count / covered if covered else None} for status, count in counts.items()},
        "failed_requests": {"count": covered - counts["ok"], "rate": (covered - counts["ok"]) / covered if covered else None},
        "retry_requests": {"count": retries if retry_covered else None, "rate": retries / retry_covered if retry_covered else None},
        "retry_covered_requests": retry_covered,
        "retry_unknown_requests": len(rows) - retry_covered,
    }


def analyze(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Main entry point for Outcome Tracking and Effectiveness Scoring (WP18).

    Guarantees:
    - Shared filter selection reuses canonical filtered_request_rows.
    - Strictly abstains when matched tasks or outcomes are sparse (declaring the threshold).
    - Preserves distinct user ownership vs inference method/confidence.
    - Slices across task types, models, harnesses, and completion statuses.
    """
    envelope_copy = dict(envelope)
    raw_min_sample = envelope_copy.pop("min_sample_size", None)
    orig_metric_id = envelope_copy.get("metric_id")
    if orig_metric_id and orig_metric_id not in {"summary", "timeline", "models", "projects", "sessions", "requests", "events", "coverage"}:
        envelope_copy["metric_id"] = "summary"
    norm_query = validate_query(envelope_copy)
    if orig_metric_id:
        norm_query["metric_id"] = orig_metric_id
    rows = filtered_request_rows(conn, norm_query)
    # Configurable sample adequacy threshold (selected in query envelope or default)
    min_sample_size = raw_min_sample or DEFAULT_MIN_SAMPLE_ADEQUACY
    try:
        min_sample_size = max(1, int(min_sample_size))
    except (ValueError, TypeError):
        min_sample_size = DEFAULT_MIN_SAMPLE_ADEQUACY
    warnings: List[str] = []

    if not rows:
        metric_id = norm_query.get("metric_id", "outcomes")
        return {
            "contract_version": "1",
            "query": norm_query,
            "metric_id": metric_id,
            "family": "outcomes",
            "sample_adequacy_threshold": min_sample_size,
            "request_reliability": _request_reliability(rows),
            "values": {
                "total_tasks": 0,
                "completed_tasks": 0,
                "partial_tasks": 0,
                "unsuccessful_tasks": 0,
                "abandoned_tasks": 0,
                "unlabeled_tasks": 0,
                "completion_rate": None,
                "adequate_sample": False,
                "abstention_reason": "No matching requests found in active filter scope.",
                "tokens_per_completed_task": None,
                "requests_per_completed_task": None,
                "time_per_completed_task_seconds": None,
                "cost_per_success_usd": None,
                "cost_per_success_inr": None,
                "user_ratings_count": 0,
                "user_ratings_mean": None,
                "user_ratings_median": None,
                "user_owned_count": 0,
                "inferred_count": 0,
                "total_corrections": 0,
                "total_restarts": 0,
                "correction_rate": None,
                "restart_rate": None,
            },
            "slices": {
                "task_type": {},
                "model": {},
                "harness": {},
                "completion_status": {},
                "inference_method": {},
            },
            "tasks": [],
            "eligible": {"total_requests": 0, "total_sessions": 0},
            "observed": {"completed_tasks": 0, "user_owned_tasks": 0, "inferred_tasks": 0},
            "missingness": {
                "unlabeled_task_count": 0,
                "unrated_tasks_count": 0,
                "unpriced_tasks_count": 0,
            },
            "coverage": {
                "outcome_coverage_fraction": 0.0,
                "manual_task_fraction": 0.0,
            },
            "warnings": ["No canonical requests matched the specified filters."],
        }

    # 1. Fetch matching outcomes for all sessions and requests in scope
    session_set: Set[str] = set()
    canonical_set: Set[str] = set()
    for r in rows:
        if r.get("session_id"):
            session_set.add(r["session_id"])
        if r.get("canonical_id"):
            canonical_set.add(r["canonical_id"])
    session_ids = list(session_set)
    canonical_ids = list(canonical_set)

    session_outcomes, request_outcomes = _load_outcomes_for_entities(
        conn, session_ids, canonical_ids
    )

    # 2. Build structured task units
    tasks = _build_task_units(rows, session_outcomes, request_outcomes)

    # 3. Overall Aggregate Metrics
    overall = _compute_slice_metrics(tasks, min_sample_size)
    if not overall["adequate_sample"]:
        warnings.append(
            f"Overall completed tasks sample size ({overall['completed_tasks']}) is below declared threshold "
            f"({min_sample_size}). Per-completion efficiency metrics withheld."
        )

    # 4. Slices: By Task Type
    slices_by_task: Dict[str, Any] = {}
    task_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        task_groups.setdefault(t["task_label"], []).append(t)
    for label, group in task_groups.items():
        slices_by_task[label] = _compute_slice_metrics(group, min_sample_size)

    # 5. Slices: By Model (Model Effectiveness Scoring - WP18 / Rebuild Plan Row 43)
    slices_by_model: Dict[str, Any] = {}
    model_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        model_groups.setdefault(t["primary_model"], []).append(t)
    for model_name, group in model_groups.items():
        m_slice = _compute_slice_metrics(group, min_sample_size)
        if not m_slice["adequate_sample"]:
            m_slice["comparison_disclaimer"] = (
                f"Sparse sample size ({m_slice['completed_tasks']} completed tasks) for model '{model_name}'. "
                "No comparative model effectiveness claim can be made."
            )
        slices_by_model[model_name] = m_slice

    # 6. Slices: By Harness (Harness Efficiency - WP18 / Rebuild Plan Row 73)
    slices_by_harness: Dict[str, Any] = {}
    harness_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        harness_groups.setdefault(t["primary_harness"], []).append(t)
    for h_name, group in harness_groups.items():
        h_slice = _compute_slice_metrics(group, min_sample_size)
        if not h_slice["adequate_sample"]:
            h_slice["efficiency_status"] = "insufficient outcome evidence"
        else:
            h_slice["efficiency_status"] = "adequate"
        slices_by_harness[h_name] = h_slice

    # 7. Slices: By Completion Status
    slices_by_status: Dict[str, Any] = {}
    status_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        status_groups.setdefault(t["completion_status"], []).append(t)
    for st_name, group in status_groups.items():
        slices_by_status[st_name] = {
            "count": len(group),
            "total_tokens": _complete_token_sum(t["total_tokens"] for t in group),
            "total_requests": sum(t["request_count"] for t in group),
            "fraction_of_tasks": round(len(group) / len(tasks), 4),
        }

    # 8. Slices: By Inference Method
    slices_by_method: Dict[str, Any] = {}
    method_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        method_name = t["inference_method"] if t["is_inferred"] else "manual_user"
        method_groups.setdefault(method_name, []).append(t)
    for m_name, group in method_groups.items():
        confidences = [t["inference_confidence"] for t in group if t["inference_confidence"] is not None]
        slices_by_method[m_name] = {
            "count": len(group),
            "fraction": round(len(group) / len(tasks), 4),
            "confidence_mean": _safe_mean(confidences),
            "confidence_median": _safe_median(confidences),
        }

    # Missingness & Coverage stats
    unlabeled_count = sum(1 for t in tasks if t["task_label"] == "unlabeled")
    unrated_count = sum(1 for t in tasks if t["manual_rating"] is None)
    unpriced_completed_count = sum(1 for t in tasks if t["completion_status"] == "complete" and not t["is_fully_priced"])
    user_owned_tasks_count = sum(1 for t in tasks if not t["is_inferred"] and t["completion_status"] != "unlabeled")

    if unpriced_completed_count > 0:
        warnings.append(
            f"{unpriced_completed_count} completed tasks contain unpriced requests. "
            "cost_per_success_usd is unavailable for those items."
        )

    metric_id = norm_query.get("metric_id", "outcomes")

    return {
        "contract_version": "1",
        "query": norm_query,
        "metric_id": metric_id,
        "family": "outcomes",
        "sample_adequacy_threshold": min_sample_size,
        "request_reliability": _request_reliability(rows),
        "values": {
            "total_tasks": overall["total_tasks"],
            "completed_tasks": overall["completed_tasks"],
            "partial_tasks": overall["partial_tasks"],
            "unsuccessful_tasks": overall["unsuccessful_tasks"],
            "abandoned_tasks": overall["abandoned_tasks"],
            "unlabeled_tasks": overall["unlabeled_tasks"],
            "completion_rate": overall["completion_rate"],
            "adequate_sample": overall["adequate_sample"],
            "abstention_reason": overall["abstention_reason"],
            "tokens_per_completed_task": overall["tokens_per_completed_task"],
            "requests_per_completed_task": overall["requests_per_completed_task"],
            "time_per_completed_task_seconds": overall["time_per_completed_task_seconds"],
            "timed_completed_tasks": overall["timed_completed_tasks"],
            "retry_covered_completed_tasks": overall["retry_covered_completed_tasks"],
            "retry_requests_per_completed_task": overall["retry_requests_per_completed_task"],
            "cost_per_success_usd": overall["cost_per_success_usd"],
            "cost_per_success_inr": overall["cost_per_success_inr"],
            "user_ratings_count": overall["user_ratings_count"],
            "user_ratings_mean": overall["user_ratings_mean"],
            "user_ratings_median": overall["user_ratings_median"],
            "user_owned_count": user_owned_tasks_count,
            "inferred_count": overall["inferred_outcomes_count"],
            "total_corrections": overall["total_corrections"],
            "total_restarts": overall["total_restarts"],
            "correction_rate": overall["correction_rate"],
            "restart_rate": overall["restart_rate"],
        },
        "slices": {
            "task_type": slices_by_task,
            "model": slices_by_model,
            "harness": slices_by_harness,
            "completion_status": slices_by_status,
            "inference_method": slices_by_method,
        },
        "tasks": tasks,
        "eligible": {
            "total_requests": len(rows),
            "total_sessions": len(session_ids),
            "total_tasks": len(tasks),
            "completed_tasks": overall["completed_tasks"],
        },
        "observed": {
            "completed_tasks": overall["completed_tasks"],
            "user_owned_tasks": user_owned_tasks_count,
            "inferred_tasks": overall["inferred_outcomes_count"],
            "rated_tasks": overall["user_ratings_count"],
        },
        "missingness": {
            "unlabeled_task_count": unlabeled_count,
            "unrated_tasks_count": unrated_count,
            "unpriced_completed_tasks_count": unpriced_completed_count,
        },
        "coverage": {
            "outcome_coverage_fraction": round((len(tasks) - unlabeled_count) / len(tasks), 4) if tasks else 0.0,
            "manual_task_fraction": round(user_owned_tasks_count / len(tasks), 4) if tasks else 0.0,
        },
        "warnings": warnings,
    }
