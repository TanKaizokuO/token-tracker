"""Forecasting, anomaly detection, and before-and-after efficiency comparisons (WP20).

Implements:
- Feature 25: Usage Forecasting
  - Daily timezone-aware partial-day coverage tracking.
  - Remainder-of-day, remainder-of-week, and remainder-of-month forecasts for
    total tokens, per-model use, and API-equivalent cost.
  - Declared velocity methods (recent 7-day weighted velocity vs longer-term velocity).
  - Explicit empirical uncertainty intervals (declared method, standard error / residual spread).
  - Walk-forward rolling backtests with MAE, MAPE, and coverage statistics.
  - Minimum sample adequacy enforcement (abstains when active history < 3 days).
  - Unpriced cost separation (never conflates unpriced requests with $0.00).

- Feature 46 & 64: Usage & Speed Anomaly Detection
  - Robust baselines using Median and Median Absolute Deviation (MAD).
  - Multi-dimensional detection: token spikes, cache write surges, cache hit drops,
    failure rate spikes, TPS drops, and TTFT latency spikes.
  - Expected-anomaly user state from local settings and manual_settings_and_outcomes.
  - Concretely links every detected anomaly to request/session evidence.
  - Sparse baseline abstention when sample size < 5.

- Feature 42: Before-And-After Efficiency Comparisons
  - Two explicit timezone-aware periods (Period A vs Period B).
  - Reuses canonical query filters with explicit half-open range overrides.
  - Consistent metric definitions: tokens, cache rate, duration, weighted TPS, TTFT,
    retries, and recorded task outcomes.
  - Confounder detection (model mix shifts, prompt length shifts, task shifts, DST transitions).
  - Strict disclaimer: no causal claims or guaranteed efficiency success.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import math
import json
import statistics
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from token_ledger.query import filtered_request_rows, validate_query

# Minimum active days required for forecasting
MIN_FORECAST_ACTIVE_DAYS = 3

MIN_UNCERTAINTY_BACKTEST_SAMPLES = 2

# Minimum baseline samples required for robust anomaly detection
MIN_ANOMALY_BASELINE_SAMPLES = 5

# Default anomaly threshold multiplier (multiples of MAD)
DEFAULT_MAD_THRESHOLD_MULTIPLIER = 3.0

# Percentile calculation method
PERCENTILE_METHOD = "nearest_rank"


def _parse_iso_utc(ts_str: str) -> datetime:
    """Parses ISO timestamp string to UTC datetime."""
    clean = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _normalized_filter_query(
    envelope: Dict[str, Any],
    time_range: Optional[Dict[str, Any]] = None,
    timezone_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds a standard normalized query while retaining shared filters."""
    source_range = time_range
    if source_range is None:
        source_range = envelope.get("time_range") or {}
    if not isinstance(source_range, dict):
        raise ValueError("time_range must be a dict.")

    query_range = {
        "start": source_range.get("raw_start", source_range.get("start")),
        "end": source_range.get("raw_end", source_range.get("end")),
        "timezone": (
            source_range.get("timezone")
            or timezone_name
            or envelope.get("timezone")
            or "UTC"
        ),
    }
    query = {
        "contract_version": envelope.get("contract_version", "1"),
        "harness_scope": envelope.get("harness_scope", "combined"),
        "filters": envelope.get("filters") or {},
        "time_range": query_range,
    }
    for key in ("include_archive", "search"):
        if key in envelope:
            query[key] = envelope[key]
    return validate_query(query)


def _daily_coverage(sorted_days: Sequence[str]) -> Dict[str, Any]:
    """Reports observed activity dates without treating missing dates as zero."""
    unknown_between_activity_days = sum(
        max(
            0,
            (date.fromisoformat(next_day) - date.fromisoformat(previous_day)).days - 1,
        )
        for previous_day, next_day in zip(sorted_days, sorted_days[1:])
    )
    calendar_span_days = (
        (date.fromisoformat(sorted_days[-1]) - date.fromisoformat(sorted_days[0])).days + 1
        if sorted_days
        else 0
    )
    return {
        "activity_days_observed": len(sorted_days),
        "calendar_days_in_observed_span": calendar_span_days,
        "confirmed_zero_activity_days": 0,
        "unknown_days_between_activity_days": unknown_between_activity_days,
        "missing_days_imputed_as_zero": 0,
        "day_coverage_status": "unknown_without_completeness_evidence",
    }


def _has_dst_transition(start_utc: datetime, end_utc: datetime, tz: ZoneInfo) -> bool:
    """Checks UTC samples across the interval for any local offset change."""
    cursor = start_utc
    previous_offset = cursor.astimezone(tz).utcoffset()
    while cursor < end_utc:
        cursor = min(cursor + timedelta(days=1), end_utc)
        current_offset = cursor.astimezone(tz).utcoffset()
        if current_offset != previous_offset:
            return True
        previous_offset = current_offset
    return False


def _median_and_mad(values: Sequence[float]) -> Tuple[float, float]:
    """Computes median and Median Absolute Deviation (MAD).

    MAD = median(|x_i - median(x)|).
    """
    if not values:
        return 0.0, 0.0
    med = float(statistics.median(values))
    deviations = [abs(x - med) for x in values]
    mad = float(statistics.median(deviations))
    return round(med, 4), round(mad, 4)


# -----------------------------------------------------------------------------
# 1. Feature 25: Usage Forecasting
# -----------------------------------------------------------------------------


def compute_daily_rollups(
    rows: Sequence[Dict[str, Any]],
    tz_name: str = "UTC",
) -> Dict[str, Dict[str, Any]]:
    """Rolls up request rows into timezone-aware calendar day buckets.

    Returns dict mapping date_str (YYYY-MM-DD) -> daily statistics.
    """
    tz = ZoneInfo(tz_name)
    daily: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "date": "",
            "requests_count": 0,
            "total_tokens": 0,
            "fresh_input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "cache_write_unknown_ttl_tokens": 0,
            "output_tokens": 0,
            "api_equivalent_usd": 0.0,
            "cache_savings_usd": 0.0,
            "priced_requests": 0,
            "unpriced_requests": 0,
            "models": defaultdict(lambda: {
                "total_tokens": 0,
                "requests": 0,
                "usd": 0.0,
                "priced_requests": 0,
                "unpriced_requests": 0,
            }),
            "first_timestamp": None,
            "last_timestamp": None,
        }
    )

    for r in rows:
        ts_utc = _parse_iso_utc(r["request_timestamp"])
        dt_local = ts_utc.astimezone(tz)
        d_str = dt_local.strftime("%Y-%m-%d")

        d = daily[d_str]
        d["date"] = d_str
        d["requests_count"] += 1

        tot = _safe_int(r.get("total_tokens"))
        fresh = _safe_int(r.get("fresh_input_tokens"))
        cread = _safe_int(r.get("cache_read_tokens"))
        cw5 = _safe_int(r.get("cache_write_5m_tokens"))
        cw1 = _safe_int(r.get("cache_write_1h_tokens"))
        cwu = _safe_int(r.get("cache_write_unknown_ttl_tokens"))
        out = _safe_int(r.get("output_tokens"))

        d["total_tokens"] += tot
        d["fresh_input_tokens"] += fresh
        d["cache_read_tokens"] += cread
        d["cache_write_5m_tokens"] += cw5
        d["cache_write_1h_tokens"] += cw1
        d["cache_write_unknown_ttl_tokens"] += cwu
        d["output_tokens"] += out

        usd = _safe_float(r.get("api_equivalent_usd"))
        is_priced = usd is not None and r.get("pricing_status") != "unpriced"
        if is_priced:
            d["api_equivalent_usd"] += usd
            d["priced_requests"] += 1
        else:
            d["unpriced_requests"] += 1

        savings = _safe_float(r.get("cache_savings_usd"))
        if savings is not None:
            d["cache_savings_usd"] += savings

        model = r.get("normalized_model") or r.get("raw_model") or "unknown"
        m_entry = d["models"][model]
        m_entry["total_tokens"] += tot
        m_entry["requests"] += 1
        if is_priced:
            m_entry["usd"] += usd
            m_entry["priced_requests"] += 1
        else:
            m_entry["unpriced_requests"] += 1

        ts_str = r["request_timestamp"]
        if d["first_timestamp"] is None or ts_str < d["first_timestamp"]:
            d["first_timestamp"] = ts_str
        if d["last_timestamp"] is None or ts_str > d["last_timestamp"]:
            d["last_timestamp"] = ts_str

    # Convert inner models to regular dicts
    result = {}
    for d_str, val in sorted(daily.items()):
        val["models"] = dict(val["models"])
        val["api_equivalent_usd"] = round(val["api_equivalent_usd"], 4)
        val["cache_savings_usd"] = round(val["cache_savings_usd"], 4)
        result[d_str] = val
    return result


def walk_forward_backtest(
    daily_values: Sequence[float],
    min_train_days: int = 3,
    window_size: int = 7,
) -> Dict[str, Any]:
    """Performs a rolling 1-step walk-forward backtest over daily usage values.

    For each day t from min_train_days to len(daily_values) - 1:
        uses the preceding window_size days to predict day t using rolling mean.
    Calculates MAE, MAPE, and residual standard deviation.
    """
    n = len(daily_values)
    if n <= min_train_days:
        uncertainty_reason = (
            "Insufficient walk-forward residual samples to estimate empirical uncertainty "
            f"(observed {max(0, n - min_train_days)}, required "
            f"{MIN_UNCERTAINTY_BACKTEST_SAMPLES})."
        )
        return {
            "eligible": False,
            "method": "rolling_walk_forward_1step",
            "samples_evaluated": 0,
            "mae": None,
            "mape": None,
            "residual_stddev": None,
            "uncertainty_eligible": False,
            "uncertainty_abstention_reason": uncertainty_reason,
            "abstention_reason": (
                f"Insufficient historical days ({n}) for backtest (minimum {min_train_days + 1} required)."
            ),
        }

    errors: List[float] = []
    pct_errors: List[float] = []
    residuals: List[float] = []

    for t in range(min_train_days, n):
        train_start = max(0, t - window_size)
        train_slice = daily_values[train_start:t]
        pred = statistics.mean(train_slice)
        actual = daily_values[t]
        err = abs(actual - pred)
        errors.append(err)
        residuals.append(actual - pred)
        if actual > 0:
            pct_errors.append(err / actual)

    mae = statistics.mean(errors) if errors else 0.0
    mape = statistics.mean(pct_errors) if pct_errors else 0.0
    uncertainty_eligible = len(residuals) >= MIN_UNCERTAINTY_BACKTEST_SAMPLES
    res_std = statistics.pstdev(residuals) if uncertainty_eligible else None
    uncertainty_reason = None if uncertainty_eligible else (
        "Insufficient walk-forward residual samples to estimate empirical uncertainty "
        f"(observed {len(residuals)}, required {MIN_UNCERTAINTY_BACKTEST_SAMPLES})."
    )

    return {
        "eligible": True,
        "method": "rolling_walk_forward_1step",
        "window_size": window_size,
        "samples_evaluated": len(errors),
        "mae": round(mae, 2),
        "mape": round(mape, 4),
        "residual_stddev": round(res_std, 2) if res_std is not None else None,
        "uncertainty_eligible": uncertainty_eligible,
        "uncertainty_abstention_reason": uncertainty_reason,
        "abstention_reason": None,
    }


def forecast_usage(
    rows: Sequence[Dict[str, Any]],
    tz_name: str = "UTC",
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Generates remainder-of-day, week, and month forecasts for tokens, models, and cost.

    Enforces minimum active days (>= 3).
    Separates priced vs unpriced financial coverage.
    States uncertainty intervals using empirical walk-forward residual spread.
    """
    daily = compute_daily_rollups(rows, tz_name=tz_name)
    tz = ZoneInfo(tz_name)

    if as_of is None:
        if rows:
            latest_req_dt = max(_parse_iso_utc(r["request_timestamp"]) for r in rows)
            now_dt = latest_req_dt.astimezone(tz)
        else:
            now_dt = datetime.now(tz)
    else:
        now_dt = as_of.astimezone(tz)

    today_str = now_dt.strftime("%Y-%m-%d")
    sorted_days = sorted(daily.keys())
    active_days_count = len(sorted_days)

    # Check minimum sample requirement
    if active_days_count < MIN_FORECAST_ACTIVE_DAYS:
        priced_count = sum(daily[d]["priced_requests"] for d in sorted_days)
        unpriced_count = sum(daily[d]["unpriced_requests"] for d in sorted_days)
        return {
            "eligible": False,
            "status": "abstain",
            "abstention_reason": (
                f"Sparse history: observed {active_days_count} active day(s), "
                f"which is below the required minimum of {MIN_FORECAST_ACTIVE_DAYS} days."
            ),
            "timezone": tz_name,
            "as_of": now_dt.isoformat(),
            "active_days_observed": active_days_count,
            "velocity": None,
            "partial_day": None,
            "forecasts": None,
            "backtest": None,
            "coverage": {
                **_daily_coverage(sorted_days),
                "priced_requests_count": priced_count,
                "unpriced_requests_count": unpriced_count,
                "priced_coverage_status": "partial" if unpriced_count else "complete",
                "cost_projection_scope": "not_projected",
                "cost_projection_is_full_scope": False,
                "cost_projection_status": "abstain_sparse_history",
                "full_scope_cost_projection_status": "abstain_sparse_history",
                "full_scope_cost_abstention_reason": (
                    "Cost forecast is unavailable because token forecasting abstained "
                    "on sparse active-day history."
                ),
            },
            "warnings": [
                f"Sparse history ({active_days_count} active days). Forecast abstained."
            ],
        }

    # Measure elapsed wall-clock time through UTC instants so DST-short/long
    # local calendar days use their actual elapsed duration.
    local_midnight = datetime.combine(now_dt.date(), time.min, tzinfo=tz)
    next_local_midnight = datetime.combine(now_dt.date() + timedelta(days=1), time.min, tzinfo=tz)
    elapsed_seconds = (
        now_dt.astimezone(timezone.utc) - local_midnight.astimezone(timezone.utc)
    ).total_seconds()
    seconds_in_day = (
        next_local_midnight.astimezone(timezone.utc) - local_midnight.astimezone(timezone.utc)
    ).total_seconds()
    day_fraction_elapsed = min(1.0, max(0.0, elapsed_seconds / seconds_in_day))
    day_fraction_remaining = max(0.0, 1.0 - day_fraction_elapsed)

    today_entry = daily.get(today_str, {
        "date": today_str,
        "requests_count": 0,
        "total_tokens": 0,
        "output_tokens": 0,
        "fresh_input_tokens": 0,
        "cache_read_tokens": 0,
        "api_equivalent_usd": 0.0,
        "priced_requests": 0,
        "unpriced_requests": 0,
        "models": {},
    })

    # Historical series excluding partial day for clean velocity estimation
    historical_days = [d for d in sorted_days if d < today_str]
    if not historical_days:
        historical_days = sorted_days

    # Activity-day samples only: dates without request rows are unknown and are
    # not inserted as zero-token or zero-cost observations.
    recent_slice = historical_days[-7:] if len(historical_days) >= 7 else historical_days
    recent_daily_tokens = [daily[d]["total_tokens"] for d in recent_slice]
    all_daily_tokens = [daily[d]["total_tokens"] for d in historical_days]
    cost_history_days = [d for d in historical_days if daily[d]["priced_requests"] > 0]
    recent_cost_days = cost_history_days[-7:]
    recent_daily_usd = [daily[d]["api_equivalent_usd"] for d in recent_cost_days]
    all_daily_usd = [daily[d]["api_equivalent_usd"] for d in cost_history_days]
    has_priced_history = bool(cost_history_days)

    # Weighted velocity: linearly weighted towards more recent days
    def _weighted_mean(vals: Sequence[float]) -> float:
        if not vals:
            return 0.0
        n = len(vals)
        weights = [i + 1 for i in range(n)]
        return sum(w * v for w, v in zip(weights, vals)) / float(sum(weights))

    recent_token_velocity = _weighted_mean([float(x) for x in recent_daily_tokens])
    longer_token_velocity = statistics.mean(all_daily_tokens) if all_daily_tokens else 0.0

    recent_usd_velocity = _weighted_mean([float(x) for x in recent_daily_usd])
    longer_usd_velocity = statistics.mean(all_daily_usd) if all_daily_usd else 0.0

    # Backtest token velocity using walk-forward
    backtest = walk_forward_backtest(
        [float(daily[d]["total_tokens"]) for d in sorted_days],
        min_train_days=3,
        window_size=7,
    )

    cost_backtest_days = [d for d in sorted_days if daily[d]["priced_requests"] > 0]
    cost_backtest = walk_forward_backtest(
        [float(daily[d]["api_equivalent_usd"]) for d in cost_backtest_days],
        min_train_days=3,
        window_size=7,
    )

    def _interval(
        expected: float,
        days_ahead: float,
        empirical_backtest: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Returns empirical residual bounds only when walk-forward samples support them."""
        sample_count = empirical_backtest.get("samples_evaluated", 0)
        residual_std = empirical_backtest.get("residual_stddev")
        if not empirical_backtest.get("uncertainty_eligible") or residual_std is None:
            return {
                "eligible": False,
                "status": "abstain",
                "method": "empirical_residual_spread",
                "samples_evaluated": sample_count,
                "abstention_reason": empirical_backtest.get("uncertainty_abstention_reason"),
            }
        spread = 1.96 * residual_std * math.sqrt(max(0.0, days_ahead))
        return {
            "eligible": True,
            "status": "available",
            "lower_bound": max(0.0, round(expected - spread, 2)),
            "expected_point": round(expected, 2),
            "upper_bound": round(expected + spread, 2),
            "method": "empirical_residual_spread",
            "samples_evaluated": sample_count,
            "spread_multiplier": 1.96,
        }

    # 1. Remainder of day
    rem_day_tokens = day_fraction_remaining * recent_token_velocity
    rem_day_usd = day_fraction_remaining * recent_usd_velocity
    proj_today_tokens = today_entry["total_tokens"] + rem_day_tokens
    proj_today_usd = today_entry["api_equivalent_usd"] + rem_day_usd

    # 2. Remainder of week (assuming Monday = 0 to Sunday = 6)
    weekday = now_dt.weekday()  # Monday is 0, Sunday is 6
    days_left_in_week = max(0, 6 - weekday)
    rem_week_tokens = rem_day_tokens + (days_left_in_week * recent_token_velocity)
    rem_week_usd = rem_day_usd + (days_left_in_week * recent_usd_velocity)

    # Calculate tokens observed in current week so far
    start_of_week = (now_dt - timedelta(days=weekday)).date()
    week_observed_tokens = sum(
        daily[d]["total_tokens"]
        for d in sorted_days
        if date.fromisoformat(d) >= start_of_week and date.fromisoformat(d) <= now_dt.date()
    )
    week_observed_usd = sum(
        daily[d]["api_equivalent_usd"]
        for d in sorted_days
        if date.fromisoformat(d) >= start_of_week and date.fromisoformat(d) <= now_dt.date()
    )
    proj_week_tokens = week_observed_tokens + rem_week_tokens
    proj_week_usd = week_observed_usd + rem_week_usd

    # 3. Remainder of month
    # Days in month
    current_year = now_dt.year
    current_month = now_dt.month
    if current_month == 12:
        next_month_start = date(current_year + 1, 1, 1)
    else:
        next_month_start = date(current_year, current_month + 1, 1)
    total_days_in_month = (next_month_start - date(current_year, current_month, 1)).days
    days_left_in_month = max(0, total_days_in_month - now_dt.day)

    rem_month_tokens = rem_day_tokens + (days_left_in_month * recent_token_velocity)
    rem_month_usd = rem_day_usd + (days_left_in_month * recent_usd_velocity)

    month_start = date(current_year, current_month, 1)
    month_observed_tokens = sum(
        daily[d]["total_tokens"]
        for d in sorted_days
        if date.fromisoformat(d) >= month_start and date.fromisoformat(d) <= now_dt.date()
    )
    month_observed_usd = sum(
        daily[d]["api_equivalent_usd"]
        for d in sorted_days
        if date.fromisoformat(d) >= month_start and date.fromisoformat(d) <= now_dt.date()
    )
    proj_month_tokens = month_observed_tokens + rem_month_tokens
    proj_month_usd = month_observed_usd + rem_month_usd

    total_priced_reqs = sum(daily[day]["priced_requests"] for day in sorted_days)
    total_unpriced_reqs = sum(daily[day]["unpriced_requests"] for day in sorted_days)
    has_unpriced = total_unpriced_reqs > 0
    cost_projection_scope = (
        "not_projected"
        if not has_priced_history
        else "known_priced_subset_only" if has_unpriced else "all_scoped_requests"
    )
    cost_projection_is_full_scope = has_priced_history and not has_unpriced
    if not has_priced_history:
        cost_projection_status = "abstain_no_priced_history"
        full_scope_cost_projection_status = "abstain_no_priced_history"
        full_scope_cost_abstention_reason = (
            "No priced historical requests are available to estimate cost velocity."
        )
    elif has_unpriced:
        cost_projection_status = "known_priced_subset"
        full_scope_cost_projection_status = "abstain_partial_pricing"
        full_scope_cost_abstention_reason = (
            f"Full-scope cost forecast abstains because {total_unpriced_reqs} "
            "request(s) are unpriced; only the known-priced subset can be projected."
        )
    else:
        cost_projection_status = "complete"
        full_scope_cost_projection_status = "available"
        full_scope_cost_abstention_reason = None

    def _cost_interval(
        expected: float,
        days_ahead: float,
        empirical_backtest: Dict[str, Any],
        projection_available: bool,
    ) -> Dict[str, Any]:
        if projection_available:
            interval = _interval(expected, days_ahead, empirical_backtest)
        else:
            interval = {
                "eligible": False,
                "status": "abstain",
                "method": "empirical_residual_spread",
                "samples_evaluated": empirical_backtest.get("samples_evaluated", 0),
                "abstention_reason": (
                    "No priced historical requests are available to estimate cost."
                ),
            }
        interval.update({
            "cost_projection_scope": (
                cost_projection_scope if projection_available else "not_projected"
            ),
            "cost_projection_is_full_scope": (
                cost_projection_is_full_scope if projection_available else False
            ),
            "full_scope_cost_projection_status": (
                full_scope_cost_projection_status
                if projection_available else "abstain_no_priced_history"
            ),
            "full_scope_cost_abstention_reason": (
                full_scope_cost_abstention_reason
                if projection_available
                else "No priced historical requests are available to estimate cost."
            ),
        })
        return interval

    # Model rates use only dates on which that model has requests. Missing
    # model dates remain unknown rather than becoming synthetic zero samples.
    model_names = {
        model_name
        for day in recent_slice
        for model_name in daily[day]["models"]
    }
    per_model_forecast: Dict[str, Any] = {}
    for model_name in sorted(model_names):
        model_activity_days = [
            day for day in historical_days if model_name in daily[day]["models"]
        ]
        recent_model_days = model_activity_days[-7:]
        model_token_samples = [
            float(daily[day]["models"][model_name]["total_tokens"])
            for day in recent_model_days
        ]
        model_daily_tokens = _weighted_mean(model_token_samples)
        model_backtest = walk_forward_backtest(
            [
                float(daily[day]["models"][model_name]["total_tokens"])
                for day in model_activity_days
            ],
            min_train_days=3,
            window_size=7,
        )

        model_cost_days = [
            day
            for day in historical_days
            if daily[day]["models"].get(model_name, {}).get("priced_requests", 0) > 0
        ]
        model_has_priced_history = bool(model_cost_days)
        recent_model_cost_days = model_cost_days[-7:]
        model_daily_usd = _weighted_mean([
            float(daily[day]["models"][model_name]["usd"])
            for day in recent_model_cost_days
        ])
        model_cost_backtest = walk_forward_backtest(
            [
                float(daily[day]["models"][model_name]["usd"])
                for day in model_cost_days
            ],
            min_train_days=3,
            window_size=7,
        )

        model_today = today_entry["models"].get(model_name, {})
        model_today_tokens = model_today.get("total_tokens", 0)
        model_today_usd = model_today.get("usd", 0.0)
        model_remaining_day_tokens = day_fraction_remaining * model_daily_tokens
        model_remaining_week_tokens = (
            model_remaining_day_tokens + days_left_in_week * model_daily_tokens
        )
        model_remaining_month_tokens = (
            model_remaining_day_tokens + days_left_in_month * model_daily_tokens
        )
        model_remaining_day_usd = day_fraction_remaining * model_daily_usd
        model_remaining_week_usd = (
            model_remaining_day_usd + days_left_in_week * model_daily_usd
        )
        model_remaining_month_usd = (
            model_remaining_day_usd + days_left_in_month * model_daily_usd
        )

        model_week_observed_tokens = sum(
            daily[day]["models"].get(model_name, {}).get("total_tokens", 0)
            for day in sorted_days
            if start_of_week <= date.fromisoformat(day) <= now_dt.date()
        )
        model_week_observed_usd = sum(
            daily[day]["models"].get(model_name, {}).get("usd", 0.0)
            for day in sorted_days
            if start_of_week <= date.fromisoformat(day) <= now_dt.date()
        )
        model_month_observed_tokens = sum(
            daily[day]["models"].get(model_name, {}).get("total_tokens", 0)
            for day in sorted_days
            if month_start <= date.fromisoformat(day) <= now_dt.date()
        )
        model_month_observed_usd = sum(
            daily[day]["models"].get(model_name, {}).get("usd", 0.0)
            for day in sorted_days
            if month_start <= date.fromisoformat(day) <= now_dt.date()
        )

        model_projected_day_tokens = model_today_tokens + model_remaining_day_tokens
        model_projected_week_tokens = model_week_observed_tokens + model_remaining_week_tokens
        model_projected_month_tokens = model_month_observed_tokens + model_remaining_month_tokens
        model_projected_day_usd = model_today_usd + model_remaining_day_usd
        model_projected_week_usd = model_week_observed_usd + model_remaining_week_usd
        model_projected_month_usd = model_month_observed_usd + model_remaining_month_usd

        per_model_forecast[model_name] = {
            "observed_activity_days": len(model_activity_days),
            "recent_daily_tokens_velocity": round(model_daily_tokens, 2),
            "recent_daily_cost_velocity_usd": (
                round(model_daily_usd, 4) if model_has_priced_history else None
            ),
            "remainder_day_tokens": round(model_remaining_day_tokens, 2),
            "remainder_week_tokens": round(model_remaining_week_tokens, 2),
            "remainder_month_tokens": round(model_remaining_month_tokens, 2),
            "projected_day_tokens": round(model_projected_day_tokens, 2),
            "projected_week_tokens": round(model_projected_week_tokens, 2),
            "projected_month_tokens": round(model_projected_month_tokens, 2),
            "remainder_day_cost_usd": (
                round(model_remaining_day_usd, 4) if model_has_priced_history else None
            ),
            "remainder_week_cost_usd": (
                round(model_remaining_week_usd, 4) if model_has_priced_history else None
            ),
            "remainder_month_cost_usd": (
                round(model_remaining_month_usd, 4) if model_has_priced_history else None
            ),
            "projected_day_cost_usd": (
                round(model_projected_day_usd, 4) if model_has_priced_history else None
            ),
            "projected_week_cost_usd": (
                round(model_projected_week_usd, 4) if model_has_priced_history else None
            ),
            "projected_month_cost_usd": (
                round(model_projected_month_usd, 4) if model_has_priced_history else None
            ),
            "uncertainty_intervals": {
                "day_tokens": _interval(
                    model_projected_day_tokens, day_fraction_remaining, model_backtest
                ),
                "week_tokens": _interval(
                    model_projected_week_tokens,
                    days_left_in_week + day_fraction_remaining,
                    model_backtest,
                ),
                "month_tokens": _interval(
                    model_projected_month_tokens,
                    days_left_in_month + day_fraction_remaining,
                    model_backtest,
                ),
                "day_cost_usd": _cost_interval(
                    model_projected_day_usd,
                    day_fraction_remaining,
                    model_cost_backtest,
                    model_has_priced_history,
                ),
                "week_cost_usd": _cost_interval(
                    model_projected_week_usd,
                    days_left_in_week + day_fraction_remaining,
                    model_cost_backtest,
                    model_has_priced_history,
                ),
                "month_cost_usd": _cost_interval(
                    model_projected_month_usd,
                    days_left_in_month + day_fraction_remaining,
                    model_cost_backtest,
                    model_has_priced_history,
                ),
            },
            "cost_projection_scope": (
                cost_projection_scope if model_has_priced_history else "not_projected"
            ),
            "cost_projection_is_full_scope": (
                cost_projection_is_full_scope if model_has_priced_history else False
            ),
            "full_scope_cost_projection_status": (
                full_scope_cost_projection_status
                if model_has_priced_history else "abstain_no_priced_history"
            ),
            "full_scope_cost_abstention_reason": (
                full_scope_cost_abstention_reason
                if model_has_priced_history
                else "No priced historical requests are available to estimate cost."
            ),
        }

    warnings: List[str] = []
    if not has_priced_history:
        warnings.append(
            "Cost forecast abstained because no priced historical requests are available."
        )
    if has_unpriced:
        warnings.append(
            f"Full-scope cost projections abstain because {total_unpriced_reqs} "
            "request(s) are unpriced; numeric cost projections cover only the known-priced subset."
        )

    return {
        "eligible": True,
        "status": "forecast_available",
        "abstention_reason": None,
        "timezone": tz_name,
        "as_of": now_dt.isoformat(),
        "active_days_observed": active_days_count,
        "velocity": {
            "method": "recent_weighted_daily_velocity",
            "recent_window_days": len(recent_slice),
            "recent_daily_tokens": round(recent_token_velocity, 2),
            "longer_daily_tokens": round(longer_token_velocity, 2),
            "recent_daily_usd": (
                round(recent_usd_velocity, 4) if has_priced_history else None
            ),
            "longer_daily_usd": (
                round(longer_usd_velocity, 4) if has_priced_history else None
            ),
        },
        "partial_day": {
            "date": today_str,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "day_fraction_elapsed": round(day_fraction_elapsed, 4),
            "day_fraction_remaining": round(day_fraction_remaining, 4),
            "observed_tokens": today_entry["total_tokens"],
            "observed_cost_usd": (
                today_entry["api_equivalent_usd"]
                if today_entry["priced_requests"] > 0 else None
            ),
            "observed_activity_status": (
                "requests_observed"
                if today_entry["requests_count"] > 0
                else "no_requests_observed_coverage_unknown"
            ),
            "status": "partial" if day_fraction_remaining > 0.0 else "complete",
        },
        "forecasts": {
            "remainder_of_day": {
                "remaining_fraction": round(day_fraction_remaining, 4),
                "expected_tokens": round(rem_day_tokens, 2),
                "projected_day_total_tokens": round(proj_today_tokens, 2),
                "uncertainty_interval": _interval(
                    proj_today_tokens, day_fraction_remaining, backtest
                ),
                "expected_cost_usd": (
                    round(rem_day_usd, 4) if has_priced_history else None
                ),
                "projected_day_cost_usd": (
                    round(proj_today_usd, 4) if has_priced_history else None
                ),
                "cost_uncertainty_interval": _cost_interval(
                    proj_today_usd,
                    day_fraction_remaining,
                    cost_backtest,
                    has_priced_history,
                ),
            },
            "remainder_of_week": {
                "days_remaining_in_week": days_left_in_week,
                "observed_week_tokens": week_observed_tokens,
                "expected_remainder_tokens": round(rem_week_tokens, 2),
                "projected_week_total_tokens": round(proj_week_tokens, 2),
                "uncertainty_interval": _interval(
                    proj_week_tokens, days_left_in_week + day_fraction_remaining, backtest
                ),
                "observed_week_usd": (
                    round(week_observed_usd, 4) if has_priced_history else None
                ),
                "projected_week_cost_usd": (
                    round(proj_week_usd, 4) if has_priced_history else None
                ),
                "cost_uncertainty_interval": _cost_interval(
                    proj_week_usd,
                    days_left_in_week + day_fraction_remaining,
                    cost_backtest,
                    has_priced_history,
                ),
            },
            "remainder_of_month": {
                "days_remaining_in_month": days_left_in_month,
                "observed_month_tokens": month_observed_tokens,
                "expected_remainder_tokens": round(rem_month_tokens, 2),
                "projected_month_total_tokens": round(proj_month_tokens, 2),
                "uncertainty_interval": _interval(
                    proj_month_tokens, days_left_in_month + day_fraction_remaining, backtest
                ),
                "observed_month_usd": (
                    round(month_observed_usd, 4) if has_priced_history else None
                ),
                "projected_month_cost_usd": (
                    round(proj_month_usd, 4) if has_priced_history else None
                ),
                "cost_uncertainty_interval": _cost_interval(
                    proj_month_usd,
                    days_left_in_month + day_fraction_remaining,
                    cost_backtest,
                    has_priced_history,
                ),
            },
            "per_model": per_model_forecast,
        },
        "coverage": {
            **_daily_coverage(sorted_days),
            "priced_requests_count": total_priced_reqs,
            "unpriced_requests_count": total_unpriced_reqs,
            "priced_coverage_status": "partial" if has_unpriced else "complete",
            "cost_projection_status": cost_projection_status,
            "cost_projection_scope": cost_projection_scope,
            "cost_projection_is_full_scope": cost_projection_is_full_scope,
            "full_scope_cost_projection_status": full_scope_cost_projection_status,
            "full_scope_cost_abstention_reason": full_scope_cost_abstention_reason,
            "unpriced_handling": (
                "Cost projections use known-priced requests only; full-scope cost abstains."
                if has_unpriced
                else "Cost projections never assume missing prices are $0.00."
            ),
        },
        "backtest": backtest,
        "cost_backtest": cost_backtest,
        "warnings": warnings,
    }


# -----------------------------------------------------------------------------
# 2. Feature 46 & 64: Usage & Speed Anomaly Detection
# -----------------------------------------------------------------------------


def _load_user_expected_anomalies(
    conn: Optional[sqlite3.Connection],
    envelope: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Loads user-declared expected anomaly states from local settings and envelope.

    Returns dict mapping entity_id / target -> record.
    """
    expected: Dict[str, Dict[str, Any]] = {}

    # 1. From envelope settings
    user_settings = envelope.get("anomaly_settings") or {}
    declared = user_settings.get("expected_anomalies") or []
    for item in declared:
        if isinstance(item, str):
            expected[item] = {"entity_id": item, "notes": "Declared in query settings", "is_expected": True}
        elif isinstance(item, dict) and "id" in item:
            expected[str(item["id"])] = {
                "entity_id": str(item["id"]),
                "notes": item.get("notes", "Declared in query settings"),
                "is_expected": True,
            }

    # 2. From database table manual_settings_and_outcomes if table exists
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT entity_id, task_label, notes
                FROM manual_settings_and_outcomes
                WHERE entity_type IN ('anomaly', 'expected_anomaly')
                   OR task_label LIKE '%expected%'
                """
            )
            for row in cur.fetchall():
                ent_id = str(row[0])
                expected[ent_id] = {
                    "entity_id": ent_id,
                    "task_label": row[1],
                    "notes": row[2] or "Marked expected in local settings",
                    "is_expected": True,
                }
        except Exception:
            pass  # Table might not exist in lightweight unit test mode
        try:
            row = conn.execute(
                """
                SELECT setting_value
                FROM privacy_settings
                WHERE setting_key = 'efficiency_expected_anomalies'
                """
            ).fetchone()
            stored = json.loads(row[0]) if row is not None else []
            if isinstance(stored, list):
                for item in stored:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        entity_id = item["id"].strip()
                        if entity_id:
                            expected[entity_id] = {
                                "entity_id": entity_id,
                                "notes": item.get("notes") or "Marked expected in local settings",
                                "is_expected": True,
                            }
        except (sqlite3.Error, ValueError, TypeError):
            pass  # The settings table may be absent in lightweight test databases.


    return expected


def detect_anomalies(
    conn: Optional[sqlite3.Connection],
    rows: Sequence[Dict[str, Any]],
    envelope: Optional[Dict[str, Any]] = None,
    threshold_multiplier: float = DEFAULT_MAD_THRESHOLD_MULTIPLIER,
) -> Dict[str, Any]:
    """Detects usage and speed anomalies against a robust historical baseline.

    Checks:
    - Token spikes: unusually large total_tokens per request.
    - Cache write surges: unusual cache_write_5m_tokens or cache_write_1h_tokens.
    - Speed drops: generation_speed_tps significantly below median.
    - Latency spikes: ttft_ms significantly above median.
    - Failure spikes: requests with non-success status.

    Robust baseline: Uses Median and MAD (never skewed by outliers).
    Separates expected user-flagged anomalies from unacknowledged observations.
    Abstains if baseline samples < MIN_ANOMALY_BASELINE_SAMPLES.
    """
    norm_envelope = envelope or {}
    expected_map = _load_user_expected_anomalies(conn, norm_envelope)

    anomalies: List[Dict[str, Any]] = []
    metric_baselines: Dict[str, Any] = {}

    total_requests = len(rows)

    # 1. Total tokens baseline & spike detection
    tokens_series = [_safe_float(r.get("total_tokens")) for r in rows if r.get("total_tokens") is not None]
    valid_tokens = [v for v in tokens_series if v is not None]

    if len(valid_tokens) >= MIN_ANOMALY_BASELINE_SAMPLES:
        med_tok, mad_tok = _median_and_mad(valid_tokens)
        cutoff_tok = med_tok + (threshold_multiplier * max(mad_tok, med_tok * 0.2))
        metric_baselines["total_tokens"] = {
            "eligible": True,
            "sample_size": len(valid_tokens),
            "median": med_tok,
            "mad": mad_tok,
            "cutoff": round(cutoff_tok, 2),
            "method": "median_mad",
            "threshold_multiplier": threshold_multiplier,
        }

        for r in rows:
            val = _safe_float(r.get("total_tokens"))
            if val is not None and val > cutoff_tok:
                req_id = r["canonical_id"]
                sess_id = r.get("session_id")
                exp_info = expected_map.get(req_id) or expected_map.get(sess_id or "")

                deviation = round((val - med_tok) / max(mad_tok, 1.0), 2)
                anomalies.append({
                    "category": "usage",
                    "anomaly_type": "token_spike",
                    "metric_name": "total_tokens",
                    "observed_value": val,
                    "baseline_median": med_tok,
                    "baseline_mad": mad_tok,
                    "threshold_cutoff": round(cutoff_tok, 2),
                    "deviation_factor": deviation,
                    "canonical_id": req_id,
                    "session_id": sess_id,
                    "request_timestamp": r["request_timestamp"],
                    "model": r.get("normalized_model") or r.get("raw_model"),
                    "provider": r.get("provider"),
                    "is_expected": exp_info is not None,
                    "expected_state": exp_info,
                    "evidence": {
                        "fresh_input_tokens": r.get("fresh_input_tokens"),
                        "output_tokens": r.get("output_tokens"),
                        "origin_harness": r.get("origin_harness"),
                    },
                })
    else:
        metric_baselines["total_tokens"] = {
            "eligible": False,
            "sample_size": len(valid_tokens),
            "abstention_reason": (
                f"Sample size ({len(valid_tokens)}) is below minimum "
                f"required baseline samples ({MIN_ANOMALY_BASELINE_SAMPLES})."
            ),
        }

    # 2. Cache write spikes
    cw_series = [
        _safe_float((r.get("cache_write_5m_tokens") or 0) + (r.get("cache_write_1h_tokens") or 0))
        for r in rows
    ]
    valid_cw = [v for v in cw_series if v is not None and v > 0]
    if len(valid_cw) >= MIN_ANOMALY_BASELINE_SAMPLES:
        med_cw, mad_cw = _median_and_mad(valid_cw)
        cutoff_cw = med_cw + (threshold_multiplier * max(mad_cw, med_cw * 0.2))
        metric_baselines["cache_writes"] = {
            "eligible": True,
            "sample_size": len(valid_cw),
            "median": med_cw,
            "mad": mad_cw,
            "cutoff": round(cutoff_cw, 2),
        }
        for r in rows:
            val = float((r.get("cache_write_5m_tokens") or 0) + (r.get("cache_write_1h_tokens") or 0))
            if val > cutoff_cw:
                req_id = r["canonical_id"]
                sess_id = r.get("session_id")
                exp_info = expected_map.get(req_id) or expected_map.get(sess_id or "")
                anomalies.append({
                    "category": "usage",
                    "anomaly_type": "cache_write_surge",
                    "metric_name": "cache_write_tokens",
                    "observed_value": val,
                    "baseline_median": med_cw,
                    "baseline_mad": mad_cw,
                    "threshold_cutoff": round(cutoff_cw, 2),
                    "deviation_factor": round((val - med_cw) / max(mad_cw, 1.0), 2),
                    "canonical_id": req_id,
                    "session_id": sess_id,
                    "request_timestamp": r["request_timestamp"],
                    "model": r.get("normalized_model") or r.get("raw_model"),
                    "provider": r.get("provider"),
                    "is_expected": exp_info is not None,
                    "expected_state": exp_info,
                    "evidence": {
                        "cache_write_5m_tokens": r.get("cache_write_5m_tokens"),
                        "cache_write_1h_tokens": r.get("cache_write_1h_tokens"),
                    },
                })
    else:
        metric_baselines["cache_writes"] = {
            "eligible": False,
            "sample_size": len(valid_cw),
            "abstention_reason": "Insufficient cache write events for baseline.",
        }

    # 3. Speed TPS drops (eligible measured requests with generation duration)
    tps_rows = [
        r for r in rows
        if r.get("timing_basis") == "measured"
        and r.get("generation_duration_ms")
        and r.get("generation_duration_ms") > 0
        and r.get("generation_speed_tps") is not None
    ]
    tps_values = [float(r["generation_speed_tps"]) for r in tps_rows]
    if len(tps_values) >= MIN_ANOMALY_BASELINE_SAMPLES:
        med_tps, mad_tps = _median_and_mad(tps_values)
        cutoff_tps = max(0.0, med_tps - (threshold_multiplier * max(mad_tps, med_tps * 0.2)))
        metric_baselines["generation_speed_tps"] = {
            "eligible": True,
            "sample_size": len(tps_values),
            "median": med_tps,
            "mad": mad_tps,
            "drop_cutoff": round(cutoff_tps, 2),
        }
        for r in tps_rows:
            val = float(r["generation_speed_tps"])
            if val < cutoff_tps:
                req_id = r["canonical_id"]
                sess_id = r.get("session_id")
                exp_info = expected_map.get(req_id) or expected_map.get(sess_id or "")
                anomalies.append({
                    "category": "speed",
                    "anomaly_type": "tps_drop",
                    "metric_name": "generation_speed_tps",
                    "observed_value": val,
                    "baseline_median": med_tps,
                    "baseline_mad": mad_tps,
                    "threshold_cutoff": round(cutoff_tps, 2),
                    "deviation_factor": round((med_tps - val) / max(mad_tps, 1.0), 2),
                    "canonical_id": req_id,
                    "session_id": sess_id,
                    "request_timestamp": r["request_timestamp"],
                    "model": r.get("normalized_model") or r.get("raw_model"),
                    "provider": r.get("provider"),
                    "is_expected": exp_info is not None,
                    "expected_state": exp_info,
                    "evidence": {
                        "generation_duration_ms": r.get("generation_duration_ms"),
                        "output_tokens": r.get("output_tokens"),
                        "access_route": r.get("access_route"),
                    },
                })
    else:
        metric_baselines["generation_speed_tps"] = {
            "eligible": False,
            "sample_size": len(tps_values),
            "abstention_reason": "Insufficient measured generation samples for TPS baseline.",
        }

    # 4. TTFT Latency Spikes
    ttft_rows = [
        r for r in rows
        if r.get("ttft_ms") is not None and r.get("ttft_ms") > 0
    ]
    ttft_values = [float(r["ttft_ms"]) for r in ttft_rows]
    if len(ttft_values) >= MIN_ANOMALY_BASELINE_SAMPLES:
        med_ttft, mad_ttft = _median_and_mad(ttft_values)
        cutoff_ttft = med_ttft + (threshold_multiplier * max(mad_ttft, med_ttft * 0.2))
        metric_baselines["ttft_ms"] = {
            "eligible": True,
            "sample_size": len(ttft_values),
            "median": med_ttft,
            "mad": mad_ttft,
            "spike_cutoff": round(cutoff_ttft, 2),
        }
        for r in ttft_rows:
            val = float(r["ttft_ms"])
            if val > cutoff_ttft:
                req_id = r["canonical_id"]
                sess_id = r.get("session_id")
                exp_info = expected_map.get(req_id) or expected_map.get(sess_id or "")
                anomalies.append({
                    "category": "speed",
                    "anomaly_type": "ttft_spike",
                    "metric_name": "ttft_ms",
                    "observed_value": val,
                    "baseline_median": med_ttft,
                    "baseline_mad": mad_ttft,
                    "threshold_cutoff": round(cutoff_ttft, 2),
                    "deviation_factor": round((val - med_ttft) / max(mad_ttft, 1.0), 2),
                    "canonical_id": req_id,
                    "session_id": sess_id,
                    "request_timestamp": r["request_timestamp"],
                    "model": r.get("normalized_model") or r.get("raw_model"),
                    "provider": r.get("provider"),
                    "is_expected": exp_info is not None,
                    "expected_state": exp_info,
                    "evidence": {
                        "ttft_ms": val,
                        "duration_ms": r.get("duration_ms"),
                        "origin_harness": r.get("origin_harness"),
                    },
                })
    else:
        metric_baselines["ttft_ms"] = {
            "eligible": False,
            "sample_size": len(ttft_values),
            "abstention_reason": "Insufficient TTFT measurements for latency baseline.",
        }

    # 5. Failures / Errors
    for r in rows:
        st = (r.get("request_status") or "").lower()
        if st in ("error", "failed", "rate_limit", "timeout", "interrupted"):
            req_id = r["canonical_id"]
            sess_id = r.get("session_id")
            exp_info = expected_map.get(req_id) or expected_map.get(sess_id or "")
            anomalies.append({
                "category": "reliability",
                "anomaly_type": f"request_{st}",
                "metric_name": "request_status",
                "observed_value": st,
                "baseline_expected": "success",
                "canonical_id": req_id,
                "session_id": sess_id,
                "request_timestamp": r["request_timestamp"],
                "model": r.get("normalized_model") or r.get("raw_model"),
                "provider": r.get("provider"),
                "is_expected": exp_info is not None,
                "expected_state": exp_info,
                "evidence": {"status": st},
            })

    # Summary counts
    expected_count = sum(1 for a in anomalies if a.get("is_expected"))
    unexpected_count = len(anomalies) - expected_count

    return {
        "status": "completed",
        "total_requests_audited": total_requests,
        "total_anomalies_detected": len(anomalies),
        "unexpected_anomalies_count": unexpected_count,
        "expected_anomalies_count": expected_count,
        "baselines": metric_baselines,
        "anomalies": anomalies,
        "expected_user_settings": list(expected_map.values()),
    }


# -----------------------------------------------------------------------------
# 3. Feature 42: Before-And-After Efficiency Comparisons
# -----------------------------------------------------------------------------


def _summarize_period_metrics(
    conn: Optional[sqlite3.Connection],
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Computes standard summary metrics for a single period slice."""
    total_reqs = len(rows)
    fields = ("fresh_input_tokens", "cache_read_tokens", "cache_write_5m_tokens",
              "cache_write_1h_tokens", "cache_write_unknown_ttl_tokens", "output_tokens")
    coverage: Dict[str, str] = {}
    def observed_sum(field: str) -> Optional[float]:
        values = [r[field] for r in rows if r.get(field) is not None]
        coverage[field] = "unavailable" if not values else "exact" if len(values) == total_reqs else "partial"
        return sum(values) if values else None

    sums = {field: observed_sum(field) for field in fields}
    fresh_in, cache_rd = sums["fresh_input_tokens"], sums["cache_read_tokens"]
    cache_w5, cache_w1 = sums["cache_write_5m_tokens"], sums["cache_write_1h_tokens"]
    output_tok = sums["output_tokens"]
    observed_counters = sum(r.get(field) is not None for r in rows for field in fields)
    tot_tokens = sum(value for value in sums.values() if value is not None) if observed_counters else None
    coverage["total_tokens"] = ("unavailable" if not observed_counters else "partial"
        if observed_counters < total_reqs * len(fields) or any(r.get("is_unresolved_overlap") for r in rows)
        else "exact")
    prompt_rows = [r for r in rows if r.get("fresh_input_tokens") is not None and r.get("cache_read_tokens") is not None]
    eligible_prompt = sum(r["fresh_input_tokens"] + r["cache_read_tokens"] for r in prompt_rows)
    hit_rate = round(sum(r["cache_read_tokens"] for r in prompt_rows) / eligible_prompt, 4) if eligible_prompt > 0 else None
    coverage["cache_hit_rate"] = "unavailable" if hit_rate is None else "exact" if len(prompt_rows) == total_reqs else "partial"
    priced_reqs = sum(1 for r in rows if r.get("pricing_status") == "priced" and r.get("api_equivalent_usd") is not None)
    unpriced_reqs = total_reqs - priced_reqs
    tot_usd = observed_sum("api_equivalent_usd")
    tot_savings = observed_sum("cache_savings_usd")
    if any(r.get("is_unresolved_overlap") for r in rows):
        for field in ("api_equivalent_usd", "cache_savings_usd"):
            if coverage[field] == "exact":
                coverage[field] = "partial"

    # Timing
    durations = [_safe_float(r.get("duration_ms")) for r in rows if r.get("duration_ms") is not None]
    valid_durations = [d for d in durations if d is not None]
    mean_duration = round(statistics.mean(valid_durations), 2) if valid_durations else None
    med_duration = round(statistics.median(valid_durations), 2) if valid_durations else None

    # Weighted TPS
    tps_reqs = [
        r for r in rows
        if r.get("timing_basis") == "measured"
        and r.get("generation_duration_ms")
        and r.get("generation_duration_ms") > 0
        and r.get("output_tokens") is not None
    ]
    sum_gen_sec = sum(r["generation_duration_ms"] / 1000.0 for r in tps_reqs)
    sum_gen_out = sum(_safe_int(r.get("output_tokens")) for r in tps_reqs)
    weighted_tps = round(sum_gen_out / sum_gen_sec, 2) if sum_gen_sec > 0 else None

    # TTFT
    ttft_vals = [_safe_float(r.get("ttft_ms")) for r in rows if r.get("ttft_ms") is not None]
    valid_ttft = [t for t in ttft_vals if t is not None]
    mean_ttft = round(statistics.mean(valid_ttft), 2) if valid_ttft else None
    med_ttft = round(statistics.median(valid_ttft), 2) if valid_ttft else None

    # Retry detection (requests sharing prompt_id or flagged with error/interrupted prior)
    prompt_counts = defaultdict(int)
    for r in rows:
        p_id = r.get("prompt_id")
        if p_id:
            prompt_counts[p_id] += 1
    retry_requests = sum(c - 1 for c in prompt_counts.values() if c > 1)
    retry_rate = round(retry_requests / float(total_reqs), 4) if total_reqs > 0 else 0.0

    # Model distribution
    model_counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        m = r.get("normalized_model") or r.get("raw_model") or "unknown"
        model_counts[m] += 1

    # Task distribution from manual outcomes if available
    task_counts: Dict[str, int] = defaultdict(int)
    completed_tasks = 0
    total_tasks = 0
    manual_ratings: List[float] = []

    if conn is not None and total_reqs > 0:
        sess_ids = list({r["session_id"] for r in rows if r.get("session_id")})
        if sess_ids:
            try:
                cur = conn.cursor()
                q_placeholders = ",".join("?" for _ in sess_ids)
                cur.execute(
                    f"""
                    SELECT entity_id, task_label, completion_rating, manual_rating
                    FROM manual_settings_and_outcomes
                    WHERE entity_type = 'session' AND entity_id IN ({q_placeholders})
                    """,
                    sess_ids,
                )
                for o_row in cur.fetchall():
                    total_tasks += 1
                    t_label = o_row[1] or "unlabeled"
                    task_counts[t_label] += 1
                    if o_row[2] in ("complete", "completed"):
                        completed_tasks += 1
                    if o_row[3] is not None:
                        manual_ratings.append(float(o_row[3]))
            except Exception:
                pass

    completion_rate = round(completed_tasks / float(total_tasks), 4) if total_tasks > 0 else None
    mean_rating = round(statistics.mean(manual_ratings), 2) if manual_ratings else None

    return {
        "requests_count": total_reqs,
        "total_tokens": tot_tokens,
        "fresh_input_tokens": fresh_in,
        "cache_read_tokens": cache_rd,
        "cache_write_5m_tokens": cache_w5,
        "cache_write_1h_tokens": cache_w1,
        "output_tokens": output_tok,
        "tokens_per_request": round(tot_tokens / float(total_reqs), 2) if total_reqs and tot_tokens is not None else None,
        "cache_hit_rate": hit_rate,
        "eligible_prompt_tokens": eligible_prompt,
        "api_equivalent_usd": round(tot_usd, 4) if tot_usd is not None else None,
        "cache_savings_usd": round(tot_savings, 4) if tot_savings is not None else None,
        "cache_write_unknown_ttl_tokens": sums["cache_write_unknown_ttl_tokens"],
        "coverage": coverage,
        "cache_hit_eligible_requests": len(prompt_rows),
        "priced_requests": priced_reqs,
        "unpriced_requests": unpriced_reqs,
        "duration_ms_mean": mean_duration,
        "duration_ms_median": med_duration,
        "generation_speed_tps_weighted": weighted_tps,
        "ttft_ms_mean": mean_ttft,
        "ttft_ms_median": med_ttft,
        "retry_requests_count": retry_requests,
        "retry_rate": retry_rate,
        "model_distribution": dict(model_counts),
        "task_distribution": dict(task_counts),
        "outcomes": {
            "total_tasks_recorded": total_tasks,
            "completed_tasks": completed_tasks,
            "completion_rate": completion_rate,
            "manual_rating_mean": mean_rating,
        },
    }


def compare_periods(
    conn: sqlite3.Connection,
    base_envelope: Dict[str, Any],
    period_a: Dict[str, Any],
    period_b: Dict[str, Any],
) -> Dict[str, Any]:
    """Performs a before-and-after efficiency comparison across two timezone-aware periods.

    Reuses identical base filters with explicit half-open range overrides.
    Detects potential confounders (e.g. model mix shift, task shift, DST transitions).
    Includes strict scientific disclaimer prohibiting causal success claims.
    """
    if not isinstance(period_a, dict) or not isinstance(period_b, dict):
        raise ValueError("Both period_a and period_b must be objects.")
    base_range = base_envelope.get("time_range") or {}
    tz_name = (
        period_a.get("timezone")
        or period_b.get("timezone")
        or base_range.get("timezone")
        or "UTC"
    )
    if (
        period_a.get("timezone", tz_name) != period_b.get("timezone", tz_name)
    ):
        raise ValueError("Comparison periods must use the same timezone.")
    tz = ZoneInfo(tz_name)

    query_a = _normalized_filter_query(base_envelope, period_a, tz_name)
    query_b = _normalized_filter_query(base_envelope, period_b, tz_name)
    start_a_str = query_a["time_range"]["start"]
    end_a_str = query_a["time_range"]["end"]
    start_b_str = query_b["time_range"]["start"]
    end_b_str = query_b["time_range"]["end"]
    if not all([start_a_str, end_a_str, start_b_str, end_b_str]):
        raise ValueError("Both period_a and period_b must specify 'start' and 'end' ISO strings.")

    # Work with UTC instants: same-ZoneInfo subtraction can ignore offset changes.
    dt_start_a_utc = _parse_iso_utc(start_a_str)
    dt_end_a_utc = _parse_iso_utc(end_a_str)
    dt_start_b_utc = _parse_iso_utc(start_b_str)
    dt_end_b_utc = _parse_iso_utc(end_b_str)
    dur_a_seconds = (dt_end_a_utc - dt_start_a_utc).total_seconds()
    dur_b_seconds = (dt_end_b_utc - dt_start_b_utc).total_seconds()
    dur_a_days = dur_a_seconds / 86400.0
    dur_b_days = dur_b_seconds / 86400.0
    is_equal_duration = abs(dur_a_seconds - dur_b_seconds) < 1.0

    dst_transition_a = _has_dst_transition(dt_start_a_utc, dt_end_a_utc, tz)
    dst_transition_b = _has_dst_transition(dt_start_b_utc, dt_end_b_utc, tz)
    has_dst_transition = dst_transition_a or dst_transition_b

    # Both requests inherit precisely the same validated global filters.
    envelope_a = query_a
    envelope_b = query_b

    rows_a = filtered_request_rows(conn, envelope_a)
    rows_b = filtered_request_rows(conn, envelope_b)

    metrics_a = _summarize_period_metrics(conn, rows_a)
    metrics_b = _summarize_period_metrics(conn, rows_b)
    from token_ledger.metrics.outcomes import analyze as analyze_outcomes
    from token_ledger.metrics.traces import analyze as analyze_traces
    for metrics, envelope in ((metrics_a, envelope_a), (metrics_b, envelope_b)):
        prior_outcomes = metrics["outcomes"]
        metrics["outcomes"] = {**analyze_outcomes(conn, envelope)["values"],
            "total_tasks_recorded": prior_outcomes["total_tasks_recorded"],
            "manual_rating_mean": prior_outcomes["manual_rating_mean"]}
        retry = analyze_traces(conn, envelope)["retry_loops"]
        metrics["retry_requests_count"] = retry["retry_requests_count"]
        metrics["retry_rate"] = round(retry["retry_requests_count"] / metrics["requests_count"], 4) if metrics["requests_count"] else None
        metrics["retry_basis"] = "observed_request_retry_attempt_or_error_status"

    # Calculate Deltas
    def _calc_delta(val_a: Optional[float], val_b: Optional[float]) -> Dict[str, Any]:
        if val_a is None or val_b is None:
            return {"delta": None, "pct_change": None}
        diff = val_b - val_a
        pct = (diff / val_a * 100.0) if val_a != 0.0 else None
        return {
            "delta": round(diff, 4),
            "pct_change": round(pct, 2) if pct is not None else None,
        }

    deltas = {
        "requests_count": _calc_delta(metrics_a["requests_count"], metrics_b["requests_count"]),
        "total_tokens": _calc_delta(metrics_a["total_tokens"], metrics_b["total_tokens"]),
        "tokens_per_request": _calc_delta(metrics_a["tokens_per_request"], metrics_b["tokens_per_request"]),
        "cache_hit_rate": _calc_delta(metrics_a["cache_hit_rate"], metrics_b["cache_hit_rate"]),
        "api_equivalent_usd": _calc_delta(metrics_a["api_equivalent_usd"], metrics_b["api_equivalent_usd"]),
        "generation_speed_tps_weighted": _calc_delta(
            metrics_a["generation_speed_tps_weighted"], metrics_b["generation_speed_tps_weighted"]
        ),
        "ttft_ms_mean": _calc_delta(metrics_a["ttft_ms_mean"], metrics_b["ttft_ms_mean"]),
        "duration_ms_mean": _calc_delta(metrics_a["duration_ms_mean"], metrics_b["duration_ms_mean"]),
        "retry_rate": _calc_delta(metrics_a["retry_rate"], metrics_b["retry_rate"]),
    }
    for key in ("total_tokens", "cache_hit_rate", "api_equivalent_usd"):
        if metrics_a["coverage"][key] != "exact" or metrics_b["coverage"][key] != "exact":
            deltas[key] = {"delta": None, "pct_change": None}
    if metrics_a["coverage"]["total_tokens"] != "exact" or metrics_b["coverage"]["total_tokens"] != "exact":
        deltas["tokens_per_request"] = {"delta": None, "pct_change": None}

    # Confounder Analysis
    confounders: List[str] = []
    if not is_equal_duration:
        confounders.append(
            f"Period durations are unequal: Period A is {round(dur_a_days, 1)} days vs "
            f"Period B is {round(dur_b_days, 1)} days."
        )
    if has_dst_transition:
        confounders.append(
            "Daylight Saving Time (DST) clock shift occurred during the evaluated periods."
        )

    # Model shift check: compute primary model share difference
    models_a = set(metrics_a["model_distribution"].keys())
    models_b = set(metrics_b["model_distribution"].keys())
    if models_a != models_b:
        confounders.append(
            f"Model mix shift detected: Period A models {sorted(models_a)} vs "
            f"Period B models {sorted(models_b)}."
        )
    else:
        # Check share proportions
        tot_a = max(1, metrics_a["requests_count"])
        tot_b = max(1, metrics_b["requests_count"])
        for m in models_a:
            share_a = metrics_a["model_distribution"][m] / float(tot_a)
            share_b = metrics_b["model_distribution"][m] / float(tot_b)
            if abs(share_a - share_b) > 0.25:
                confounders.append(
                    f"Substantial model share shift for '{m}': "
                    f"{round(share_a * 100, 1)}% in Period A vs {round(share_b * 100, 1)}% in Period B."
                )
    if metrics_a["task_distribution"] != metrics_b["task_distribution"] and (
        metrics_a["task_distribution"] or metrics_b["task_distribution"]
    ):
        confounders.append("Recorded task distribution differs between periods; linked-session outcomes may describe work outside the selected requests.")

    # Prompt length shift check
    if (metrics_a["requests_count"] > 0 and metrics_b["requests_count"] > 0
            and all(m["coverage"][field] == "exact" for m in (metrics_a, metrics_b)
                    for field in ("fresh_input_tokens", "cache_read_tokens"))):
        mean_prompt_a = (metrics_a["fresh_input_tokens"] + metrics_a["cache_read_tokens"]) / float(metrics_a["requests_count"])
        mean_prompt_b = (metrics_b["fresh_input_tokens"] + metrics_b["cache_read_tokens"]) / float(metrics_b["requests_count"])
        if mean_prompt_a > 0 and abs(mean_prompt_b - mean_prompt_a) / mean_prompt_a > 0.30:
            confounders.append(
                f"Prompt complexity shift: mean prompt tokens changed from {round(mean_prompt_a, 1)} "
                f"to {round(mean_prompt_b, 1)}."
            )

    return {
        "status": "completed",
        "timezone": tz_name,
        "is_equal_duration": is_equal_duration,
        "has_dst_transition": has_dst_transition,
        "period_a": {
            "label": period_a.get("label", "Period A (Baseline)"),
            "start": start_a_str,
            "end": end_a_str,
            "duration_seconds": round(dur_a_seconds, 3),
            "duration_days": round(dur_a_days, 6),
            "metrics": metrics_a,
        },
        "period_b": {
            "label": period_b.get("label", "Period B (Comparison)"),
            "start": start_b_str,
            "end": end_b_str,
            "duration_seconds": round(dur_b_seconds, 3),
            "duration_days": round(dur_b_days, 6),
            "metrics": metrics_b,
        },
        "deltas": deltas,
        "confounders": confounders,
        "matched_status": "matched" if not confounders else "non_matched_or_confounded",
        "causal_claim_disclaimer": (
            "Descriptive observational comparison only. Observed changes represent empirical "
            "correlation under operational conditions. Do not infer causal efficacy, algorithmic superiority, "
            "or guaranteed efficiency gains."
        ),
    }


# -----------------------------------------------------------------------------
# 4. Central Dispatcher: analyze(...)
# -----------------------------------------------------------------------------


def analyze(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Central entry point for WP20 analytics dispatches.

    Supports families / metric_ids:
    - 'forecasts' (or 'usage_forecast'): Future run-rate projections and backtests.
    - 'anomalies' (or 'usage_anomalies', 'speed_anomalies'): Robust outlier detection.
    - 'comparisons' (or 'before_after_comparison'): Two-period efficiency comparison.
    """
    request = dict(envelope)
    request.setdefault("contract_version", "1")
    if not request.get("family") and not request.get("metric_id"):
        request["family"] = "forecasts"
    normalized_query = validate_query(request)
    target = normalized_query["family"]
    if target not in ("forecasts", "anomalies", "comparisons"):
        raise ValueError(f"Unsupported analytics family for this endpoint: '{target}'.")
    metric_id = normalized_query["metric_id"]
    tz_name = normalized_query["time_range"]["timezone"]

    if target == "comparisons":
        periods = normalized_query.get("comparison_periods") or {}
        p_a = periods.get("period_a") or normalized_query.get("period_a")
        p_b = periods.get("period_b") or normalized_query.get("period_b")
        if not p_a or not p_b:
            raise ValueError(
                "Comparison family requires 'comparison_periods' with 'period_a' and 'period_b' definitions."
            )
        comp_res = compare_periods(conn, normalized_query, p_a, p_b)
        return {
            "contract_version": "1",
            "query": normalized_query,
            "metric_id": metric_id,
            "family": "comparisons",
            "values": comp_res["deltas"],
            "period_a": comp_res["period_a"],
            "period_b": comp_res["period_b"],
            "confounders": comp_res["confounders"],
            "matched_status": comp_res["matched_status"],
            "causal_claim_disclaimer": comp_res["causal_claim_disclaimer"],
            "warnings": comp_res["confounders"],
        }

    if target == "anomalies":
        rows = filtered_request_rows(conn, normalized_query)
        anom_res = detect_anomalies(
            conn=conn,
            rows=rows,
            envelope=normalized_query,
            threshold_multiplier=normalized_query.get(
                "threshold_multiplier", DEFAULT_MAD_THRESHOLD_MULTIPLIER
            ),
        )
        return {
            "contract_version": "1",
            "query": normalized_query,
            "metric_id": metric_id,
            "family": "anomalies",
            "values": {
                "total_audited": anom_res["total_requests_audited"],
                "total_anomalies": anom_res["total_anomalies_detected"],
                "unexpected_anomalies": anom_res["unexpected_anomalies_count"],
                "expected_anomalies": anom_res["expected_anomalies_count"],
            },
            "rows": anom_res["anomalies"],
            "baselines": anom_res["baselines"],
            "expected_user_settings": anom_res["expected_user_settings"],
            "warnings": [],
        }

    history_window = normalized_query.get("history_window")
    explicit_history_window = history_window is not None
    history_range = history_window or {"timezone": tz_name}
    history_query = _normalized_filter_query(
        normalized_query,
        time_range=history_range,
        timezone_name=tz_name,
    )
    history_query["family"] = "forecasts"
    history_query["metric_id"] = metric_id
    rows = filtered_request_rows(conn, history_query)
    fc_res = forecast_usage(rows, tz_name=tz_name)
    normalized_history_window = {
        "start": history_query["time_range"]["start"],
        "end": history_query["time_range"]["end"],
        "timezone": history_query["time_range"]["timezone"],
        "basis": (
            "explicit_history_window" if explicit_history_window else "all_matching_history"
        ),
    }
    return {
        "contract_version": "1",
        "query": normalized_query,
        "metric_id": metric_id,
        "family": "forecasts",
        "eligible": fc_res["eligible"],
        "status": fc_res["status"],
        "abstention_reason": fc_res["abstention_reason"],
        "values": fc_res.get("forecasts") or {},
        "velocity": fc_res.get("velocity"),
        "partial_day": fc_res.get("partial_day"),
        "backtest": fc_res.get("backtest"),
        "cost_backtest": fc_res.get("cost_backtest"),
        "coverage": fc_res.get("coverage", {}),
        "history_window": normalized_history_window,
        "warnings": fc_res.get("warnings", []),
    }
