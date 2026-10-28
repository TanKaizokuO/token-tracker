"""Core query engine for Token Ledger.

Implements WP08 shared query interface:
- validate_query(envelope) -> normalized_envelope
- execute_query(conn, envelope) -> response_dict

Key principles enforced:
1. Strict contract versioning (version='1') and unknown field rejection.
2. Half-open instant boundaries [start, end) with accurate IANA timezone / DST handling via zoneinfo.
3. Global combined filters (model, provider, project, session, route, task, application).
   - 'task' matches explicit manual labels in manual_settings_and_outcomes.
4. Canonical exclusive session counts (COUNT(DISTINCT session_id)).
5. Unresolved overlap detection: combined total is flagged as partial/unresolved, never silently exact sum.
6. Null-aware disjoint billable counters (6 counters + reported reasoning subset).
7. Cost / priced coverage via financial_facts (separating unpriced from measured zero).
8. Timed eligibility separated: generation_speed_tps uses only eligible timed requests; untimed requests are never assigned zero speed.
9. Mutually exclusive archive combination at supported grain; granular request/session details are never invented for archive records.
10. Allowlisted sorting, pagination, and parameterized text search.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from token_ledger.metrics import (
    METRIC_REGISTRY,
    MetricDefinition,
    get_metric_definition,
)
from token_ledger.pricing import (
    CANONICAL_DISPLAY_IDENTITIES,
    resolve_versioned_model_identity,
    _QueryModelIdentityResolver,
)

# Standard units dictionary
STANDARD_UNITS: Dict[str, str] = {
    "total_tokens": "tokens",
    "fresh_input_tokens": "tokens",
    "cache_read_tokens": "tokens",
    "cache_write_5m_tokens": "tokens",
    "cache_write_1h_tokens": "tokens",
    "cache_write_unknown_ttl_tokens": "tokens",
    "output_tokens": "tokens",
    "reasoning_tokens": "tokens",
    "requests_count": "count",
    "sessions_count": "count",
    "api_equivalent_usd": "USD",
    "api_equivalent_inr": "INR",
    "actual_billed_amount": "USD",
    "cache_savings_usd": "USD",
    "cache_hit_rate": "percentage",
    "generation_speed_tps": "tps",
    "ttft_ms": "ms",
    "duration_ms": "ms",
    "session_duration_ms": "ms",
}

# Allowlisted top-level envelope fields
ALLOWED_ENVELOPE_KEYS: Set[str] = {
    "contract_version",
    "harness_scope",
    "time_range",
    "filters",
    "metric_id",
    "group_by",
    "sort",
    "pagination",
    "coverage",
    "search",
    "include_archive",
    "simulator_parameters",
    "min_sample_size",
    "heuristic_window_minutes",
    "interval_minutes",
    "family",
    "scenario_inputs",
    "anomaly_settings",
    "comparison_periods",
    "period_a",
    "period_b",
    "threshold_multiplier",
    "timezone",
    "history_window",
}

# Allowlisted filter keys
ALLOWED_FILTER_KEYS: Set[str] = {
    "model",
    "provider",
    "project",
    "repository",
    "session",
    "route",
    "task",
    "application",
    "subscription",
    "duration",
}

# Allowlisted sort fields per family
ALLOWED_SORT_FIELDS: Dict[str, Set[str]] = {
    "summary": {"total_tokens", "requests_count", "api_equivalent_usd"},
    "timeline": {"bucket", "timestamp", "total_tokens", "requests_count", "api_equivalent_usd"},
    "models": {
        "model",
        "display_name",
        "total_tokens",
        "output_tokens",
        "fresh_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "requests_count",
        "sessions_count",
        "api_equivalent_usd",
        "generation_speed_tps",
    },
    "projects": {
        "project_id",
        "project_name",
        "total_tokens",
        "api_equivalent_usd",
        "sessions_count",
        "requests_count",
    },
    "sessions": {
        "session_id",
        "started_at",
        "duration_ms",
        "total_tokens",
        "api_equivalent_usd",
        "requests_count",
    },
    "requests": {
        "canonical_id",
        "request_timestamp",
        "duration_ms",
        "ttft_ms",
        "generation_speed_tps",
        "total_tokens",
        "output_tokens",
        "fresh_input_tokens",
        "api_equivalent_usd",
    },
    "events": {
        "event_id",
        "started_at",
        "duration_ms",
        "output_size_bytes",
        "event_kind",
        "event_name",
    },
    "coverage": {
        "start_time",
        "end_time",
        "grain",
        "source_name",
        "total_tokens",
    },
    "tokens": {"total_tokens", "fresh_input_tokens", "cache_read_tokens", "output_tokens", "requests_count", "sessions_count", "api_equivalent_usd", "group_key"},
    "cache": {"total_tokens", "fresh_input_tokens", "cache_read_tokens", "output_tokens", "requests_count", "sessions_count", "api_equivalent_usd", "group_key"},
    "speed": {"weighted_tps", "mean_tps", "median_tps", "median_ttft_ms", "total_requests", "sum_output_tokens"},
    "performance": {"weighted_tps", "mean_tps", "median_tps", "median_ttft_ms", "total_requests", "sum_output_tokens"},
    "traces": {"total_tokens", "total_requests", "total_sessions", "cumulative_input_tokens", "session_id", "timestamp"},
    "attribution": {"total_tokens", "total_requests", "total_sessions", "cumulative_input_tokens", "session_id", "timestamp"},
    "providers": {"total_tokens", "requests_count", "api_equivalent_usd", "provider", "account_alias"},
    "quotas": {"provider", "percent_used", "reset_time", "limit_value"},
    "burn": {"current_burn_rate_tpm", "typical_burn_rate_tpm", "ratio_vs_baseline", "provider", "account_alias"},
    "live": {"started_at", "last_active_at", "total_tokens", "session_id"},
    "budgets": {"name", "frequency", "updated_at", "created_at"},
    "model_profiles": {"model", "display_name", "total_tokens", "cost_usd", "requests_count", "weighted_tps", "median_ttft_ms"},
    "outcomes": {"completion_rate", "tokens_per_completed_task", "cost_per_success_usd", "user_ratings_mean", "total_tasks", "task_label"},
    "task_outcomes": {"completion_rate", "tokens_per_completed_task", "cost_per_success_usd", "user_ratings_mean", "total_tasks", "task_label"},
    "efficiency": {"total_tokens", "potential_savings_usd", "efficiency_score"},
}

SUPPORTED_HARNESS_SCOPES: Set[str] = {"combined", "claude-code", "omp"}


def parse_and_validate_iso_instant(
    dt_str: str,
    default_tz: ZoneInfo,
    is_end: bool = False,
) -> Tuple[datetime, str]:
    """Parses a date or timestamp string into a timezone-aware datetime and its UTC ISO representation.
    
    If given a bare date 'YYYY-MM-DD', treats it as midnight (00:00:00) in default_tz.
    Returns (dt_aware, utc_iso_str).
    """
    dt_str = dt_str.strip()
    # Check if bare date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", dt_str):
        d = datetime.strptime(dt_str, "%Y-%m-%d").date()
        dt_local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=default_tz)
        dt_utc = dt_local.astimezone(timezone.utc)
        return dt_local, dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Has timestamp
    # Replace trailing Z with +00:00 for fromisoformat
    iso_clean = dt_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_clean)
    except Exception as exc:
        raise ValueError(f"Invalid timestamp format: '{dt_str}'") from exc

    if dt.tzinfo is None:
        dt_local = dt.replace(tzinfo=default_tz)
    else:
        dt_local = dt

    dt_utc = dt_local.astimezone(timezone.utc)
    return dt_local, dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


_OPTION_KEYS_BY_FAMILY: Dict[str, Set[str]] = {
    "tokens": {"simulator_parameters"},
    "efficiency": {"min_sample_size", "scenario_inputs"},
    "outcomes": {"min_sample_size"},
    "burn": {"interval_minutes"},
    "live": {"heuristic_window_minutes"},
    "forecasts": {"timezone", "history_window"},
    "anomalies": {"anomaly_settings", "threshold_multiplier"},
    "comparisons": {"comparison_periods", "period_a", "period_b", "timezone"},
}


def _as_finite_number(value: Any, name: str, minimum: float, maximum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be >= {minimum}{upper}.")
    return number


def _as_positive_integer(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}.")
    return value


def _validate_simulator_parameters(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("simulator_parameters must be an object.")
    allowed = {
        "fresh_rate_per_m", "read_rate_per_m", "write_5m_rate_per_m",
        "write_1h_rate_per_m", "reuse_count", "token_count",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown simulator_parameters: {sorted(unknown)}")

    params = dict(value)
    for key in ("fresh_rate_per_m", "read_rate_per_m", "write_5m_rate_per_m", "write_1h_rate_per_m", "reuse_count"):
        if key in params:
            params[key] = _as_finite_number(params[key], f"simulator_parameters.{key}", 0)
    fresh_rate = params.get("fresh_rate_per_m")
    read_rate = params.get("read_rate_per_m")
    if fresh_rate is not None and fresh_rate <= 0:
        raise ValueError("simulator_parameters.fresh_rate_per_m must be greater than 0.")
    if fresh_rate is not None and read_rate is not None and read_rate > fresh_rate:
        raise ValueError("simulator_parameters.read_rate_per_m cannot exceed fresh_rate_per_m.")
    for key in ("write_5m_rate_per_m", "write_1h_rate_per_m"):
        if fresh_rate is not None and key in params and params[key] < fresh_rate:
            raise ValueError(f"simulator_parameters.{key} cannot be less than fresh_rate_per_m.")
    return params


def _validate_scenario_inputs(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("scenario_inputs must be an object.")
    allowed = {
        "scenario_type", "target_model", "source_model", "source_provider",
        "source_route", "target_provider", "target_route", "context_threshold_tokens",
        "cache_expansion_pct", "output_reduction_pct",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown scenario_inputs: {sorted(unknown)}")

    params = dict(value)
    if "scenario_type" in params and params["scenario_type"] not in {
        "model-switch", "cache-read", "output-reduction", "longcontext",
    }:
        raise ValueError("scenario_inputs.scenario_type is not supported.")
    for key in ("source_model", "target_model", "source_provider", "source_route", "target_provider", "target_route"):
        if key in params and (not isinstance(params[key], str) or not params[key].strip()):
            raise ValueError(f"scenario_inputs.{key} must be a non-empty string.")
    if "context_threshold_tokens" in params:
        _as_positive_integer(
            params["context_threshold_tokens"],
            "scenario_inputs.context_threshold_tokens",
            minimum=0,
        )
    for key in ("cache_expansion_pct", "output_reduction_pct"):
        if key in params:
            _as_finite_number(params[key], f"scenario_inputs.{key}", 0, 100)
    return params


def _validate_anomaly_settings(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("anomaly_settings must be an object.")
    unknown = set(value) - {"expected_anomalies"}
    if unknown:
        raise ValueError(f"Unknown anomaly_settings: {sorted(unknown)}")
    declared = value.get("expected_anomalies", [])
    if not isinstance(declared, list):
        raise ValueError("anomaly_settings.expected_anomalies must be a list.")
    normalized: List[Union[str, Dict[str, str]]] = []
    for index, item in enumerate(declared):
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
            continue
        if not isinstance(item, dict) or set(item) - {"id", "notes"}:
            raise ValueError(f"anomaly_settings.expected_anomalies[{index}] must be an ID string or an object with id and notes.")
        identifier = item.get("id")
        notes = item.get("notes", "Declared in query settings")
        if not isinstance(identifier, str) or not identifier.strip() or not isinstance(notes, str):
            raise ValueError(f"anomaly_settings.expected_anomalies[{index}] has invalid id or notes.")
        normalized.append({"id": identifier.strip(), "notes": notes})
    return {"expected_anomalies": normalized}


def _validate_history_window(value: Any, default_timezone: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("history_window must be an object.")
    unknown = set(value) - {"start", "end", "timezone"}
    if unknown:
        raise ValueError(f"Unknown fields in history_window: {sorted(unknown)}")

    timezone_name = value.get("timezone", default_timezone)
    if not isinstance(timezone_name, str):
        raise ValueError("history_window.timezone must be a string.")
    try:
        history_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid timezone name in history_window: '{timezone_name}'") from exc

    normalized: Dict[str, Any] = {
        "start": None,
        "end": None,
        "timezone": timezone_name,
    }
    for boundary in ("start", "end"):
        raw_value = value.get(boundary)
        if raw_value is not None:
            if not isinstance(raw_value, str):
                raise ValueError(f"history_window.{boundary} must be an ISO string.")
            _, normalized[boundary] = parse_and_validate_iso_instant(raw_value, history_zone)
    if (
        normalized["start"] is not None
        and normalized["end"] is not None
        and normalized["start"] >= normalized["end"]
    ):
        raise ValueError("history_window.start must be earlier than history_window.end.")
    return normalized



def _validate_comparison_period(value: Any, name: str, timezone_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    unknown = set(value) - {"start", "end", "timezone", "label"}
    if unknown:
        raise ValueError(f"Unknown fields in {name}: {sorted(unknown)}")
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError(f"{name} must specify ISO string start and end values.")
    period_timezone = value.get("timezone", timezone_name)
    if not isinstance(period_timezone, str):
        raise ValueError(f"{name}.timezone must be a string.")
    try:
        period_zone = ZoneInfo(period_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid timezone name in {name}: '{period_timezone}'") from exc
    _, start_utc = parse_and_validate_iso_instant(start, period_zone)
    _, end_utc = parse_and_validate_iso_instant(end, period_zone)
    if start_utc >= end_utc:
        raise ValueError(f"{name}.start must be earlier than {name}.end.")
    label = value.get("label")
    if label is not None and not isinstance(label, str):
        raise ValueError(f"{name}.label must be a string.")
    return {
        "start": start_utc,
        "end": end_utc,
        "timezone": period_timezone,
        **({"label": label} if label is not None else {}),
    }


def _raw_query_from_normalized(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuilds raw query fields before strict validation of normalized internal callers."""
    raw = dict(envelope)
    tr = envelope["time_range"]
    raw["time_range"] = {
        "start": tr["raw_start"],
        "end": tr["raw_end"],
        "timezone": tr["timezone"],
    }
    return raw


def validate_query(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Validates and normalizes a query request envelope.

    Normalized internal queries are reconstructed as raw fields and validated again;
    caller-supplied internal markers never bypass this public validation contract.
    
    Raises:
        ValueError: If contract_version is unsupported, unknown fields are provided,
                    or field types/values are invalid.
    """
    if not isinstance(envelope, dict):
        raise ValueError("Query envelope must be a JSON object (dict).")

    normalized_time_range_keys = {"start", "end", "timezone", "raw_start", "raw_end", "dst_transition"}
    time_range_value = envelope.get("time_range")
    if (
        "_validated" not in envelope
        and isinstance(time_range_value, dict)
        and set(time_range_value) == normalized_time_range_keys
    ):
        envelope = _raw_query_from_normalized(envelope)

    # Reject unknown top-level keys, including any claimed internal validation marker.
    unknown_keys = set(envelope.keys()) - ALLOWED_ENVELOPE_KEYS
    if unknown_keys:
        raise ValueError(f"Unknown fields in query envelope: {sorted(unknown_keys)}")

    # 1. Contract version
    contract_version = envelope.get("contract_version")
    if contract_version != "1":
        raise ValueError(
            f"Unsupported contract_version: '{contract_version}'. Must be exactly '1'."
        )

    # 2. Harness scope
    raw_harness = envelope.get("harness_scope", "combined")
    if raw_harness is None:
        raw_harness = "combined"
    if not isinstance(raw_harness, str):
        raise ValueError("harness_scope must be a string.")
    harness_scope = raw_harness.lower().replace("_", "-")
    if harness_scope not in SUPPORTED_HARNESS_SCOPES:
        raise ValueError(
            f"Invalid harness_scope: '{raw_harness}'. Must be one of {sorted(SUPPORTED_HARNESS_SCOPES)}"
        )

    # 3. Time range
    time_range = envelope.get("time_range") or {}
    if not isinstance(time_range, dict):
        raise ValueError("time_range must be a dict.")

    unknown_tr_keys = set(time_range.keys()) - {"start", "end", "timezone"}
    if unknown_tr_keys:
        raise ValueError(f"Unknown keys in time_range: {sorted(unknown_tr_keys)}")

    timezone_alias = envelope.get("timezone", "UTC")
    if not isinstance(timezone_alias, str):
        raise ValueError("timezone must be a string.")
    tz_name = time_range.get("timezone", timezone_alias)
    if not isinstance(tz_name, str):
        raise ValueError("time_range.timezone must be a string.")
    try:
        ZoneInfo(timezone_alias)
        tz_obj = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid timezone name: '{exc.args[0]}'") from exc
    except Exception as exc:
        raise ValueError(f"Error loading timezone '{tz_name}': {exc}") from exc

    raw_start = time_range.get("start")
    raw_end = time_range.get("end")

    utc_start: Optional[str] = None
    utc_end: Optional[str] = None
    dst_transition_detected = False

    if raw_start is not None:
        if not isinstance(raw_start, str):
            raise ValueError("time_range.start must be an ISO string.")
        dt_start_local, utc_start = parse_and_validate_iso_instant(raw_start, tz_obj, is_end=False)
    else:
        dt_start_local = None

    if raw_end is not None:
        if not isinstance(raw_end, str):
            raise ValueError("time_range.end must be an ISO string.")
        dt_end_local, utc_end = parse_and_validate_iso_instant(raw_end, tz_obj, is_end=True)
    else:
        dt_end_local = None

    if utc_start is not None and utc_end is not None:
        if utc_start >= utc_end:
            raise ValueError(
                f"time_range.start ({utc_start}) must be strictly earlier than time_range.end ({utc_end}) for half-open interval [start, end)."
            )
        # Check for DST transition
        if dt_start_local and dt_end_local and tz_name != "UTC":
            offset_start = dt_start_local.utcoffset()
            offset_end = dt_end_local.utcoffset()
            if offset_start != offset_end:
                dst_transition_detected = True

    normalized_time_range = {
        "start": utc_start,
        "end": utc_end,
        "timezone": tz_name,
        "raw_start": raw_start,
        "raw_end": raw_end,
        "dst_transition": dst_transition_detected,
    }

    # 4. Filters
    filters = envelope.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be a dict.")

    unknown_filter_keys = set(filters.keys()) - ALLOWED_FILTER_KEYS
    if unknown_filter_keys:
        raise ValueError(f"Unknown filter keys: {sorted(unknown_filter_keys)}")

    normalized_filters: Dict[str, List[str]] = {}
    for k, v in filters.items():
        if v is None:
            continue
        if isinstance(v, (str, int)):
            vals = [str(v).strip()]
        elif isinstance(v, (list, tuple, set)):
            vals = [str(x).strip() for x in v if x is not None and str(x).strip()]
        else:
            raise ValueError(f"Filter value for '{k}' must be string or list of strings.")
        if vals:
            normalized_filters[k] = vals
    for key, allowed_values in {
        "subscription": {"recorded", "unrecorded"},
        "duration": {"under_1m", "1m_to_5m", "5m_to_30m", "30m_plus", "unknown"},
    }.items():
        invalid = set(normalized_filters.get(key, [])) - allowed_values
        if invalid:
            raise ValueError(f"Invalid {key} filter value(s): {sorted(invalid)}")

    # 5. Metric ID / family
    raw_family = envelope.get("family")
    metric_id = envelope.get("metric_id")
    if not metric_id and raw_family:
        metric_id = raw_family
    elif not metric_id:
        metric_id = "summary"
    if not isinstance(metric_id, str):
        raise ValueError("metric_id must be a string.")
    metric_id = metric_id.strip()

    valid_families = {
        "summary", "timeline", "models", "projects", "sessions", "requests", "events", "coverage",
        "tokens", "cache", "speed", "performance", "traces", "attribution",
        "providers", "quotas", "burn", "live", "budgets",
        "model_profiles", "outcomes", "task_outcomes", "efficiency",
        "forecasts", "forecast", "anomalies", "usage_anomalies", "speed_anomalies",
        "comparisons", "before_after_comparison",
    }
    if metric_id not in valid_families and metric_id not in METRIC_REGISTRY:
        raise ValueError(
            f"Unknown metric_id: '{metric_id}'. Must be a metric family {sorted(valid_families)} or registered metric."
        )

    # Determine family
    if metric_id in ("tokens", "cache") or raw_family in ("tokens", "cache"):
        family = "tokens"
    elif metric_id in ("speed", "performance", "generation_speed_tps") or raw_family in ("speed", "performance"):
        family = "speed"
    elif metric_id in ("traces", "attribution") or raw_family in ("traces", "attribution"):
        family = "traces"
    elif metric_id in ("providers", "providers_breakdown") or raw_family == "providers":
        family = "providers"
    elif metric_id in ("quotas", "provider_quotas") or raw_family == "quotas":
        family = "quotas"
    elif metric_id in ("burn", "token_burn_rate") or raw_family == "burn":
        family = "burn"
    elif metric_id in ("live", "live_sessions") or raw_family == "live":
        family = "live"
    elif metric_id in ("budgets", "usage_budgets") or raw_family == "budgets":
        family = "budgets"
    elif metric_id == "model_profiles" or raw_family == "model_profiles":
        family = "model_profiles"
    elif metric_id in ("outcomes", "task_outcomes") or raw_family in ("outcomes", "task_outcomes"):
        family = "outcomes"
    elif metric_id in ("efficiency", "efficiency_summary", "efficiency_scenarios") or raw_family == "efficiency":
        family = "efficiency"
    elif metric_id in ("forecasts", "forecast", "usage_forecast") or raw_family in ("forecasts", "forecast"):
        family = "forecasts"
    elif metric_id in ("anomalies", "usage_anomalies", "speed_anomalies") or raw_family in ("anomalies", "usage_anomalies", "speed_anomalies"):
        family = "anomalies"
    elif metric_id in ("comparisons", "before_after_comparison") or raw_family in ("comparisons", "before_after_comparison"):
        family = "comparisons"
    elif metric_id in valid_families:
        family = metric_id
    else:
        defn = METRIC_REGISTRY.get(metric_id)
        family = defn.family if defn else "summary"

    option_keys = set().union(*_OPTION_KEYS_BY_FAMILY.values())
    provided_options = set(envelope) & option_keys
    unsupported_options = provided_options - _OPTION_KEYS_BY_FAMILY.get(family, set())
    if unsupported_options:
        raise ValueError(f"Options {sorted(unsupported_options)} are not supported for family '{family}'.")

    if "simulator_parameters" in envelope:
        envelope = dict(envelope)
        envelope["simulator_parameters"] = _validate_simulator_parameters(envelope["simulator_parameters"])
    if "scenario_inputs" in envelope:
        envelope = dict(envelope)
        envelope["scenario_inputs"] = _validate_scenario_inputs(envelope["scenario_inputs"])
    if "history_window" in envelope:
        envelope = dict(envelope)
        envelope["history_window"] = _validate_history_window(
            envelope["history_window"], tz_name
        )
    if "min_sample_size" in envelope:
        _as_positive_integer(envelope["min_sample_size"], "min_sample_size")
    if "interval_minutes" in envelope:
        _as_positive_integer(envelope["interval_minutes"], "interval_minutes")
    if "heuristic_window_minutes" in envelope:
        _as_positive_integer(envelope["heuristic_window_minutes"], "heuristic_window_minutes")
    if "threshold_multiplier" in envelope:
        _as_finite_number(envelope["threshold_multiplier"], "threshold_multiplier", 0)
        if envelope["threshold_multiplier"] == 0:
            raise ValueError("threshold_multiplier must be greater than 0.")
    if "anomaly_settings" in envelope:
        envelope = dict(envelope)
        envelope["anomaly_settings"] = _validate_anomaly_settings(envelope["anomaly_settings"])
    if "comparison_periods" in envelope:
        periods = envelope["comparison_periods"]
        if not isinstance(periods, dict) or set(periods) != {"period_a", "period_b"}:
            raise ValueError("comparison_periods must contain exactly period_a and period_b.")
        normalized_periods = {
            key: _validate_comparison_period(periods[key], key, tz_name)
            for key in ("period_a", "period_b")
        }
        if normalized_periods["period_a"]["timezone"] != normalized_periods["period_b"]["timezone"]:
            raise ValueError("comparison period timezones must match.")
        envelope = dict(envelope)
        envelope["comparison_periods"] = normalized_periods
    if "period_a" in envelope or "period_b" in envelope:
        if "period_a" not in envelope or "period_b" not in envelope:
            raise ValueError("Comparison family requires both period_a and period_b.")
        if "comparison_periods" in envelope:
            raise ValueError("Provide comparison_periods or top-level period_a/period_b, not both.")
        period_a = _validate_comparison_period(envelope["period_a"], "period_a", tz_name)
        period_b = _validate_comparison_period(envelope["period_b"], "period_b", tz_name)
        if period_a["timezone"] != period_b["timezone"]:
            raise ValueError("comparison period timezones must match.")
        envelope = dict(envelope)
        envelope["period_a"] = period_a
        envelope["period_b"] = period_b
    # 6. Group by
    group_by = envelope.get("group_by") or []
    if isinstance(group_by, str):
        group_by = [group_by]
    elif not isinstance(group_by, (list, tuple)):
        raise ValueError("group_by must be a list of strings.")
    normalized_group_by = [str(g).strip().lower() for g in group_by if g]

    # 7. Sort
    raw_sort = envelope.get("sort")
    sort_field: Optional[str] = None
    sort_order = "desc"

    if raw_sort is not None:
        if isinstance(raw_sort, str):
            parts = raw_sort.split(":")
            sort_field = parts[0].strip()
            if len(parts) > 1 and parts[1].strip().lower() in ("asc", "desc"):
                sort_order = parts[1].strip().lower()
        elif isinstance(raw_sort, dict):
            sort_field = raw_sort.get("field")
            if sort_field:
                sort_field = str(sort_field).strip()
            order_val = raw_sort.get("order", "desc")
            if str(order_val).lower() in ("asc", "desc"):
                sort_order = str(order_val).lower()
        else:
            raise ValueError("sort must be a string (e.g. 'total_tokens:desc') or dict.")

        if sort_field:
            allowed_for_family = ALLOWED_SORT_FIELDS.get(family, set())
            if sort_field not in allowed_for_family:
                raise ValueError(
                    f"Sort field '{sort_field}' is not allowed for family '{family}'. Allowed: {sorted(allowed_for_family)}"
                )

    normalized_sort = {"field": sort_field, "order": sort_order} if sort_field else None

    # 8. Pagination
    raw_pagination = envelope.get("pagination") or {}
    if not isinstance(raw_pagination, dict):
        raise ValueError("pagination must be a dict.")

    limit = raw_pagination.get("limit", 100)
    offset = raw_pagination.get("offset", 0)

    try:
        limit = int(limit)
        offset = int(offset)
    except (ValueError, TypeError) as exc:
        raise ValueError("pagination limit and offset must be integers.") from exc

    if limit < 1 or limit > 5000:
        raise ValueError("pagination limit must be between 1 and 5000.")
    if offset < 0:
        raise ValueError("pagination offset must be >= 0.")

    normalized_pagination = {"limit": limit, "offset": offset}

    # 9. Search
    search = envelope.get("search")
    if search is not None:
        search = str(search).strip()
        if not search:
            search = None

    # 10. Archive inclusion
    include_archive = envelope.get("include_archive", True)
    if not isinstance(include_archive, bool):
        include_archive = bool(include_archive)

    res = {
        "contract_version": "1",
        "harness_scope": harness_scope,
        "time_range": normalized_time_range,
        "filters": normalized_filters,
        "metric_id": metric_id,
        "family": family,
        "group_by": normalized_group_by,
        "sort": normalized_sort,
        "pagination": normalized_pagination,
        "search": search,
        "include_archive": include_archive,
    }
    for opt_key in (
        "simulator_parameters",
        "min_sample_size",
        "scenario_inputs",
        "anomaly_settings",
        "comparison_periods",
        "period_a",
        "period_b",
        "threshold_multiplier",
        "interval_minutes",
        "heuristic_window_minutes",
        "history_window",
        "timezone",
    ):
        if opt_key in envelope:
            res[opt_key] = envelope[opt_key]
    return res


def _build_where_clause(
    norm_query: Dict[str, Any],
    table_prefix: str = "c",
    param_prefix: str = "p_",
) -> Tuple[str, Dict[str, Any]]:
    """Constructs SQL WHERE conditions and parameter bindings for canonical_requests."""
    clauses: List[str] = []
    params: Dict[str, Any] = {}

    # Time range (half-open [start, end))
    tr = norm_query.get("time_range", {})
    if tr.get("start"):
        p_name = f"{param_prefix}start_time"
        clauses.append(f"{table_prefix}.request_timestamp >= :{p_name}")
        params[p_name] = tr["start"]
    if tr.get("end"):
        p_name = f"{param_prefix}end_time"
        clauses.append(f"{table_prefix}.request_timestamp < :{p_name}")
        params[p_name] = tr["end"]

    # Harness scope
    scope = norm_query.get("harness_scope", "combined")
    if scope == "claude-code":
        clauses.append(f"{table_prefix}.origin_harness IN ('claude-code', 'claude_code')")
    elif scope == "omp":
        clauses.append(f"{table_prefix}.origin_harness IN ('omp', 'oh-my-pi', 'oh_my_pi')")

    # Filters
    filters = norm_query.get("filters", {})

    # Model filter
    if "model" in filters:
        model_vals = filters["model"]
        placeholders = []
        for i, m in enumerate(model_vals):
            p = f"{param_prefix}m_{i}"
            placeholders.append(f":{p}")
            params[p] = m
        in_list = ", ".join(placeholders)
        clauses.append(f"({table_prefix}.normalized_model IN ({in_list}) OR {table_prefix}.raw_model IN ({in_list}))")

    # Provider filter
    if "provider" in filters:
        prov_vals = filters["provider"]
        placeholders = []
        for i, pv in enumerate(prov_vals):
            p = f"{param_prefix}pv_{i}"
            placeholders.append(f":{p}")
            params[p] = pv
        in_list = ", ".join(placeholders)
        clauses.append(f"{table_prefix}.provider IN ({in_list})")

    # Project filter matches project ID/name/alias; "unattributed" is the NULL-project sentinel.
    if "project" in filters:
        proj_vals = filters["project"]
        project_terms = []
        named_project_vals = [value for value in proj_vals if value != "unattributed"]
        if named_project_vals:
            named_placeholders = []
            for i, value in enumerate(named_project_vals):
                p = f"{param_prefix}pr_named_{i}"
                named_placeholders.append(f":{p}")
                params[p] = value
            named_in_list = ", ".join(named_placeholders)
            project_terms.append(
                f"({table_prefix}.project_id IN ({named_in_list}) OR EXISTS ("
                f"SELECT 1 FROM projects prj WHERE prj.project_id = {table_prefix}.project_id "
                f"AND (prj.project_name IN ({named_in_list}) OR prj.project_alias IN ({named_in_list}))))"
            )
        if "unattributed" in proj_vals:
            project_terms.append(f"{table_prefix}.project_id IS NULL")
        clauses.append(f"({' OR '.join(project_terms)})")

    # Repository filter uses the persisted project_alias (there is no separate repository field).
    if "repository" in filters:
        repository_vals = filters["repository"]
        placeholders = []
        for i, repository in enumerate(repository_vals):
            p = f"{param_prefix}repo_{i}"
            placeholders.append(f":{p}")
            params[p] = repository
        in_list = ", ".join(placeholders)
        clauses.append(
            f"EXISTS (SELECT 1 FROM projects repo WHERE repo.project_id = {table_prefix}.project_id "
            f"AND repo.project_alias IN ({in_list}))"
        )

    # Session filter
    if "session" in filters:
        sess_vals = filters["session"]
        placeholders = []
        for i, s in enumerate(sess_vals):
            p = f"{param_prefix}s_{i}"
            placeholders.append(f":{p}")
            params[p] = s
        in_list = ", ".join(placeholders)
        clauses.append(f"{table_prefix}.session_id IN ({in_list})")

    # Route filter
    if "route" in filters:
        rt_vals = filters["route"]
        placeholders = []
        for i, r in enumerate(rt_vals):
            p = f"{param_prefix}r_{i}"
            placeholders.append(f":{p}")
            params[p] = r
        in_list = ", ".join(placeholders)
        clauses.append(f"{table_prefix}.access_route IN ({in_list})")

    # Application filter (matches origin_harness)
    if "application" in filters:
        app_vals = filters["application"]
        placeholders = []
        for i, a in enumerate(app_vals):
            p = f"{param_prefix}app_{i}"
            placeholders.append(f":{p}")
            params[p] = a
        in_list = ", ".join(placeholders)
        clauses.append(f"{table_prefix}.origin_harness IN ({in_list})")
    # Subscription filter means a non-NULL recorded subscription fee fact, not quota coverage.
    if "subscription" in filters:
        subscription_values = filters["subscription"]
        subscription_terms = []
        if "recorded" in subscription_values:
            subscription_terms.append(
                f"EXISTS (SELECT 1 FROM financial_facts sub WHERE sub.canonical_id = "
                f"{table_prefix}.canonical_id AND sub.subscription_fee_usd IS NOT NULL)"
            )
        if "unrecorded" in subscription_values:
            subscription_terms.append(
                f"NOT EXISTS (SELECT 1 FROM financial_facts sub WHERE sub.canonical_id = "
                f"{table_prefix}.canonical_id AND sub.subscription_fee_usd IS NOT NULL)"
            )
        clauses.append(f"({' OR '.join(subscription_terms)})")

    # Duration categories are based on session.duration_ms; no request-level proxy is inferred.
    if "duration" in filters:
        duration_values = filters["duration"]
        duration_terms = []
        duration_ranges = {
            "under_1m": "duration_ms < 60000",
            "1m_to_5m": "duration_ms >= 60000 AND duration_ms < 300000",
            "5m_to_30m": "duration_ms >= 300000 AND duration_ms < 1800000",
            "30m_plus": "duration_ms >= 1800000",
        }
        for value in duration_values:
            if value in duration_ranges:
                duration_terms.append(
                    f"EXISTS (SELECT 1 FROM sessions ds WHERE ds.session_id = "
                    f"{table_prefix}.session_id AND ds.duration_ms IS NOT NULL "
                    f"AND {duration_ranges[value]})"
                )
            elif value == "unknown":
                duration_terms.append(
                    f"NOT EXISTS (SELECT 1 FROM sessions ds WHERE ds.session_id = "
                    f"{table_prefix}.session_id AND ds.duration_ms IS NOT NULL)"
                )
        clauses.append(f"({' OR '.join(duration_terms)})")

    # Task filter (matches explicit manual labels in manual_settings_and_outcomes)
    if "task" in filters:
        task_vals = filters["task"]
        placeholders = []
        for i, t in enumerate(task_vals):
            p = f"{param_prefix}t_{i}"
            placeholders.append(f":{p}")
            params[p] = t
        in_list = ", ".join(placeholders)
        clauses.append(
            f"EXISTS (SELECT 1 FROM manual_settings_and_outcomes m "
            f"WHERE m.task_label IN ({in_list}) AND ("
            f"(m.entity_type = 'session' AND m.entity_id = {table_prefix}.session_id) OR "
            f"(m.entity_type = 'request' AND m.entity_id = {table_prefix}.canonical_id)))"
        )

    # Search filter
    search = norm_query.get("search")
    if search:
        p_search = f"{param_prefix}search"
        params[p_search] = f"%{search}%"
        clauses.append(
            f"({table_prefix}.canonical_id LIKE :{p_search} OR "
            f"{table_prefix}.session_id LIKE :{p_search} OR "
            f"{table_prefix}.raw_model LIKE :{p_search} OR "
            f"{table_prefix}.normalized_model LIKE :{p_search} OR "
            f"{table_prefix}.provider LIKE :{p_search})"
        )

    where_sql = " AND ".join(clauses) if clauses else "1=1"
    return where_sql, params


def _check_unresolved_overlap(
    conn: sqlite3.Connection,
    where_sql: str,
    params: Dict[str, Any],
) -> Tuple[bool, int]:
    """Checks whether any matching canonical requests have unresolved overlap flag or collision."""
    query = f"""
    SELECT COUNT(*)
    FROM canonical_requests c
    WHERE {where_sql} AND c.is_unresolved_overlap = 1
    """
    cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    count = row[0] if row else 0
    return (count > 0, count)


def _coverage_status(observed_count: int, expected_count: int) -> str:
    """Describe whether an aggregate has no, partial, or complete source coverage."""
    if observed_count == 0:
        return "unavailable"
    return "exact" if observed_count == expected_count else "partial"



def _query_summary(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes high-level KPI rollups across disjoint counters, costs, speed, and sessions."""
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")
    has_overlap, overlap_count = _check_unresolved_overlap(conn, where_sql, params)

    if has_overlap:
        warnings.append(
            f"Unresolved overlap detected between harnesses ({overlap_count} overlapping requests). "
            "Combined total cannot be verified as an exact sum."
        )

    # Core SQL aggregation joining normalized_usage and financial_facts
    sql = f"""
    SELECT
        COUNT(c.canonical_id) AS total_requests,
        COUNT(DISTINCT c.session_id) AS total_sessions,
        SUM(u.fresh_input_tokens) AS sum_fresh_input,
        SUM(u.cache_read_tokens) AS sum_cache_read,
        SUM(u.cache_write_5m_tokens) AS sum_cache_write_5m,
        SUM(u.cache_write_1h_tokens) AS sum_cache_write_1h,
        SUM(u.cache_write_unknown_ttl_tokens) AS sum_cache_write_unknown_ttl,
        SUM(u.output_tokens) AS sum_output,
        SUM(u.reasoning_tokens) AS sum_reasoning,
        SUM(f.api_equivalent_usd) AS sum_api_usd,
        SUM(f.api_equivalent_inr) AS sum_api_inr,
        SUM(f.actual_billed_amount) AS sum_billed_amount,
        SUM(f.cache_savings_usd) AS sum_cache_savings_usd,
        COUNT(CASE WHEN f.pricing_status = 'priced' THEN 1 END) AS count_priced,
        COUNT(CASE WHEN f.pricing_status = 'unpriced' OR f.pricing_status IS NULL THEN 1 END) AS count_unpriced,
        COUNT(CASE WHEN f.pricing_status = 'partial' THEN 1 END) AS count_partial_priced,
        -- Timing eligibility (excluding untimed requests)
        COUNT(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0 THEN 1 END) AS count_eligible_timing,
        SUM(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0 THEN u.output_tokens ELSE 0 END) AS timing_output_tokens,
        SUM(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0 THEN c.generation_duration_ms ELSE 0 END) AS sum_generation_ms,
        AVG(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0 THEN c.generation_speed_tps END) AS avg_speed_tps,
        AVG(CASE WHEN c.ttft_ms IS NOT NULL THEN c.ttft_ms END) AS avg_ttft_ms,
        COUNT(CASE WHEN c.ttft_ms IS NOT NULL THEN 1 END) AS count_ttft,
        COUNT(u.fresh_input_tokens) AS count_fresh_input,
        COUNT(u.cache_read_tokens) AS count_cache_read,
        COUNT(u.cache_write_5m_tokens) AS count_cache_write_5m,
        COUNT(u.cache_write_1h_tokens) AS count_cache_write_1h,
        COUNT(u.cache_write_unknown_ttl_tokens) AS count_cache_write_unknown,
        COUNT(u.output_tokens) AS count_output,
        COUNT(u.reasoning_tokens) AS count_reasoning,
        COUNT(f.api_equivalent_usd) AS count_api_usd,
        COUNT(f.api_equivalent_inr) AS count_api_inr,
        COUNT(f.actual_billed_amount) AS count_billed_amount,
        COUNT(f.cache_savings_usd) AS count_cache_savings
    FROM canonical_requests c
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()

    total_requests = row[0] or 0
    total_sessions = row[1] or 0
    fresh_input = row[2]
    cache_read = row[3]
    cache_write_5m = row[4]
    cache_write_1h = row[5]
    cache_write_unknown_ttl = row[6]
    output_tokens = row[7]
    reasoning_tokens = row[8]
    api_usd = row[9]
    api_inr = row[10]
    actual_billed = row[11]
    cache_savings = row[12]
    count_priced = row[13] or 0
    count_unpriced = row[14] or 0
    count_partial_priced = row[15] or 0
    count_eligible_timing = row[16] or 0
    timing_output_tokens = row[17] or 0
    sum_gen_ms = row[18] or 0
    avg_speed_tps = row[19]
    avg_ttft_ms = row[20]
    count_ttft = row[21] or 0
    token_observed_counts = {
        "fresh_input_tokens": row[22] or 0,
        "cache_read_tokens": row[23] or 0,
        "cache_write_5m_tokens": row[24] or 0,
        "cache_write_1h_tokens": row[25] or 0,
        "cache_write_unknown_ttl_tokens": row[26] or 0,
        "output_tokens": row[27] or 0,
        "reasoning_tokens": row[28] or 0,
    }
    token_fields_status = {
        field: _coverage_status(count, total_requests)
        for field, count in token_observed_counts.items()
    }
    billable_token_count = sum(
        token_observed_counts[field]
        for field in (
            "fresh_input_tokens",
            "cache_read_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_write_unknown_ttl_tokens",
            "output_tokens",
        )
    )
    live_disjoint_tokens = (
        0
        if total_requests == 0
        else (
            sum(value or 0 for value in (
                fresh_input,
                cache_read,
                cache_write_5m,
                cache_write_1h,
                cache_write_unknown_ttl,
                output_tokens,
            ))
            if billable_token_count > 0
            else None
        )
    )
    api_usd_count = row[29] or 0
    api_inr_count = row[30] or 0
    billed_count = row[31] or 0
    cache_savings_count = row[32] or 0

    # Archive rollup if included
    archive_tokens: Optional[int] = 0
    archive_cost_usd: Optional[float] = None
    archive_records = 0
    archive_unpriced = 0
    archive_partitions = 0
    archive_token_count = 0
    archive_cost_count = 0
    has_archive = False

    if norm_query.get("include_archive", True):
        request_specific_scope = bool(norm_query.get("filters") or norm_query.get("search")) or (
            norm_query.get("harness_scope", "combined") != "combined"
        )
        if request_specific_scope:
            warnings.append(
                "Archive partitions cannot be scoped by request filters, search, or harness; "
                "they were omitted from this filtered summary."
            )
        else:
            # Query mutually exclusive archive partitions (is_live_replaced = 0)
            arch_clauses = ["is_live_replaced = 0"]
            arch_params: Dict[str, Any] = {}
            tr = norm_query.get("time_range", {})
            if tr.get("start"):
                arch_clauses.append("end_time >= :arch_start")
                arch_params["arch_start"] = tr["start"]
            if tr.get("end"):
                arch_clauses.append("start_time < :arch_end")
                arch_params["arch_end"] = tr["end"]

            arch_sql = f"""
            SELECT
                COUNT(*),
                SUM(total_tokens),
                SUM(total_cost_usd),
                SUM(records_count),
                COUNT(CASE WHEN total_cost_usd IS NULL OR pricing_status = 'unpriced' THEN 1 END),
                COUNT(total_tokens),
                COUNT(total_cost_usd)
            FROM archive_partitions
            WHERE {" AND ".join(arch_clauses)}
            """
            cur.execute(arch_sql, arch_params)
            arch_row = cur.fetchone()
            if arch_row and (arch_row[0] or 0) > 0:
                has_archive = True
                archive_partitions = arch_row[0] or 0
                archive_token_count = arch_row[5] or 0
                archive_cost_count = arch_row[6] or 0
                archive_tokens = arch_row[1] if archive_token_count else None
                archive_cost_usd = (
                    float(arch_row[2]) if archive_cost_count and arch_row[2] is not None else None
                )
                archive_records = arch_row[3] or 0
                archive_unpriced = arch_row[4] or 0

    total_observed_requests = total_requests + archive_records
    token_components = []
    if total_requests and live_disjoint_tokens is not None:
        token_components.append(live_disjoint_tokens)
    if has_archive and archive_tokens is not None:
        token_components.append(archive_tokens)
    combined_total_tokens = sum(token_components) if token_components else None
    live_tokens_missing = billable_token_count < total_requests * 6
    archive_tokens_missing = has_archive and archive_token_count < archive_partitions
    total_tokens_status = (
        "unavailable"
        if combined_total_tokens is None
        else "partial"
        if has_overlap or live_tokens_missing or archive_tokens_missing
        else "exact"
    )
    token_fields_status["total_tokens"] = total_tokens_status
    token_fields_status["cache_write_tokens"] = _coverage_status(
        token_observed_counts["cache_write_5m_tokens"]
        + token_observed_counts["cache_write_1h_tokens"]
        + token_observed_counts["cache_write_unknown_ttl_tokens"],
        total_requests * 3,
    )

    # Cache hit rate (eligible denominator = fresh_input + cache_read)
    cache_denom = (
        fresh_input + cache_read
        if fresh_input is not None and cache_read is not None
        else None
    )
    cache_hit_rate = (
        cache_read / cache_denom
        if cache_denom is not None and cache_denom > 0
        else 0.0 if cache_denom == 0 else None
    )


    # Calculated speed TPS (weighted throughput over eligible requests)
    calc_speed_tps: Optional[float] = None
    if sum_gen_ms > 0:
        calc_speed_tps = round(timing_output_tokens / (sum_gen_ms / 1000.0), 2)
    elif avg_speed_tps is not None:
        calc_speed_tps = round(avg_speed_tps, 2)

    # Average session duration
    dur_sql = f"""
    SELECT AVG(s.duration_ms), COUNT(s.duration_ms), COUNT(*)
    FROM sessions s
    WHERE s.session_id IN (
        SELECT DISTINCT c.session_id FROM canonical_requests c WHERE {where_sql}
    )
    """
    cur.execute(dur_sql, params)
    dur_row = cur.fetchone()
    avg_session_duration_ms = dur_row[0] if dur_row and dur_row[0] is not None else None
    duration_observed = dur_row[1] if dur_row else 0
    duration_sessions = dur_row[2] if dur_row else 0
    # Stored session spans are not request active time and may be source-inferred.
    from token_ledger.metrics.performance import compute_percentiles
    cur.execute(f"""
        SELECT s.duration_ms FROM sessions s
        WHERE s.session_id IN (
            SELECT DISTINCT c.session_id FROM canonical_requests c WHERE {where_sql}
        ) AND s.duration_ms IS NOT NULL AND s.duration_ms >= 0
    """, params)
    session_duration_distribution = compute_percentiles([r[0] for r in cur.fetchall()])
    session_duration_distribution["basis"] = "stored_session_duration_ms"
    session_duration_distribution["linked_sessions"] = duration_sessions
    session_duration_distribution["status"] = _coverage_status(
        session_duration_distribution["count"], duration_sessions
    )


    api_cost_components = []
    api_cost_missing = False
    if total_requests:
        if api_usd_count and api_usd is not None:
            api_cost_components.append(api_usd)
        if api_usd_count < total_requests:
            api_cost_missing = True
    if has_archive:
        if archive_cost_count and archive_cost_usd is not None:
            api_cost_components.append(archive_cost_usd)
        if archive_cost_count < archive_partitions:
            api_cost_missing = True
    combined_api_usd = (
        round(sum(api_cost_components), 4) if api_cost_components else None
    )
    api_usd_status = (
        "unavailable"
        if combined_api_usd is None
        else "partial" if api_cost_missing else "exact"
    )

    values = {
        "total_tokens": combined_total_tokens,
        "total_tokens_status": total_tokens_status,
        "token_fields_status": token_fields_status,
        "has_unresolved_overlap": has_overlap,
        "live_disjoint_tokens": live_disjoint_tokens,
        "live_disjoint_tokens_status": _coverage_status(billable_token_count, total_requests * 6),
        "archive_tokens": archive_tokens,
        "archive_tokens_status": (
            _coverage_status(archive_token_count, archive_partitions)
            if has_archive
            else "unavailable"
        ),
        "fresh_input_tokens": fresh_input,
        "cache_read_tokens": cache_read,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "cache_write_unknown_ttl_tokens": cache_write_unknown_ttl,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "requests_count": total_observed_requests,
        "live_requests_count": total_requests,
        "archive_records_count": archive_records,
        "sessions_count": total_sessions,
        "cache_hit_rate": round(cache_hit_rate, 4) if cache_hit_rate is not None else None,
        "api_equivalent_usd": combined_api_usd,
        "api_equivalent_usd_status": api_usd_status,
        "api_equivalent_inr": round(api_inr, 2) if api_inr is not None else None,
        "api_equivalent_inr_status": _coverage_status(api_inr_count, total_requests),
        "actual_billed_amount": round(actual_billed, 4) if actual_billed is not None else None,
        "actual_billed_amount_status": _coverage_status(billed_count, total_requests),
        "cache_savings_usd": round(cache_savings, 4) if cache_savings is not None else None,
        "cache_savings_usd_status": _coverage_status(cache_savings_count, total_requests),
        "generation_speed_tps": calc_speed_tps,
        "ttft_ms": round(avg_ttft_ms, 2) if avg_ttft_ms is not None else None,
        "session_duration_ms": round(avg_session_duration_ms, 2) if avg_session_duration_ms is not None else None,
    }

    missingness = {
        "unpriced_requests_count": count_unpriced,
        "partial_priced_requests_count": count_partial_priced,
        "priced_requests_count": count_priced,
        "priced_coverage_ratio": round(count_priced / total_requests, 4) if total_requests > 0 else 1.0,
        "eligible_timing_requests_count": count_eligible_timing,
        "missing_timing_requests_count": total_requests - count_eligible_timing,
        "missing_ttft_requests_count": total_requests - count_ttft,
        "session_duration_observed_count": duration_observed,
        "session_duration_sessions_count": duration_sessions,
        "archive_unpriced_partitions_count": archive_unpriced,
    }

    coverage = {
        "live_data_available": total_requests > 0,
        "live_requests_count": total_requests,
        "archive_data_included": has_archive,
        "archive_records_count": archive_records,
        "timezone": norm_query["time_range"]["timezone"],
    }

    return {
        "values": values,
        "rows": [],
        "session_duration_distribution": session_duration_distribution,
        "eligible": total_observed_requests,
        "observed": total_observed_requests,
        "missingness": missingness,
        "coverage": coverage,
    }


def _query_timeline(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes time-bucketed aggregation (hourly, daily, weekly, monthly) in requested timezone."""
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")
    tz_name = norm_query["time_range"]["timezone"]
    tz_obj = ZoneInfo(tz_name)

    # Determine bucketing grain from group_by or default to 'day'
    group_by = norm_query.get("group_by", [])
    grain = "day"
    for g in group_by:
        if g in ("hour", "day", "week", "month"):
            grain = g
            break

    # Fetch rows to bucket accurately with zoneinfo (handling DST boundaries precisely)
    sql = f"""
    SELECT
        c.canonical_id,
        c.request_timestamp,
        c.normalized_model,
        u.fresh_input_tokens,
        u.cache_read_tokens,
        u.cache_write_5m_tokens,
        u.cache_write_1h_tokens,
        u.cache_write_unknown_ttl_tokens,
        u.output_tokens,
        u.reasoning_tokens,
        f.api_equivalent_usd,
        f.api_equivalent_inr,
        f.pricing_status,
        c.timing_basis,
        c.generation_duration_ms,
        c.generation_speed_tps,
        c.session_id,
        c.is_unresolved_overlap
    FROM canonical_requests c
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    ORDER BY c.request_timestamp ASC
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()

    # Only explicit request events establish interval endpoints. A request's
    # observation timestamp and duration do not establish which endpoint it is.
    interval_scope = {**norm_query, "time_range": {**norm_query["time_range"], "start": None, "end": None}}
    interval_where, interval_params = _build_where_clause(interval_scope, table_prefix="c", param_prefix="i_")
    interval_rows = conn.execute(f"""
        SELECT e.canonical_id, e.started_at, e.ended_at, e.duration_ms, c.normalized_model, c.is_unresolved_overlap
        FROM events e JOIN canonical_requests c ON c.canonical_id = e.canonical_id
        WHERE {interval_where} AND e.event_kind = 'request'
    """, interval_params).fetchall()
    intervals: List[Tuple[str, str, datetime, datetime, bool]] = []
    for canonical_id, start, end, duration, model, overlap in interval_rows:
        try:
            begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
            finish = (datetime.fromisoformat(end.replace("Z", "+00:00")) if end
                      else begin + timedelta(milliseconds=duration) if duration is not None else None)
            if begin.tzinfo is None or finish is None or finish.tzinfo is None:
                continue
            begin, finish = begin.astimezone(timezone.utc), finish.astimezone(timezone.utc)
            if finish > begin:
                intervals.append((canonical_id, model or "unknown", begin, finish, bool(overlap)))
        except (ValueError, TypeError, OverflowError):
            continue
    selected_start = datetime.fromisoformat(norm_query["time_range"]["start"].replace("Z", "+00:00")) if norm_query["time_range"].get("start") else None
    selected_end = datetime.fromisoformat(norm_query["time_range"]["end"].replace("Z", "+00:00")) if norm_query["time_range"].get("end") else None

    buckets: Dict[Any, Dict[str, Any]] = {}
    by_model = "model" in group_by

    def get_bucket(dt_utc: datetime, model_name: str) -> Dict[str, Any]:
        dt_local = dt_utc.astimezone(tz_obj)

        if grain == "hour":
            bucket_key = dt_local.replace(minute=0, second=0, microsecond=0).isoformat()
        elif grain == "day":
            bucket_key = dt_local.strftime("%Y-%m-%d")
        elif grain == "week":
            # Monday as start of week
            start_of_week = dt_local.date() - timedelta(days=dt_local.weekday())
            bucket_key = start_of_week.strftime("%Y-W%U")
        elif grain == "month":
            bucket_key = dt_local.strftime("%Y-%m")
        else:
            bucket_key = dt_local.strftime("%Y-%m-%d")
        if grain == "hour":
            bucket_start = dt_local.replace(minute=0, second=0, microsecond=0)
            bucket_end = (bucket_start.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(tz_obj)
        elif grain == "month":
            bucket_start = dt_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            bucket_end = (bucket_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            bucket_start = dt_local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            if grain == "week":
                bucket_start -= timedelta(days=bucket_start.weekday())
            bucket_end = bucket_start + timedelta(days=7 if grain == "week" else 1)

        if bucket_start.tzinfo is None:
            bucket_start = bucket_start.replace(tzinfo=tz_obj)
        if bucket_end.tzinfo is None:
            bucket_end = bucket_end.replace(tzinfo=tz_obj)
        bucket_start_utc = bucket_start.astimezone(timezone.utc)
        bucket_end_utc = bucket_end.astimezone(timezone.utc)
        if selected_start:
            bucket_start_utc = max(bucket_start_utc, selected_start)
        if selected_end:
            bucket_end_utc = min(bucket_end_utc, selected_end)

        # Model grouping applies the same canonical identity as request counts.
        grouping_key = (bucket_key, model_name) if by_model else bucket_key
        if grouping_key not in buckets:
            buckets[grouping_key] = {
                "model": model_name if by_model else None,
                "_sessions": set(),
                "_overlap": False,
                "_counter_counts": [0] * 7,
                "_usd_count": 0,
                "_intervals": [],
                "_timed_requests": set(),
                "_request_ids": set(),
                "_inr_count": 0,
                "bucket": bucket_key,
                "bucket_start": bucket_start_utc.isoformat(),
                "bucket_end": bucket_end_utc.isoformat(),
                "grain": grain,
                "timezone": tz_name,
                "requests_count": 0,
                "fresh_input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "cache_write_unknown_ttl_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "api_equivalent_usd": 0.0,
                "api_equivalent_inr": 0.0,
                "unpriced_requests_count": 0,
                "model_counts": {},
                "_speed_output": 0,
                "_speed_ms": 0,
            }

        return buckets[grouping_key]

    for r in rows:
        dt_utc = datetime.fromisoformat(r[1].replace("Z", "+00:00"))
        b = get_bucket(dt_utc, r[2] or "unknown")
        b["_request_ids"].add(r[0])
        if r[16] is not None:
            b["_sessions"].add(r[16])
        for i, value in enumerate(r[3:10]):
            if value is not None:
                b["_counter_counts"][i] += 1
        b["requests_count"] += 1
        b["_overlap"] = b["_overlap"] or bool(r[17])
        fresh = r[3] or 0
        cread = r[4] or 0
        cw5m = r[5] or 0
        cw1h = r[6] or 0
        cwunk = r[7] or 0
        outp = r[8] or 0
        reas = r[9] or 0

        b["fresh_input_tokens"] += fresh
        b["cache_read_tokens"] += cread
        b["cache_write_5m_tokens"] += cw5m
        b["cache_write_1h_tokens"] += cw1h
        b["cache_write_unknown_ttl_tokens"] += cwunk
        b["output_tokens"] += outp
        b["reasoning_tokens"] += reas
        b["total_tokens"] += (fresh + cread + cw5m + cw1h + cwunk + outp)

        if r[10] is not None:
            b["api_equivalent_usd"] += float(r[10])
            b["_usd_count"] += 1
        else:
            b["unpriced_requests_count"] += 1

        if r[11] is not None:
            b["api_equivalent_inr"] += float(r[11])
            b["_inr_count"] += 1

        # Model tally
        model_name = r[2] or "unknown"
        b["model_counts"][model_name] = b["model_counts"].get(model_name, 0) + 1

        # Timing
        timing_basis = r[13]
        gen_ms = r[14]
        if timing_basis != "none" and gen_ms is not None and gen_ms > 0:
            b["_speed_output"] += outp
            b["_speed_ms"] += gen_ms

    # Intervals may begin outside the selected range or cross bucket boundaries.
    # Allocate each slice to every intersected bucket, even without a request
    # timestamp in that bucket; never infer an endpoint from the observation.
    for canonical_id, model, begin, finish, overlap in intervals:
        start = max(begin, selected_start) if selected_start else begin
        end = min(finish, selected_end) if selected_end else finish
        while start < end:
            b = get_bucket(start, model)
            bucket_end = datetime.fromisoformat(b["bucket_end"])
            slice_end = min(end, bucket_end)
            if slice_end <= start:
                break
            b["_intervals"].append((start, slice_end))
            b["_timed_requests"].add(canonical_id)
            b["_interval_overlap"] = b.get("_interval_overlap", False) or overlap
            start = slice_end

    # Finalize bucket rows
    result_rows = []
    for b in buckets.values():
        top_model = None
        if b["model_counts"]:
            top_model = max(b["model_counts"].items(), key=lambda x: x[1])[0]
        tps = None
        if b["_speed_ms"] > 0:
            tps = round(b["_speed_output"] / (b["_speed_ms"] / 1000.0), 2)

        cache_denom = b["fresh_input_tokens"] + b["cache_read_tokens"]
        hit_rate = (b["cache_read_tokens"] / cache_denom) if cache_denom > 0 else 0.0
        interval_seconds = 0.0
        merged_end = None
        for start, end in sorted(b["_intervals"]):
            if merged_end is None or start > merged_end:
                interval_seconds += (end - start).total_seconds()
                merged_end = end
            elif end > merged_end:
                interval_seconds += (end - merged_end).total_seconds()
                merged_end = end
        active_status = ("unavailable" if not b["_intervals"] else
                         "exact" if not b["_overlap"] and not b.get("_interval_overlap") and b["_request_ids"] and b["_request_ids"] <= b["_timed_requests"]
                         else "partial")

        row_dict = {
            "bucket": b["bucket"],
            "grain": b["grain"],
            "timezone": b["timezone"],
            "model": b["model"],
            "bucket_start": b["bucket_start"],
            "bucket_end": b["bucket_end"],
            "sessions_count": len(b["_sessions"]),
            "active_minutes": round(interval_seconds / 60, 4) if b["_intervals"] else None,
            "active_minutes_basis": "explicit_request_event_interval_union" if b["_intervals"] else "unavailable",
            "active_minutes_status": active_status,
            "active_minutes_timed_requests": len(b["_timed_requests"]),
            "total_tokens_status": "partial" if b["_overlap"] and sum(b["_counter_counts"][:6]) else _coverage_status(sum(b["_counter_counts"][:6]), b["requests_count"] * 6),
            "api_equivalent_usd_status": "partial" if b["_overlap"] and b["_usd_count"] else _coverage_status(b["_usd_count"], b["requests_count"]),
            "requests_count": b["requests_count"],
            "total_tokens": b["total_tokens"] if sum(b["_counter_counts"][:6]) else None,
            "fresh_input_tokens": b["fresh_input_tokens"] if b["_counter_counts"][0] else None,
            "cache_read_tokens": b["cache_read_tokens"] if b["_counter_counts"][1] else None,
            "cache_write_5m_tokens": b["cache_write_5m_tokens"] if b["_counter_counts"][2] else None,
            "cache_write_1h_tokens": b["cache_write_1h_tokens"] if b["_counter_counts"][3] else None,
            "cache_write_unknown_ttl_tokens": b["cache_write_unknown_ttl_tokens"] if b["_counter_counts"][4] else None,
            "output_tokens": b["output_tokens"] if b["_counter_counts"][5] else None,
            "reasoning_tokens": b["reasoning_tokens"] if b["_counter_counts"][6] else None,
            "cache_hit_rate": round(hit_rate, 4),
            "api_equivalent_usd": round(b["api_equivalent_usd"], 4) if b["_usd_count"] else None,
            "api_equivalent_inr": round(b["api_equivalent_inr"], 2) if b["_inr_count"] else None,
            "top_model": top_model,
            "generation_speed_tps": tps,
            "unpriced_requests_count": b["unpriced_requests_count"],
        }
        result_rows.append(row_dict)

    # Sorting
    sort_info = norm_query.get("sort")
    if sort_info and sort_info.get("field"):
        fld = sort_info["field"]
        reverse = sort_info.get("order") == "desc"
        result_rows.sort(key=lambda x: (x.get(fld) is None, x.get(fld)), reverse=reverse)
    else:
        result_rows.sort(key=lambda x: x["bucket"])

    return {
        "values": {},
        "rows": result_rows,
        "eligible": len(result_rows),
        "observed": len(rows),
        "missingness": {},
        "coverage": {"grain": grain, "timezone": tz_name},
    }


def _query_models(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes model usage distribution ranking across canonical identities."""
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")

    sql = f"""
    SELECT
        c.normalized_model AS normalized_model,
        c.provider AS provider,
        c.raw_model AS raw_model,
        c.access_route AS access_route,
        COALESCE(c.pricing_version_id, (
            SELECT p.version_id FROM pricing_versions p
            WHERE julianday(p.effective_start) <= julianday(c.request_timestamp)
              AND (p.effective_end IS NULL OR julianday(c.request_timestamp) < julianday(p.effective_end))
            ORDER BY julianday(p.effective_start) DESC, p.created_at DESC, p.version_id DESC LIMIT 1
        )) AS identity_version,
        COUNT(c.canonical_id) AS requests_count,
        COUNT(DISTINCT c.session_id) AS sessions_count,
        SUM(u.fresh_input_tokens) AS fresh_input_tokens,
        SUM(u.cache_read_tokens) AS cache_read_tokens,
        SUM(u.cache_write_5m_tokens) AS cache_write_5m_tokens,
        SUM(u.cache_write_1h_tokens) AS cache_write_1h_tokens,
        SUM(u.cache_write_unknown_ttl_tokens) AS cache_write_unknown_ttl_tokens,
        SUM(u.output_tokens) AS output_tokens,
        SUM(u.reasoning_tokens) AS reasoning_tokens,
        SUM(u.reported_total_tokens) AS reported_total_tokens,
        SUM(f.api_equivalent_usd) AS api_equivalent_usd,
        SUM(f.api_equivalent_inr) AS api_equivalent_inr,
        SUM(f.cache_savings_usd) AS cache_savings_usd,
        COUNT(f.api_equivalent_usd) AS api_equivalent_usd_count,
        COUNT(f.api_equivalent_inr) AS api_equivalent_inr_count,
        COUNT(f.cache_savings_usd) AS cache_savings_usd_count,
        COUNT(CASE WHEN f.pricing_status = 'priced' AND f.api_equivalent_usd IS NOT NULL THEN 1 END) AS priced_requests_count,
        COUNT(CASE WHEN f.pricing_status = 'unpriced' OR f.pricing_status IS NULL THEN 1 END) AS unpriced_requests,
        SUM(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0 THEN u.output_tokens ELSE 0 END) AS timing_output,
        SUM(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0 THEN c.generation_duration_ms ELSE 0 END) AS timing_ms,
        MAX(CASE WHEN u.fresh_input_tokens IS NULL OR u.cache_read_tokens IS NULL
            OR u.cache_write_5m_tokens IS NULL OR u.cache_write_1h_tokens IS NULL
            OR u.cache_write_unknown_ttl_tokens IS NULL OR u.output_tokens IS NULL
            THEN 1 ELSE 0 END) AS has_incomplete_tokens,
        MAX(CASE WHEN c.is_unresolved_overlap = 1 THEN 1 ELSE 0 END) AS has_unresolved_overlap,
        MAX(CASE WHEN u.fresh_input_tokens IS NULL THEN 1 ELSE 0 END) AS missing_fresh_input,
        MAX(CASE WHEN u.cache_read_tokens IS NULL THEN 1 ELSE 0 END) AS missing_cache_read,
        MAX(CASE WHEN u.cache_write_5m_tokens IS NULL THEN 1 ELSE 0 END) AS missing_cache_write_5m,
        MAX(CASE WHEN u.cache_write_1h_tokens IS NULL THEN 1 ELSE 0 END) AS missing_cache_write_1h,
        MAX(CASE WHEN u.cache_write_unknown_ttl_tokens IS NULL THEN 1 ELSE 0 END) AS missing_cache_write_unknown,
        MAX(CASE WHEN u.output_tokens IS NULL THEN 1 ELSE 0 END) AS missing_output,
        MAX(CASE WHEN u.reasoning_tokens IS NULL THEN 1 ELSE 0 END) AS missing_reasoning,
        MAX(CASE WHEN u.reported_total_tokens IS NULL THEN 1 ELSE 0 END) AS missing_reported_total,
        MAX(CASE WHEN c.timing_basis != 'none' AND c.generation_duration_ms > 0
            AND u.output_tokens IS NULL THEN 1 ELSE 0 END) AS missing_timing_output
    FROM canonical_requests c
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    GROUP BY c.normalized_model, c.raw_model, c.provider, c.access_route, identity_version
    """
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    cur.execute(sql, params)
    rows = cur.fetchall()

    result_rows = []
    total_observed = 0

    for r in rows:
        norm_model = r["normalized_model"] or "unknown"
        provider = r["provider"] or "unknown"
        id_res = resolve_versioned_model_identity(conn, r["raw_model"] or norm_model, provider,
                                                   r["access_route"], None, r["identity_version"])
        req_count = r["requests_count"] or 0
        total_observed += req_count
        sess_count = r["sessions_count"] or 0
        fresh = r["fresh_input_tokens"]
        cread = r["cache_read_tokens"]
        cw5m = r["cache_write_5m_tokens"]
        cw1h = r["cache_write_1h_tokens"]
        cwunk = r["cache_write_unknown_ttl_tokens"]
        outp = r["output_tokens"]
        reas = r["reasoning_tokens"]
        token_fields_status = {
            "fresh_input_tokens": (
                "unavailable" if fresh is None
                else "partial" if r["missing_fresh_input"]
                else "exact"
            ),
            "cache_read_tokens": (
                "unavailable" if cread is None
                else "partial" if r["missing_cache_read"]
                else "exact"
            ),
            "cache_write_5m_tokens": (
                "unavailable" if cw5m is None
                else "partial" if r["missing_cache_write_5m"]
                else "exact"
            ),
            "cache_write_1h_tokens": (
                "unavailable" if cw1h is None
                else "partial" if r["missing_cache_write_1h"]
                else "exact"
            ),
            "cache_write_unknown_ttl_tokens": (
                "unavailable" if cwunk is None
                else "partial" if r["missing_cache_write_unknown"]
                else "exact"
            ),
            "output_tokens": (
                "unavailable" if outp is None
                else "partial" if r["missing_output"]
                else "exact"
            ),
            "reasoning_tokens": (
                "unavailable" if reas is None
                else "partial" if r["missing_reasoning"]
                else "exact"
            ),
            "reported_total_tokens": (
                "unavailable" if r["reported_total_tokens"] is None
                else "partial" if r["missing_reported_total"]
                else "exact"
            ),
        }
        cache_write_parts = (cw5m, cw1h, cwunk)
        cache_write_tokens = (
            None
            if all(value is None for value in cache_write_parts)
            else sum(value or 0 for value in cache_write_parts)
        )
        cache_write_tokens_status = (
            "unavailable"
            if cache_write_tokens is None
            else "partial"
            if any(
                r[name]
                for name in (
                    "missing_cache_write_5m",
                    "missing_cache_write_1h",
                    "missing_cache_write_unknown",
                )
            )
            else "exact"
        )
        billable_parts = (fresh, cread, cw5m, cw1h, cwunk, outp)
        total_tokens = (
            None
            if all(value is None for value in billable_parts)
            else sum(value or 0 for value in billable_parts)
        )
        total_tokens_status = (
            "unavailable"
            if total_tokens is None
            else "partial"
            if r["has_incomplete_tokens"] or r["has_unresolved_overlap"]
            else "exact"
        )
        token_fields_status["cache_write_tokens"] = cache_write_tokens_status
        token_fields_status["total_tokens"] = total_tokens_status
        usd = r["api_equivalent_usd"]
        inr = r["api_equivalent_inr"]
        api_equivalent_usd_status = (
            "unavailable"
            if not r["api_equivalent_usd_count"]
            else "exact"
            if r["api_equivalent_usd_count"] == req_count
            else "partial"
        )
        api_equivalent_inr_status = (
            "unavailable"
            if not r["api_equivalent_inr_count"]
            else "exact"
            if r["api_equivalent_inr_count"] == req_count
            else "partial"
        )
        unpriced = r["unpriced_requests"] or 0
        cs_usd = r["cache_savings_usd"]
        cache_savings_usd_status = (
            "unavailable"
            if not r["cache_savings_usd_count"]
            else "exact"
            if r["cache_savings_usd_count"] == req_count
            else "partial"
        )
        eligible_prompt = (
            None
            if fresh is None or cread is None
            else fresh + cread
        )
        priced_reqs = r["priced_requests_count"] or (req_count - unpriced)
        timing_outp = r["timing_output"] or 0
        timing_ms = r["timing_ms"] or 0
        speed_tps = (
            round(timing_outp / (timing_ms / 1000.0), 2)
            if timing_ms > 0 and not r["missing_timing_output"]
            else None
        )

        result_rows.append({
            "total_tokens_status": total_tokens_status,
            "token_fields_status": token_fields_status,
            "api_equivalent_usd_status": api_equivalent_usd_status,
            "api_equivalent_inr_status": api_equivalent_inr_status,
            "model": norm_model,
            "raw_model": r["raw_model"],
            "access_route": r["access_route"],
            "pricing_version_id": r["identity_version"],
            "canonical_model_id": id_res.canonical_model_id,
            "display_name": id_res.display_name,
            "provider": provider,
            "is_canonical": id_res.is_known,
            "requests_count": req_count,
            "sessions_count": sess_count,
            "total_tokens": total_tokens,
            "fresh_input_tokens": fresh,
            "cache_read_tokens": cread,
            "cache_write_5m_tokens": cw5m,
            "cache_write_1h_tokens": cw1h,
            "cache_write_unknown_ttl_tokens": cwunk,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": outp,
            "reasoning_tokens": reas,
            "reported_total_tokens": r["reported_total_tokens"],
            "eligible_prompt_tokens": eligible_prompt,
            "cache_hit_rate": (
                None
                if fresh is None or cread is None
                else round(cread / float(fresh + cread), 4)
                if fresh + cread > 0
                else 0.0
            ),
            "api_equivalent_usd": round(usd, 4) if usd is not None else None,
            "api_equivalent_inr": round(inr, 2) if inr is not None else None,
            "priced_requests_count": priced_reqs,
            "unpriced_requests_count": unpriced,
            "cache_savings_usd": round(cs_usd, 4) if cs_usd is not None else None,
            "cache_savings_usd_status": cache_savings_usd_status,
            "generation_speed_tps": speed_tps,
        })
    # Proportions use all filtered model/route/version rows, not the page.
    for metric in ("total_tokens", "requests_count", "output_tokens", "sessions_count", "api_equivalent_usd"):
        def complete(row):
            if row[metric] is None:
                return False
            if metric == "total_tokens":
                return row["total_tokens_status"] == "exact"
            if metric == "output_tokens":
                return row["token_fields_status"]["output_tokens"] == "exact"
            if metric == "api_equivalent_usd":
                return row["api_equivalent_usd_status"] == "exact"
            return True
        total = sum(row[metric] for row in result_rows) if all(complete(row) for row in result_rows) else None
        for row in result_rows:
            row.setdefault("proportions", {})[metric] = row[metric] / total if total else None


    sort_info = norm_query.get("sort")
    if sort_info and sort_info.get("field"):
        fld = sort_info["field"]
        reverse = sort_info.get("order") == "desc"
        result_rows.sort(key=lambda x: (x.get(fld) is None, x.get(fld)), reverse=reverse)
    else:
        result_rows.sort(key=lambda x: (x.get("total_tokens") is None, x.get("total_tokens")), reverse=True)

    pag = norm_query.get("pagination", {"limit": 100, "offset": 0})
    limit = pag["limit"]
    offset = pag["offset"]
    paged_rows = result_rows[offset : offset + limit]

    return {
        "values": {},
        "rows": paged_rows,
        "eligible": len(result_rows),
        "observed": total_observed,
        "missingness": {},
        "coverage": {},
    }


def _query_projects(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes project workspace rollups with tokens, cost, requests, and sessions."""
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")

    sql = f"""
    SELECT
        COALESCE(c.project_id, 'unattributed') AS project_id,
        COALESCE(p.project_name, 'Unattributed') AS project_name,
        p.project_alias,
        COUNT(c.canonical_id) AS requests_count,
        COUNT(DISTINCT c.session_id) AS sessions_count,
        SUM(u.fresh_input_tokens) AS fresh_input_tokens,
        SUM(u.cache_read_tokens) AS cache_read_tokens,
        CASE WHEN COUNT(u.cache_write_5m_tokens) + COUNT(u.cache_write_1h_tokens)
                + COUNT(u.cache_write_unknown_ttl_tokens) > 0
            THEN SUM(COALESCE(u.cache_write_5m_tokens, 0)
                + COALESCE(u.cache_write_1h_tokens, 0)
                + COALESCE(u.cache_write_unknown_ttl_tokens, 0)) END AS cache_write_tokens,
        SUM(u.output_tokens) AS output_tokens,
        SUM(f.api_equivalent_usd) AS api_equivalent_usd,
        SUM(f.api_equivalent_inr) AS api_equivalent_inr,
        COUNT(f.api_equivalent_usd) AS count_api_usd,
        COUNT(f.api_equivalent_inr) AS count_api_inr,
        COUNT(u.fresh_input_tokens) AS count_fresh_input,
        COUNT(u.cache_read_tokens) AS count_cache_read,
        COUNT(u.cache_write_5m_tokens) AS count_cache_write_5m,
        COUNT(u.cache_write_1h_tokens) AS count_cache_write_1h,
        COUNT(u.cache_write_unknown_ttl_tokens) AS count_cache_write_unknown,
        COUNT(u.output_tokens) AS count_output
    FROM canonical_requests c
    LEFT JOIN projects p ON c.project_id = p.project_id
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    GROUP BY c.project_id
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()

    tz_name = norm_query.get("time_range", {}).get("timezone", "UTC")
    try:
        query_tz = ZoneInfo(tz_name)
    except Exception:
        query_tz = timezone.utc

    ts_cur = conn.cursor()
    ts_cur.execute(
        f"SELECT COALESCE(c.project_id, 'unattributed'), c.request_timestamp FROM canonical_requests c WHERE {where_sql}",
        params,
    )
    project_dates: Dict[str, Set[str]] = {}
    project_has_ts: Dict[str, bool] = {}
    for pid, ts in ts_cur:
        if pid not in project_dates:
            project_dates[pid] = set()
            project_has_ts[pid] = False
        if ts:
            project_has_ts[pid] = True
            try:
                clean_ts = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local_dt = dt.astimezone(query_tz)
                project_dates[pid].add(local_dt.date().isoformat())
            except (ValueError, TypeError, OverflowError):
                pass

    result_rows = []
    total_observed = 0

    for r in rows:
        req_count = r[3] or 0
        total_observed += req_count
        fresh = r[5]
        cread = r[6]
        cw = r[7]
        outp = r[8]
        usd = r[9]
        inr = r[10]
        token_counts = {
            "fresh_input_tokens": r[13] or 0,
            "cache_read_tokens": r[14] or 0,
            "cache_write_tokens": (
                (r[15] or 0) + (r[16] or 0) + (r[17] or 0)
            ),
            "output_tokens": r[18] or 0,
        }
        token_values = {
            "fresh_input_tokens": fresh,
            "cache_read_tokens": cread,
            "cache_write_tokens": cw,
            "output_tokens": outp,
        }
        token_field_status = {
            field: _coverage_status(
                token_counts[field],
                req_count * (3 if field == "cache_write_tokens" else 1),
            )
            for field in token_values
        }
        observed_billable = [value for value in token_values.values() if value is not None]
        total_tokens = sum(observed_billable) if observed_billable else None
        token_observed_count = sum(token_counts.values())
        total_tokens_status = (
            "unavailable"
            if token_observed_count == 0
            else "exact" if token_observed_count == req_count * 6 else "partial"
        )
        token_field_status["total_tokens"] = total_tokens_status
        cache_denom = fresh + cread if fresh is not None and cread is not None else None
        hit_rate = (
            None
            if cache_denom is None
            else (cread / cache_denom) if cache_denom > 0 else 0.0
        )
        api_usd_status = _coverage_status(r[11] or 0, req_count)
        api_inr_status = _coverage_status(r[12] or 0, req_count)

        proj_id = r[0]
        has_ts = project_has_ts.get(proj_id, False)
        dates_set = project_dates.get(proj_id, set())
        if not has_ts or not dates_set:
            active_days = None
            active_days_status = "unavailable"
        else:
            active_days = len(dates_set)
            active_days_status = "exact"

        result_rows.append({
            "project_id": r[0],
            "project_name": r[1],
            "project_alias": r[2],
            "requests_count": req_count,
            "sessions_count": r[4] or 0,
            "total_tokens": total_tokens,
            "total_tokens_status": total_tokens_status,
            "token_fields_status": token_field_status,
            "fresh_input_tokens": fresh,
            "cache_read_tokens": cread,
            "cache_write_tokens": cw,
            "output_tokens": outp,
            "cache_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "api_equivalent_usd": round(usd, 4) if usd is not None else None,
            "api_equivalent_usd_status": api_usd_status,
            "api_equivalent_inr": round(inr, 2) if inr is not None else None,
            "api_equivalent_inr_status": api_inr_status,
            "active_days": active_days,
            "active_days_count": active_days,
            "active_days_count_status": active_days_status,
        })

    sort_info = norm_query.get("sort")
    if sort_info and sort_info.get("field"):
        fld = sort_info["field"]
        reverse = sort_info.get("order") == "desc"
        result_rows.sort(key=lambda x: (x.get(fld) is None, x.get(fld)), reverse=reverse)
    else:
        result_rows.sort(key=lambda x: (x.get("total_tokens") is None, x.get("total_tokens")), reverse=True)

    pag = norm_query.get("pagination", {"limit": 100, "offset": 0})
    limit = pag["limit"]
    offset = pag["offset"]
    paged_rows = result_rows[offset : offset + limit]

    all_dates = set.union(*project_dates.values()) if project_dates else set()
    total_has_ts = any(project_has_ts.values()) if project_has_ts else False
    total_active_days = len(all_dates) if (total_has_ts and all_dates) else None
    total_active_days_status = "exact" if total_active_days is not None else "unavailable"

    return {
        "values": {
            "active_days": total_active_days,
            "active_days_count": total_active_days,
            "active_days_count_status": total_active_days_status,
        },
        "rows": paged_rows,
        "eligible": len(result_rows),
        "observed": total_observed,
        "missingness": {},
        "coverage": {},
    }


def _query_sessions(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes searchable, sortable, paginated session table queries."""
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")

    sql = f"""
    SELECT
        s.session_id,
        s.origin_harness,
        s.started_at,
        s.ended_at,
        s.duration_ms,
        s.status,
        s.is_subagent,
        s.parent_session_id,
        p.project_name,
        COUNT(c.canonical_id) AS requests_count,
        CASE WHEN COUNT(u.fresh_input_tokens) + COUNT(u.cache_read_tokens)
                + COUNT(u.cache_write_5m_tokens) + COUNT(u.cache_write_1h_tokens)
                + COUNT(u.cache_write_unknown_ttl_tokens) + COUNT(u.output_tokens) > 0
            THEN SUM(COALESCE(u.fresh_input_tokens, 0) + COALESCE(u.cache_read_tokens, 0)
                + COALESCE(u.cache_write_5m_tokens, 0) + COALESCE(u.cache_write_1h_tokens, 0)
                + COALESCE(u.cache_write_unknown_ttl_tokens, 0) + COALESCE(u.output_tokens, 0))
            END AS total_tokens,
        SUM(f.api_equivalent_usd) AS api_equivalent_usd,
        GROUP_CONCAT(DISTINCT c.normalized_model) AS models_used,
        COUNT(u.fresh_input_tokens) + COUNT(u.cache_read_tokens)
            + COUNT(u.cache_write_5m_tokens) + COUNT(u.cache_write_1h_tokens)
            + COUNT(u.cache_write_unknown_ttl_tokens) + COUNT(u.output_tokens)
            AS observed_token_counters,
        COUNT(f.api_equivalent_usd) AS observed_api_equivalent,
        s.metadata_json,
        SUM(u.cache_read_tokens) AS cache_read_tokens,
        COUNT(u.cache_read_tokens) AS count_cache_read,
        CASE WHEN COUNT(u.cache_write_5m_tokens) + COUNT(u.cache_write_1h_tokens)
                + COUNT(u.cache_write_unknown_ttl_tokens) > 0
            THEN SUM(COALESCE(u.cache_write_5m_tokens, 0)
                + COALESCE(u.cache_write_1h_tokens, 0)
                + COALESCE(u.cache_write_unknown_ttl_tokens, 0)) END AS cache_write_tokens,
        COUNT(u.cache_write_5m_tokens) AS count_cache_write_5m,
        COUNT(u.cache_write_1h_tokens) AS count_cache_write_1h,
        COUNT(u.cache_write_unknown_ttl_tokens) AS count_cache_write_unknown,
        SUM(f.cache_savings_usd) AS cache_savings_usd,
        COUNT(f.cache_savings_usd) AS count_cache_savings
    FROM sessions s
    JOIN canonical_requests c ON s.session_id = c.session_id
    LEFT JOIN projects p ON s.project_id = p.project_id
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    GROUP BY s.session_id
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    result_rows = []

    for r in rows:
        models = r[12].split(",") if r[12] else []
        req_count = r[9] or 0
        token_status = _coverage_status(r[13] or 0, req_count * 6)
        api_status = _coverage_status(r[14] or 0, req_count)
        meta = {}
        if r[15]:
            try:
                meta = json.loads(r[15])
            except Exception:
                meta = {}
        title_val = meta.get("session_title") or meta.get("title")
        if title_val:
            session_title = {
                "status": "exact",
                "value": title_val,
                "reason": None,
            }
        else:
            session_title = {
                "status": "unavailable",
                "reason": "Canonical session title is not stored in sessions schema or metadata",
                "value": None,
            }

        cread = r[16]
        count_cread = r[17] or 0
        cwrite = r[18]
        count_cwrite = (r[19] or 0) + (r[20] or 0) + (r[21] or 0)
        cache_read_status = _coverage_status(count_cread, req_count)
        cache_write_status = _coverage_status(count_cwrite, req_count * 3)

        token_fields_status = {
            "total_tokens": token_status,
            "cache_read_tokens": cache_read_status,
            "cache_write_tokens": cache_write_status,
        }

        if len(models) == 1:
            primary_model = models[0]
            primary_model_status = "exact"
            primary_model_label = "primary"
        elif len(models) > 1:
            primary_model = [f"{m} (multi-model)" for m in models]
            primary_model_status = "multi-model"
            primary_model_label = "multi-model"
        else:
            primary_model = None
            primary_model_status = "unavailable"
            primary_model_label = "unavailable"

        app_basis = r[1]
        if app_basis:
            application_basis = app_basis
            application_basis_status = "exact"
        else:
            application_basis = None
            application_basis_status = "unavailable"

        cs_usd = r[22]
        count_cs = r[23] or 0
        cs_status = _coverage_status(count_cs, req_count)

        result_rows.append({
            "session_id": r[0],
            "origin_harness": r[1],
            "application_basis": application_basis,
            "application_basis_status": application_basis_status,
            "session_title": session_title,
            "session_title_status": session_title["status"],
            "session_title_reason": session_title["reason"],
            "primary_model": primary_model,
            "primary_model_status": primary_model_status,
            "primary_model_label": primary_model_label,
            "started_at": r[2],
            "ended_at": r[3],
            "duration_ms": r[4],
            "status": r[5],
            "is_subagent": bool(r[6]),
            "parent_session_id": r[7],
            "project_name": r[8],
            "requests_count": req_count,
            "total_tokens": r[10],
            "total_tokens_status": token_status,
            "cache_read_tokens": cread,
            "cache_read_tokens_status": cache_read_status,
            "cache_write_tokens": cwrite,
            "cache_write_tokens_status": cache_write_status,
            "cache_read": cread,
            "cache_write": cwrite,
            "cache_savings_usd": round(cs_usd, 4) if cs_usd is not None else None,
            "cache_savings_usd_status": cs_status,
            "token_fields_status": token_fields_status,
            "api_equivalent_usd": round(r[11], 4) if r[11] is not None else None,
            "api_equivalent_usd_status": api_status,
            "models_used": models,
        })

    # Sort
    sort_info = norm_query.get("sort")
    if sort_info and sort_info.get("field"):
        fld = sort_info["field"]
        reverse = sort_info.get("order") == "desc"
        result_rows.sort(key=lambda x: (x.get(fld) is None, x.get(fld)), reverse=reverse)
    else:
        result_rows.sort(key=lambda x: (x.get("started_at") is None, x.get("started_at")), reverse=True)

    pag = norm_query.get("pagination", {"limit": 100, "offset": 0})
    limit = pag["limit"]
    offset = pag["offset"]
    paged_rows = result_rows[offset : offset + limit]

    return {
        "values": {},
        "rows": paged_rows,
        "eligible": len(result_rows),
        "observed": len(result_rows),
        "missingness": {},
        "coverage": {},
    }


def _query_requests(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes individual canonical request inspector queries with usage and pricing facts."""
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")

    count_sql = f"SELECT COUNT(*) FROM canonical_requests c WHERE {where_sql}"
    cur = conn.cursor()
    cur.execute(count_sql, params)
    total_count = cur.fetchone()[0]

    # Sorting
    sort_info = norm_query.get("sort")
    order_clause = "ORDER BY c.request_timestamp DESC"
    if sort_info and sort_info.get("field"):
        fld = sort_info["field"]
        direction = "DESC" if sort_info.get("order") == "desc" else "ASC"
        # Map sort fields to SQL column names
        fld_map = {
            "canonical_id": "c.canonical_id",
            "request_timestamp": "c.request_timestamp",
            "duration_ms": "c.duration_ms",
            "ttft_ms": "c.ttft_ms",
            "generation_speed_tps": "c.generation_speed_tps",
            "total_tokens": "total_tokens",
            "output_tokens": "u.output_tokens",
            "fresh_input_tokens": "u.fresh_input_tokens",
            "api_equivalent_usd": "f.api_equivalent_usd",
        }
        col = fld_map.get(fld, "c.request_timestamp")
        order_clause = f"ORDER BY {col} {direction}"

    pag = norm_query.get("pagination", {"limit": 100, "offset": 0})
    limit = pag["limit"]
    offset = pag["offset"]

    sql = f"""
    SELECT
        c.canonical_id,
        c.request_timestamp,
        c.provider,
        c.origin_harness,
        c.access_route,
        c.raw_model,
        c.normalized_model,
        c.session_id,
        c.project_id,
        c.request_status,
        c.timing_basis,
        c.duration_ms,
        c.ttft_ms,
        c.generation_duration_ms,
        c.generation_speed_tps,
        c.is_unresolved_overlap,
        u.fresh_input_tokens,
        u.cache_read_tokens,
        u.cache_write_5m_tokens,
        u.cache_write_1h_tokens,
        u.cache_write_unknown_ttl_tokens,
        u.output_tokens,
        u.reasoning_tokens,
        CASE WHEN u.fresh_input_tokens IS NOT NULL AND u.cache_read_tokens IS NOT NULL
            AND u.cache_write_5m_tokens IS NOT NULL AND u.cache_write_1h_tokens IS NOT NULL
            AND u.cache_write_unknown_ttl_tokens IS NOT NULL AND u.output_tokens IS NOT NULL
            THEN u.fresh_input_tokens + u.cache_read_tokens + u.cache_write_5m_tokens
                + u.cache_write_1h_tokens + u.cache_write_unknown_ttl_tokens + u.output_tokens
            END AS total_tokens,
        f.pricing_status,
        f.api_equivalent_usd,
        f.api_equivalent_inr,
        f.cache_savings_usd,
        c.pricing_version_id,
        f.pricing_version_id,
        f.valuation_provenance_json,
        c.raw_payload_json
    FROM canonical_requests c
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    {order_clause}
    LIMIT :p_limit OFFSET :p_offset
    """
    exec_params = dict(params)
    exec_params["p_limit"] = limit
    exec_params["p_offset"] = offset

    cur.execute(sql, exec_params)
    rows = cur.fetchall()

    result_rows = []
    for r in rows:
        raw_payload = {}
        if r[31]:
            try:
                raw_payload = json.loads(r[31])
            except Exception:
                raw_payload = {}
        reasoning_val = raw_payload.get("reasoning_settings") or raw_payload.get("reasoning")
        if reasoning_val is not None:
            reasoning_settings = {
                "status": "exact",
                "value": reasoning_val,
                "reason": None,
            }
        else:
            reasoning_settings = {
                "status": "unavailable",
                "reason": "Canonical request schema and payload do not store reasoning settings",
                "value": None,
            }
        result_rows.append({
            "canonical_id": r[0],
            "request_timestamp": r[1],
            "provider": r[2],
            "origin_harness": r[3],
            "access_route": r[4],
            "raw_model": r[5],
            "normalized_model": r[6],
            "display_name": resolve_versioned_model_identity(conn, r[5], r[2], r[4], r[1], r[28]).display_name,
            "session_id": r[7],
            "project_id": r[8],
            "request_status": r[9],
            "timing_basis": r[10],
            "duration_ms": r[11],
            "ttft_ms": r[12],
            "generation_duration_ms": r[13],
            "generation_speed_tps": r[14],
            "is_unresolved_overlap": bool(r[15]),
            "fresh_input_tokens": r[16],
            "cache_read_tokens": r[17],
            "cache_write_5m_tokens": r[18],
            "cache_write_1h_tokens": r[19],
            "cache_write_unknown_ttl_tokens": r[20],
            "output_tokens": r[21],
            "reasoning_tokens": r[22],
            "total_tokens": r[23],
            "pricing_status": r[24] or "unpriced",
            "api_equivalent_usd": r[25],
            "api_equivalent_inr": r[26],
            "valuation_pricing_version_id": r[29],
            "valuation_provenance": json.loads(r[30]) if r[30] else None,
            "cache_savings_usd": r[27],
            "reasoning_settings": reasoning_settings,
            "reasoning_settings_status": reasoning_settings["status"],
            "reasoning_settings_reason": reasoning_settings["reason"],
        })

    return {
        "values": {},
        "rows": result_rows,
        "eligible": len(result_rows),
        "observed": total_count,
        "missingness": {},
        "coverage": {},
    }


def _query_events(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes trace events queries scoped by their session or linked canonical request."""
    clauses = ["1=1"]
    params: Dict[str, Any] = {}

    tr = norm_query.get("time_range", {})
    if tr.get("start"):
        clauses.append("e.started_at >= :ev_start")
        params["ev_start"] = tr["start"]
    if tr.get("end"):
        clauses.append("e.started_at < :ev_end")
        params["ev_end"] = tr["end"]

    filters = norm_query.get("filters", {})
    if "session" in filters:
        sess_vals = filters["session"]
        p_list = [f":ev_s_{i}" for i in range(len(sess_vals))]
        for i, s in enumerate(sess_vals):
            params[f"ev_s_{i}"] = s
        clauses.append(f"e.session_id IN ({', '.join(p_list)})")

    # Event rows only store session/request links. Apply request dimensions via the
    # linked request when present, or the event's session for session-level events.
    needs_request_scope = any(key != "session" for key in filters) or norm_query.get(
        "harness_scope", "combined"
    ) != "combined"
    if needs_request_scope:
        request_query = dict(norm_query)
        request_query["time_range"] = {}
        request_query["search"] = None
        request_where, request_params = _build_where_clause(
            request_query, table_prefix="matched_c", param_prefix="evc_"
        )
        params.update(request_params)
        clauses.append(
            "EXISTS (SELECT 1 FROM canonical_requests matched_c "
            f"WHERE {request_where} AND (matched_c.canonical_id = e.canonical_id "
            "OR (e.canonical_id IS NULL AND matched_c.session_id = e.session_id)))"
        )

    search = norm_query.get("search")
    if search:
        params["ev_search"] = f"%{search}%"
        clauses.append(
            "(e.event_id LIKE :ev_search OR e.event_name LIKE :ev_search OR e.event_kind LIKE :ev_search)"
        )

    where_sql = " AND ".join(clauses)

    count_sql = f"SELECT COUNT(*) FROM events e WHERE {where_sql}"
    cur = conn.cursor()
    cur.execute(count_sql, params)
    total_count = cur.fetchone()[0]

    pag = norm_query.get("pagination", {"limit": 100, "offset": 0})
    limit = pag["limit"]
    offset = pag["offset"]

    sql = f"""
    SELECT
        e.event_id,
        e.session_id,
        e.canonical_id,
        e.parent_event_id,
        e.event_kind,
        e.event_name,
        e.started_at,
        e.ended_at,
        e.duration_ms,
        e.output_size_bytes,
        e.status
    FROM events e
    WHERE {where_sql}
    ORDER BY e.started_at DESC
    LIMIT :limit OFFSET :offset
    """
    exec_params = dict(params)
    exec_params["limit"] = limit
    exec_params["offset"] = offset

    cur.execute(sql, exec_params)
    rows = cur.fetchall()

    result_rows = []
    for r in rows:
        result_rows.append({
            "event_id": r[0],
            "session_id": r[1],
            "canonical_id": r[2],
            "parent_event_id": r[3],
            "event_kind": r[4],
            "event_name": r[5],
            "started_at": r[6],
            "ended_at": r[7],
            "duration_ms": r[8],
            "output_size_bytes": r[9],
            "status": r[10],
        })

    return {
        "values": {},
        "rows": result_rows,
        "eligible": len(result_rows),
        "observed": total_count,
        "missingness": {},
        "coverage": {},
    }


def _query_coverage(
    conn: sqlite3.Connection,
    norm_query: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Executes coverage and source sync health queries distinguishing live vs archive."""
    if (
        norm_query.get("filters")
        or norm_query.get("search")
        or norm_query.get("harness_scope", "combined") != "combined"
        or norm_query.get("time_range", {}).get("start")
        or norm_query.get("time_range", {}).get("end")
    ):
        warnings.append(
            "Coverage is global source/checkpoint metadata; request filters, search, harness scope, "
            "and time range are unsupported and were not applied."
        )
    cur = conn.cursor()

    # 1. Checkpoints
    cur.execute("""
    SELECT c.source_adapter, c.origin_harness, c.account_alias, c.file_locator,
           c.last_scan_at, c.last_success_at, c.status, c.error_count, c.last_error,
           c.last_line_index, c.last_byte_offset, c.file_size,
           COUNT(o.observation_id),
           COUNT(CASE WHEN o.parse_status = 'ok' THEN 1 END),
           COUNT(CASE WHEN o.parse_status IN ('corrupt', 'parse_error') THEN 1 END),
           COUNT(CASE WHEN o.parse_status = 'skipped' THEN 1 END)
    FROM source_checkpoints c
    LEFT JOIN source_observations o
      ON o.file_locator = c.file_locator
     AND (o.source_adapter = c.source_adapter
          OR (c.source_adapter IN ('claude', 'claude-code', 'claude_code')
              AND o.source_adapter = 'claude_code'))
     AND o.file_generation IS c.file_generation
    GROUP BY c.source_adapter, c.file_locator
    ORDER BY c.last_scan_at DESC
    """)
    checkpoint_rows = cur.fetchall()
    checkpoints = []
    for r in checkpoint_rows:
        checkpoints.append({
            "source_adapter": r[0],
            "origin_harness": r[1],
            "account_alias": r[2],
            "file_locator": r[3],
            "last_scan_at": r[4],
            "last_success_at": r[5],
            "status": r[6],
            "error_count": r[7],
            "last_error": r[8],
            "last_line_index": r[9],
            "last_byte_offset": r[10],
            "file_size": r[11],
            "discovered_count": 1,
            "observations_count": r[12],
            "parsed_count": r[13],
            "unparseable_lines": r[14],
            "skipped_count": r[15],
            # observed_at can fall back to ingestion time; it is not event-time evidence.
            "newest_record_timestamp": None,
        })

    # 2. Archive Partitions
    cur.execute("""
    SELECT partition_id, partition_type, grain, source_name, timezone_name,
           start_time, end_time, total_tokens, total_cost_usd, records_count,
           pricing_status, is_reconciled, is_live_replaced, reconciliation_notes
    FROM archive_partitions
    ORDER BY start_time DESC
    """)
    arch_rows = cur.fetchall()
    partitions = []
    for r in arch_rows:
        partitions.append({
            "partition_id": r[0],
            "partition_type": r[1],
            "grain": r[2],
            "source_name": r[3],
            "timezone_name": r[4],
            "start_time": r[5],
            "end_time": r[6],
            "total_tokens": r[7],
            "total_cost_usd": r[8],
            "records_count": r[9],
            "pricing_status": r[10],
            "is_reconciled": bool(r[11]),
            "is_live_replaced": bool(r[12]),
            "reconciliation_notes": r[13],
        })

    # 3. Live spans
    cur.execute("SELECT MIN(request_timestamp), MAX(request_timestamp), COUNT(*) FROM canonical_requests")
    live_span = cur.fetchone()

    coverage_summary = {
        "live_requests_count": live_span[2] if live_span else 0,
        "live_earliest_utc": live_span[0] if live_span else None,
        "live_latest_utc": live_span[1] if live_span else None,
        "archive_partitions_count": len(partitions),
        "checkpoints_count": len(checkpoints),
    }

    return {
        "values": coverage_summary,
        "rows": partitions,
        "checkpoints": checkpoints,
        "eligible": len(partitions),
        "observed": len(partitions),
        "missingness": {},
        "coverage": coverage_summary,
    }


def execute_query(conn: sqlite3.Connection, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Executes a query envelope against the SQLite database.
    
    Args:
        conn: An open sqlite3.Connection to the token ledger database.
        envelope: Raw query request dict.
        
    Returns:
        Standard response dict echoing normalized query, values/rows, units, metric definition,
        missingness, coverage, pricing_fx, and warnings.
    """
    # 1. Validate envelope
    norm_query = validate_query(envelope)
    family = norm_query["family"]
    metric_id = norm_query["metric_id"]

    warnings: List[str] = []

    # Check DST warning
    if norm_query["time_range"].get("dst_transition"):
        warnings.append(
            f"Timezone transition (DST) occurs within the requested time range in {norm_query['time_range']['timezone']}."
        )

    # 2. Dispatch to family executor
    if family == "summary":
        result = _query_summary(conn, norm_query, warnings)
    elif family == "timeline":
        result = _query_timeline(conn, norm_query, warnings)
    elif family == "models":
        result = _query_models(conn, norm_query, warnings)
    elif family == "projects":
        result = _query_projects(conn, norm_query, warnings)
    elif family == "sessions":
        result = _query_sessions(conn, norm_query, warnings)
    elif family == "requests":
        result = _query_requests(conn, norm_query, warnings)
    elif family == "events":
        result = _query_events(conn, norm_query, warnings)
    elif family == "coverage":
        result = _query_coverage(conn, norm_query, warnings)
    elif family in ("tokens", "cache") or metric_id in ("tokens", "cache"):
        from token_ledger.metrics.tokens import analyze as analyze_tokens
        result = analyze_tokens(conn, norm_query)
    elif family in ("speed", "performance") or metric_id in ("speed", "performance", "generation_speed_tps"):
        from token_ledger.metrics.performance import analyze as analyze_performance
        result = analyze_performance(conn, norm_query)
    elif family in ("traces", "attribution") or metric_id in ("traces", "attribution"):
        from token_ledger.metrics.traces import analyze as analyze_traces
        result = analyze_traces(conn, norm_query)
    elif family in ("providers", "quotas", "burn", "live", "budgets") or metric_id in (
        "providers_breakdown", "provider_quotas", "token_burn_rate", "live_sessions", "usage_budgets"
    ):
        from token_ledger.metrics.providers import analyze as analyze_providers
        result = analyze_providers(conn, norm_query)
    elif family == "model_profiles" or metric_id == "model_profiles":
        from token_ledger.metrics.models import analyze as analyze_models
        result = analyze_models(conn, norm_query)
    elif family in ("outcomes", "task_outcomes") or metric_id in ("outcomes", "task_outcomes"):
        from token_ledger.metrics.outcomes import analyze as analyze_outcomes
        result = analyze_outcomes(conn, norm_query)
    elif family == "efficiency" or metric_id in ("efficiency", "efficiency_summary", "efficiency_scenarios"):
        try:
            from token_ledger.metrics.efficiency import analyze as analyze_efficiency
            result = analyze_efficiency(conn, norm_query)
        except ImportError:
            result = _query_summary(conn, norm_query, warnings)
    elif family in ("forecasts", "anomalies", "comparisons"):
        from token_ledger.metrics.forecast import analyze as analyze_forecast
        result = analyze_forecast(conn, norm_query)
    else:
        result = _query_summary(conn, norm_query, warnings)
    # 3. Resolve metric definition
    m_def = get_metric_definition(metric_id)
    metric_def_dict = m_def.to_dict() if m_def else {
        "metric_id": metric_id,
        "family": family,
        "version": "1.0",
        "counter_semantics": "disjoint_billable",
    }

    # 4. Pricing / FX information
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT rate_per_usd FROM historical_fx WHERE base_currency = 'USD' AND target_currency = 'INR' ORDER BY effective_date DESC LIMIT 5")
    fx_rows = cur.fetchall()
    fx_rates = [r[0] for r in fx_rows if r[0] is not None]

    cur.execute("SELECT DISTINCT pricing_hash FROM pricing_versions LIMIT 5")
    pv_rows = cur.fetchall()
    pricing_hashes = [r[0] for r in pv_rows if r[0] is not None]

    pricing_fx = {
        "fx_rates_available": fx_rates,
        "pricing_version_hashes": pricing_hashes,
    }

    response = dict(result)
    response["contract_version"] = "1"
    response["query"] = norm_query
    response["metric_id"] = metric_id
    response["family"] = family
    response["values"] = result.get("values", {})
    response["rows"] = result.get("rows", [])
    response["units"] = STANDARD_UNITS
    response["metric_definition"] = result.get("metric_definition") or metric_def_dict
    response["eligible"] = result.get("eligible", 0)
    response["observed"] = result.get("observed", 0)
    response["missingness"] = result.get("missingness", {})
    response["coverage"] = result.get("coverage", {})
    response["pricing_fx"] = result.get("pricing_fx") or pricing_fx

    all_warnings = list(warnings)
    if "warnings" in result and isinstance(result["warnings"], list):
        for w in result["warnings"]:
            if w not in all_warnings:
                all_warnings.append(w)

    # Invariant: Exact harness/timefilter invariants + unresolved overlap:
    # specialized totals must partial/null exactcombined when uncertain, not silently sum
    is_combined = norm_query.get("harness_scope") == "combined"
    cov = response.get("coverage", {})
    has_unresolved = False
    if isinstance(cov, dict):
        has_unresolved = bool(
            cov.get("has_unresolved_overlap", False)
            or (cov.get("unresolved_overlap_requests_count", 0) > 0)
            or (cov.get("unresolved_overlap_partitions_count", 0) > 0)
        )
    if not has_unresolved and is_combined:
        where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="ov_")
        cur.execute(f"SELECT COUNT(*) FROM canonical_requests c WHERE {where_sql} AND c.is_unresolved_overlap = 1", params)
        ov_count = cur.fetchone()[0]
        if ov_count > 0:
            has_unresolved = True
            if isinstance(cov, dict):
                cov["has_unresolved_overlap"] = True
                cov["unresolved_overlap_requests_count"] = ov_count

    if is_combined and has_unresolved:
        overlap_msg = "Unresolved overlap detected across combined harnesses: combined totals are marked partial and exact combined sum is withheld."
        if overlap_msg not in all_warnings:
            all_warnings.append(overlap_msg)
        if isinstance(response.get("values"), dict):
            response["values"]["total_tokens_status"] = "partial"
            response["values"]["has_unresolved_overlap"] = True

    response["warnings"] = all_warnings

    if "checkpoints" in result:
        response["checkpoints"] = result["checkpoints"]

    return response


def filtered_request_rows(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Retrieves all canonical request rows matching the envelope's filters and time range.

    Reuses validate_query to parse and validate incoming envelope.
    Joins canonical_requests with normalized_usage and financial_facts (1-to-1 joins).
    No pagination limit or offset is applied.
    Returns list of dicts with all identity, timing, token counter, and financial fields.
    """
    norm_query = validate_query(envelope)
    where_sql, params = _build_where_clause(norm_query, table_prefix="c", param_prefix="p_")

    resolver = _QueryModelIdentityResolver(conn)
    has_versions = bool(resolver._versions)
    jd_expr = "julianday(c.request_timestamp)" if has_versions else "NULL"

    sql = f"""
    SELECT
        c.canonical_id,
        c.request_timestamp,
        c.provider,
        c.account_alias,
        c.origin_harness,
        c.access_route,
        c.raw_model,
        c.normalized_model,
        c.session_id,
        c.project_id,
        c.prompt_id,
        c.request_status,
        c.timing_basis,
        c.duration_ms,
        c.ttft_ms,
        c.generation_duration_ms,
        c.generation_speed_tps,
        c.is_unresolved_overlap,
        u.fresh_input_tokens,
        u.cache_read_tokens,
        u.cache_write_5m_tokens,
        u.cache_write_1h_tokens,
        u.cache_write_unknown_ttl_tokens,
        u.output_tokens,
        u.reasoning_tokens,
        u.reported_total_tokens,
        CASE WHEN u.fresh_input_tokens IS NOT NULL AND u.cache_read_tokens IS NOT NULL
            AND u.cache_write_5m_tokens IS NOT NULL AND u.cache_write_1h_tokens IS NOT NULL
            AND u.cache_write_unknown_ttl_tokens IS NOT NULL AND u.output_tokens IS NOT NULL
            THEN u.fresh_input_tokens + u.cache_read_tokens + u.cache_write_5m_tokens
                + u.cache_write_1h_tokens + u.cache_write_unknown_ttl_tokens + u.output_tokens
            END AS total_tokens,
        f.pricing_status,
        f.api_equivalent_usd,
        f.api_equivalent_inr,
        f.actual_billed_amount,
        f.subscription_fee_usd,
        f.cache_savings_usd,
        c.pricing_version_id,
        c.parent_request_id,
        c.retry_attempt,
        {jd_expr},
        CASE WHEN c.raw_payload_json LIKE '%reasoning%' THEN c.raw_payload_json END
    FROM canonical_requests c
    LEFT JOIN normalized_usage u ON c.canonical_id = u.canonical_id
    LEFT JOIN financial_facts f ON c.canonical_id = f.canonical_id
    WHERE {where_sql}
    ORDER BY c.request_timestamp ASC
    """
    cur = conn.cursor()
    cur.row_factory = None
    cur.execute(sql, params)

    unavail_reasoning = {
        "status": "unavailable",
        "reason": "Canonical request schema and payload do not store reasoning settings",
        "value": None,
    }

    resolve = resolver.resolve
    identity_cache: Dict[Any, Any] = {}

    result_rows: List[Dict[str, Any]] = []
    append = result_rows.append
    for r in cur:
        raw_model = r[6]
        provider = r[2]
        route = r[5]
        v_id = r[33]
        jd = r[36]

        ckey = (raw_model, provider, route, jd if has_versions and not v_id else None, v_id)
        if ckey in identity_cache:
            display_name = identity_cache[ckey]
        else:
            display_name = resolve(raw_model, provider, route, jd, v_id).display_name
            identity_cache[ckey] = display_name

        raw_payload_json = r[37]
        if raw_payload_json is not None:
            try:
                raw_payload = json.loads(raw_payload_json)
                reasoning_val = raw_payload.get("reasoning_settings") or raw_payload.get("reasoning")
                if reasoning_val is not None:
                    reasoning_settings = {
                        "status": "exact",
                        "value": reasoning_val,
                        "reason": None,
                    }
                else:
                    reasoning_settings = unavail_reasoning
            except Exception:
                reasoning_settings = unavail_reasoning
        else:
            reasoning_settings = unavail_reasoning

        append({
            "canonical_id": r[0],
            "request_timestamp": r[1],
            "provider": provider,
            "account_alias": r[3],
            "origin_harness": r[4],
            "access_route": route,
            "raw_model": raw_model,
            "normalized_model": r[7],
            "pricing_version_id": v_id,
            "display_name": display_name,
            "parent_request_id": r[34],
            "retry_attempt": r[35],
            "session_id": r[8],
            "project_id": r[9],
            "prompt_id": r[10],
            "request_status": r[11],
            "timing_basis": r[12],
            "duration_ms": r[13],
            "ttft_ms": r[14],
            "generation_duration_ms": r[15],
            "generation_speed_tps": r[16],
            "is_unresolved_overlap": bool(r[17]),
            "fresh_input_tokens": r[18],
            "cache_read_tokens": r[19],
            "cache_write_5m_tokens": r[20],
            "cache_write_1h_tokens": r[21],
            "cache_write_unknown_ttl_tokens": r[22],
            "output_tokens": r[23],
            "reasoning_tokens": r[24],
            "reported_total_tokens": r[25],
            "total_tokens": r[26],
            "pricing_status": r[27] or "unpriced",
            "api_equivalent_usd": r[28],
            "api_equivalent_inr": r[29],
            "actual_billed_amount": r[30],
            "subscription_fee_usd": r[31],
            "cache_savings_usd": r[32],
            "reasoning_settings": reasoning_settings,
            "reasoning_settings_status": reasoning_settings["status"],
            "reasoning_settings_reason": reasoning_settings["reason"],
        })
    return result_rows
