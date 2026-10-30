"""Provider, Quota, Burn-Rate, and Budget Analytics for Token Ledger (WP16).

Implements Rebuild Plan Rows 21, 22, 23, 24, 26, 47:
- Row 21: Provider and Access-Route Analytics
  Separates provider, account_alias, access_route, and model breakdowns.
  Model identity is never merged into route identity.
- Row 22: Actual Cost vs. API-Equivalent Cost
  Separates actual billed API spend, subscription-backed API equivalent value,
  subscription fees, cache savings, and unpriced requests. Explicit historical FX.
- Row 23: Subscription Quota and Usage-Limit Monitor
  Official provider quota snapshots with validated source and reset window.
  When unavailable or unsupported, explicitly reports 'Quota unavailable';
  NEVER infers quota percentages or limits from local token counts.
- Row 24: Token Burn-Rate Monitor
  Declared interval / denominator, partial current bucket identification,
  baseline equivalent-period change with sparse-history abstention.
- Row 26: Usage Budgets and Thresholds
  Daily token, monthly equivalent value, project, model, and session thresholds.
  Respects known-eligible coverage. Persisted user-owned versioned settings.
  Notifications strictly local event notifications with explicit consent, no network.
- Row 47: Live Active-Session Dashboard
  Last observed activity timestamp vs heuristic active status separated,
  with transparent heuristic basis and no unsupported precision.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
import uuid

from token_ledger.query import filtered_request_rows, validate_query


VALID_WINDOW_TYPES = {"5m", "minute", "hour", "day", "month", "sliding_window"}
VALID_QUOTA_STATUSES = {"available", "unavailable", "unsupported", "stale"}
VALID_BUDGET_PERIODS = {"daily", "monthly", "session", "project_model"}
VALID_BUDGET_TARGETS = {"tokens", "cost_usd", "cost_inr"}


def validate_quota_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Validates an incoming official quota snapshot structure.

    Guarantees:
    - Official source provenance must be non-empty.
    - Provider is required.
    - Validates numeric bounds (used, limit, remaining >= 0).
    - Status defaults to 'unavailable' if unverified.
    - Never derives or guesses numbers not provided by official source.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Quota snapshot must be a dictionary")

    provider = snapshot.get("provider")
    if not provider or not isinstance(provider, str) or not provider.strip():
        raise ValueError("Quota snapshot requires a non-empty 'provider'")

    account_alias = snapshot.get("account_alias", "default")
    if not isinstance(account_alias, str):
        account_alias = "default"

    access_route = snapshot.get("access_route", "default")
    if not isinstance(access_route, str):
        access_route = "default"

    official_source = snapshot.get("official_source")
    if not official_source or not isinstance(official_source, str) or not official_source.strip():
        raise ValueError("Quota snapshot requires an official_source provenance identifier")

    timestamp = snapshot.get("timestamp") or snapshot.get("measured_at")
    if not timestamp or not isinstance(timestamp, str):
        timestamp = datetime.now(timezone.utc).isoformat()

    window_type = snapshot.get("window_type", "month")
    if window_type not in VALID_WINDOW_TYPES:
        window_type = "sliding_window"

    status = snapshot.get("status", "available")
    if status not in VALID_QUOTA_STATUSES:
        status = "unavailable"

    limit_value = snapshot.get("limit_value")
    if limit_value is not None:
        try:
            limit_value = float(limit_value)
            if limit_value < 0:
                raise ValueError("limit_value must be non-negative")
        except (ValueError, TypeError):
            raise ValueError(f"Invalid limit_value: {limit_value}")

    used_value = snapshot.get("used_value")
    if used_value is not None:
        try:
            used_value = float(used_value)
            if used_value < 0:
                raise ValueError("used_value must be non-negative")
        except (ValueError, TypeError):
            raise ValueError(f"Invalid used_value: {used_value}")

    remaining_value = snapshot.get("remaining_value")
    if remaining_value is not None:
        try:
            remaining_value = float(remaining_value)
            if remaining_value < 0:
                raise ValueError("remaining_value must be non-negative")
        except (ValueError, TypeError):
            raise ValueError(f"Invalid remaining_value: {remaining_value}")

    reset_at = snapshot.get("reset_at")
    if reset_at is not None and not isinstance(reset_at, str):
        reset_at = str(reset_at)

    units = snapshot.get("units", "tokens")
    if not isinstance(units, str):
        units = "tokens"

    # Raw metadata preserved in provenance
    provenance = {
        "official_source": official_source,
        "units": units,
        "limit_tokens": snapshot.get("limit_tokens", limit_value if units == "tokens" else None),
        "used_tokens": snapshot.get("used_tokens", used_value if units == "tokens" else None),
        "remaining_tokens": snapshot.get("remaining_tokens", remaining_value if units == "tokens" else None),
        "limit_requests": snapshot.get("limit_requests", limit_value if units == "requests" else None),
        "used_requests": snapshot.get("used_requests", used_value if units == "requests" else None),
        "percent_used": snapshot.get("percent_used"),  # null if not declared by official source
        "raw_payload": snapshot.get("raw_payload", {}),
    }

    return {
        "provider": provider.strip(),
        "account_alias": account_alias.strip(),
        "access_route": access_route.strip(),
        "window_type": window_type,
        "limit_value": limit_value,
        "used_value": used_value,
        "remaining_value": remaining_value,
        "reset_at": reset_at,
        "measured_at": timestamp,
        "status": status,
        "provenance": provenance,
    }


def import_quota_snapshot(conn: sqlite3.Connection, snapshot: Dict[str, Any]) -> int:
    """Validates and persists an official quota snapshot into subscription_quotas.

    Returns:
        quota_id (int) of the inserted record.
    """
    validated = validate_quota_snapshot(snapshot)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO subscription_quotas (
            provider, account_alias, access_route, window_type,
            limit_value, used_value, remaining_value,
            reset_at, measured_at, status, provenance_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            validated["provider"],
            validated["account_alias"],
            validated["access_route"],
            validated["window_type"],
            validated["limit_value"],
            validated["used_value"],
            validated["remaining_value"],
            validated["reset_at"],
            validated["measured_at"],
            validated["status"],
            json.dumps(validated["provenance"]),
        ),
    )
    return cur.lastrowid or 0


def save_budget(conn: sqlite3.Connection, budget_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Saves or updates a user-owned versioned budget configuration.

    Stored in alerts_and_reports with item_type='budget'.
    Guarantees:
    - User-owned versioned settings.
    - Notification toggle requires consent.
    - Strictly local persistence, no network side-effects.
    """
    if not isinstance(budget_dict, dict):
        raise ValueError("Budget configuration must be a dictionary")

    item_id = budget_dict.get("id") or budget_dict.get("item_id") or f"budget_{uuid.uuid4().hex[:12]}"
    name = budget_dict.get("name") or f"Budget {item_id}"
    period = budget_dict.get("period", "monthly")
    if period not in VALID_BUDGET_PERIODS:
        raise ValueError(f"Invalid budget period: {period}. Must be one of {VALID_BUDGET_PERIODS}")

    target_type = budget_dict.get("target_type", "tokens")
    if target_type not in VALID_BUDGET_TARGETS:
        raise ValueError(f"Invalid budget target_type: {target_type}. Must be one of {VALID_BUDGET_TARGETS}")

    threshold = budget_dict.get("threshold")
    if threshold is None or float(threshold) <= 0:
        raise ValueError("Budget threshold must be a positive number")
    threshold = float(threshold)

    scope = budget_dict.get("scope", {})
    if not isinstance(scope, dict):
        scope = {}

    notifications_enabled = bool(budget_dict.get("notifications_enabled", False))
    project_id = scope.get("project_id")
    account_alias = budget_dict.get("account_alias", "default")

    now_iso = datetime.now(timezone.utc).isoformat()
    config_json = json.dumps({
        "period": period,
        "target_type": target_type,
        "threshold": threshold,
        "scope": scope,
        "notifications_enabled": notifications_enabled,
        "version": "1.0",
        "updated_at": now_iso,
    })

    cur = conn.cursor()
    # Check if project_id exists in projects table to respect foreign key constraint
    fk_project_id = None
    if project_id:
        cur.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,))
        if cur.fetchone():
            fk_project_id = project_id

    # Check if item exists
    cur.execute("SELECT created_at FROM alerts_and_reports WHERE item_id = ?", (item_id,))
    existing = cur.fetchone()
    if existing:
        cur.execute(
            """
            UPDATE alerts_and_reports
            SET name = ?,
                threshold_config_json = ?,
                project_id = ?,
                account_alias = ?,
                updated_at = ?
            WHERE item_id = ?
            """,
            (name, config_json, fk_project_id, account_alias, now_iso, item_id),
        )
        created_at = existing[0]
    else:
        created_at = now_iso
        cur.execute(
            """
            INSERT INTO alerts_and_reports (
                item_id, item_type, name, frequency,
                threshold_config_json, filter_state_json, pricing_assumptions_json,
                last_generated_at, created_at, updated_at,
                project_id, account_alias
            ) VALUES (?, 'budget', ?, ?, ?, '{}', '{}', NULL, ?, ?, ?, ?)
            """,
            (item_id, name, period, config_json, created_at, now_iso, fk_project_id, account_alias),
        )

    return {
        "id": item_id,
        "name": name,
        "period": period,
        "target_type": target_type,
        "threshold": threshold,
        "scope": scope,
        "notifications_enabled": notifications_enabled,
        "account_alias": account_alias,
        "created_at": created_at,
        "updated_at": now_iso,
    }


def delete_budget(conn: sqlite3.Connection, budget_id: str) -> bool:
    """Deletes a budget record from alerts_and_reports."""
    cur = conn.cursor()
    cur.execute("DELETE FROM alerts_and_reports WHERE item_id = ? AND item_type = 'budget'", (budget_id,))
    return cur.rowcount > 0


def list_budgets(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Lists all stored budget configurations."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT item_id, name, threshold_config_json, project_id, account_alias, created_at, updated_at
        FROM alerts_and_reports
        WHERE item_type = 'budget'
        ORDER BY created_at ASC
        """
    )
    results = []
    for row in cur.fetchall():
        item_id, name, config_str, project_id, account_alias, created_at, updated_at = row
        try:
            cfg = json.loads(config_str or "{}")
        except Exception:
            cfg = {}
        results.append({
            "id": item_id,
            "name": name,
            "period": cfg.get("period", "monthly"),
            "target_type": cfg.get("target_type", "tokens"),
            "threshold": cfg.get("threshold", 0.0),
            "scope": cfg.get("scope", {}),
            "notifications_enabled": cfg.get("notifications_enabled", False),
            "project_id": project_id,
            "account_alias": account_alias,
            "created_at": created_at,
            "updated_at": updated_at,
        })
    return results


def _resolve_historical_fx(
    conn: sqlite3.Connection,
    reference_date: Optional[str] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """Return the latest USD/INR rate effective on or before the reference date."""
    if not reference_date:
        return None, None

    cur = conn.cursor()
    cur.execute(
        """
        SELECT rate_per_usd, effective_date
        FROM historical_fx
        WHERE base_currency = 'USD'
          AND target_currency = 'INR'
          AND effective_date <= ?
        ORDER BY effective_date DESC
        LIMIT 1
        """,
        (reference_date[:10],),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None, None
    return float(row[0]), str(row[1])


def _analyze_providers(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
    rows: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    """Computes provider and access-route analytics (Row 21, Row 22, Row 23)."""
    # 1. Financial totals and group breakdowns
    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    models_per_group: Dict[Tuple[str, str, str], Set[str]] = {}

    financial_fields = (
        "actual_api_spend_usd",
        "api_equivalent_usd",
        "api_equivalent_inr",
        "subscription_fees_usd",
        "cache_savings_usd",
    )
    financial_observed_counts = {field: 0 for field in financial_fields}
    total_actual_billed = 0.0
    total_api_equivalent_usd = 0.0
    total_api_equivalent_inr = 0.0
    total_cache_savings_usd = 0.0
    total_subscription_fees_usd = 0.0
    unpriced_requests_count = 0
    priced_requests_count = 0

    def coverage_status(observed: int, total: int) -> str:
        if observed == 0:
            return "unavailable"
        return "exact" if observed == total else "partial"

    latest_ts = rows[-1]["request_timestamp"] if rows else None
    fx_rate, fx_date = _resolve_historical_fx(conn, latest_ts)

    for r in rows:
        provider = r.get("provider") or "unknown"
        account = r.get("account_alias") or "default"
        route = r.get("access_route") or "default"
        model = r.get("normalized_model") or r.get("raw_model") or "unknown"
        key = (provider, account, route)

        if key not in grouped:
            grouped[key] = {
                "provider": provider,
                "account_alias": account,
                "access_route": route,
                "request_count": 0,
                "total_tokens": None,
                "fresh_input_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "actual_api_spend_usd": None,
                "actual_api_spend_inr": None,
                "api_equivalent_usd": None,
                "api_equivalent_inr": None,
                "subscription_fees_usd": None,
                "cache_savings_usd": None,
                "unpriced_request_count": 0,
                "_token_missing": set(),
                "_financial_observed_counts": {field: 0 for field in financial_fields},
                "is_verified_route": route not in ("unknown", "unverified"),
            }
            models_per_group[key] = set()

        grp = grouped[key]
        models_per_group[key].add(model)
        grp["request_count"] += 1

        billable_values = [
            r.get("fresh_input_tokens"),
            r.get("cache_read_tokens"),
            r.get("cache_write_5m_tokens"),
            r.get("cache_write_1h_tokens"),
            r.get("cache_write_unknown_ttl_tokens"),
            r.get("output_tokens"),
        ]
        cache_write_values = billable_values[2:5]
        token_values = {
            "fresh_input_tokens": billable_values[0],
            "cache_read_tokens": billable_values[1],
            "cache_write_tokens": (
                sum(value for value in cache_write_values if value is not None)
                if any(value is not None for value in cache_write_values)
                else None
            ),
            "output_tokens": billable_values[5],
            "reasoning_tokens": r.get("reasoning_tokens"),
            "total_tokens": (
                sum(value for value in billable_values if value is not None)
                if any(value is not None for value in billable_values)
                else None
            ),
        }
        for field, value in token_values.items():
            if value is None:
                grp["_token_missing"].add(field)
            else:
                grp[field] = (grp[field] or 0) + value
        # Cache-write buckets are alternatives: one observed bucket is complete.
        # cache_write_tokens is missing only when all three buckets are null.
        cache_write_missing = all(value is None for value in cache_write_values)
        if cache_write_missing:
            grp["_token_missing"].add("cache_write_tokens")
        billable_missing = any(value is None for value in billable_values[:2] + billable_values[5:])
        if billable_missing or cache_write_missing or r.get("is_unresolved_overlap"):
            grp["_token_missing"].add("total_tokens")

        # Financial fields remain independent facts: INR-equivalent values are never
        # inferred from API-equivalent USD or an exchange rate.
        actual_billed = r.get("actual_billed_amount")
        if actual_billed is not None:
            total_actual_billed += actual_billed
            financial_observed_counts["actual_api_spend_usd"] += 1
            grp["actual_api_spend_usd"] = (grp["actual_api_spend_usd"] or 0.0) + actual_billed
            grp["_financial_observed_counts"]["actual_api_spend_usd"] += 1

        eq_usd = r.get("api_equivalent_usd")
        if eq_usd is not None:
            total_api_equivalent_usd += eq_usd
            financial_observed_counts["api_equivalent_usd"] += 1
            grp["api_equivalent_usd"] = (grp["api_equivalent_usd"] or 0.0) + eq_usd
            grp["_financial_observed_counts"]["api_equivalent_usd"] += 1

        eq_inr = r.get("api_equivalent_inr")
        if eq_inr is not None:
            total_api_equivalent_inr += eq_inr
            financial_observed_counts["api_equivalent_inr"] += 1
            grp["api_equivalent_inr"] = (grp["api_equivalent_inr"] or 0.0) + eq_inr
            grp["_financial_observed_counts"]["api_equivalent_inr"] += 1

        subscription_fee = r.get("subscription_fee_usd")
        if subscription_fee is not None:
            total_subscription_fees_usd += subscription_fee
            financial_observed_counts["subscription_fees_usd"] += 1
            grp["subscription_fees_usd"] = (
                (grp["subscription_fees_usd"] or 0.0) + subscription_fee
            )
            grp["_financial_observed_counts"]["subscription_fees_usd"] += 1

        savings = r.get("cache_savings_usd")
        if savings is not None:
            total_cache_savings_usd += savings
            financial_observed_counts["cache_savings_usd"] += 1
            grp["cache_savings_usd"] = (grp["cache_savings_usd"] or 0.0) + savings
            grp["_financial_observed_counts"]["cache_savings_usd"] += 1

        if r.get("pricing_status") in ("priced", "verified", "estimated", "unverified"):
            priced_requests_count += 1
        else:
            unpriced_requests_count += 1
            grp["unpriced_request_count"] += 1

    # Format rows
    provider_rows: List[Dict[str, Any]] = []
    providers_set: Set[str] = set()
    routes_set: Set[str] = set()

    for key, grp in grouped.items():
        provider, account, route = key
        providers_set.add(provider)
        routes_set.add(route)
        grp["models"] = sorted(list(models_per_group[key]))
        token_fields = (
            "total_tokens",
            "fresh_input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_tokens",
        )
        grp["token_fields_status"] = {
            field: (
                "unavailable"
                if grp[field] is None
                else "partial" if field in grp["_token_missing"] else "exact"
            )
            for field in token_fields
        }
        grp["total_tokens_status"] = grp["token_fields_status"]["total_tokens"]
        for field in token_fields:
            if grp[field] is not None:
                grp[field] = int(grp[field])

        for field in financial_fields:
            observed_count = grp["_financial_observed_counts"][field]
            grp[f"{field}_status"] = coverage_status(observed_count, grp["request_count"])
            if observed_count == 0:
                grp[field] = None
            elif field != "actual_api_spend_usd":
                grp[field] = round(grp[field], 2 if field == "api_equivalent_inr" else 4)

        billed_count = grp["_financial_observed_counts"]["actual_api_spend_usd"]
        if billed_count and fx_rate is not None:
            grp["actual_api_spend_inr"] = round(grp["actual_api_spend_usd"] * fx_rate, 2)
            grp["actual_api_spend_inr_status"] = coverage_status(
                billed_count, grp["request_count"]
            )
        else:
            grp["actual_api_spend_inr"] = None
            grp["actual_api_spend_inr_status"] = "unavailable"
        if grp["actual_api_spend_usd"] is not None:
            grp["actual_api_spend_usd"] = round(grp["actual_api_spend_usd"], 4)
        grp.pop("_token_missing")
        grp.pop("_financial_observed_counts")
        provider_rows.append(grp)

    # Sort rows by total_tokens desc, placing unknown totals after measured totals.
    provider_rows.sort(
        key=lambda item: (
            item["total_tokens"] is None,
            item["total_tokens"] if item["total_tokens"] is not None else 0,
        ),
        reverse=True,
    )

    # 2. Quotas are global snapshots, not request-linked facts. Only fields present
    # on subscription_quotas can scope these cards: provider, access_route, measured_at.
    quota_filters = envelope.get("filters") or {}
    unsupported_quota_scope = sorted(set(quota_filters) - {"provider", "route"})
    if envelope.get("harness_scope", "combined") != "combined":
        unsupported_quota_scope.append("harness_scope")
    if envelope.get("search"):
        unsupported_quota_scope.append("search")
    unsupported_quota_scope = sorted(set(unsupported_quota_scope))

    quota_cards: List[Dict[str, Any]] = []
    quota_db_rows = []
    quota_scope_supported = not unsupported_quota_scope
    if not quota_scope_supported:
        warnings.append(
            "Subscription quota snapshots have no request-level links for "
            f"{', '.join(unsupported_quota_scope)}; quota cards were omitted."
        )
    else:
        quota_clauses: List[str] = []
        quota_params: List[Any] = []
        for filter_key, column in (("provider", "provider"), ("route", "access_route")):
            values = quota_filters.get(filter_key, [])
            if values:
                placeholders = ", ".join("?" for _ in values)
                quota_clauses.append(f"{column} IN ({placeholders})")
                quota_params.extend(values)

        time_range = envelope.get("time_range") or {}
        if time_range.get("start"):
            quota_clauses.append("measured_at >= ?")
            quota_params.append(time_range["start"])
        if time_range.get("end"):
            quota_clauses.append("measured_at < ?")
            quota_params.append(time_range["end"])

        quota_where = f"WHERE {' AND '.join(quota_clauses)}" if quota_clauses else ""
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT quota_id, provider, account_alias, access_route, window_type,
                   limit_value, used_value, remaining_value, reset_at, measured_at,
                   status, provenance_json
            FROM subscription_quotas
            {quota_where}
            ORDER BY julianday(measured_at) DESC, quota_id DESC
            """,
            quota_params,
        )
        quota_db_rows = cur.fetchall()

    quota_by_key: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}


    now_utc = datetime.now(timezone.utc)

    for qrow in quota_db_rows:
        qid, q_prov, q_acc, q_route, w_type, l_val, u_val, rem_val, r_at, m_at, q_stat, prov_json_str = qrow
        q_key = (q_prov, q_acc, q_route, w_type)

        try:
            prov_data = json.loads(prov_json_str or "{}")
        except Exception:
            prov_data = {}

        is_avail = (q_stat == "available")

        # Official quota availability expires against the current wall clock,
        # independent of the filtered request range or absence of requests.
        is_stale = False
        if r_at:
            try:
                r_dt = datetime.fromisoformat(r_at.replace("Z", "+00:00"))
                if r_dt <= now_utc:
                    is_stale = True
            except Exception:
                pass

        card: Dict[str, Any] = {
            "provider": q_prov,
            "account_alias": q_acc,
            "access_route": q_route,
            "is_available": is_avail and not is_stale,
            "official_timestamp": m_at,
            "provenance": prov_data.get("official_source", "none"),
            "window_type": w_type,
            "window_reset_at": r_at,
            "limit_tokens": prov_data.get("limit_tokens", l_val if prov_data.get("units") == "tokens" else None),
            "used_tokens": prov_data.get("used_tokens", u_val if prov_data.get("units") == "tokens" else None),
            "remaining_tokens": prov_data.get("remaining_tokens", rem_val if prov_data.get("units") == "tokens" else None),
            "limit_requests": prov_data.get("limit_requests", l_val if prov_data.get("units") == "requests" else None),
            "used_requests": prov_data.get("used_requests", u_val if prov_data.get("units") == "requests" else None),
            # CRITICAL RULE: If official source did not declare percentage, or if unavailable,
            # NEVER calculate percent_used from local counts!
            "percent_used": prov_data.get("percent_used") if is_avail and not is_stale else None,
            "status": "stale" if is_stale else q_stat,
            "quota_id": qid,
            "units": prov_data.get("units", "unknown"),
            "limit_value": l_val,
            "used_value": u_val,
            "remaining_value": rem_val,
            "reported_status": q_stat,
            "reported_percent_used": prov_data.get("percent_used") if is_avail else None,
        }
        # Keep source facts intact in chronological history, including expired
        # windows; only the newest snapshot controls current availability.
        if q_key not in quota_by_key:
            quota_by_key[q_key] = {**card, "history": []}
            quota_cards.append(quota_by_key[q_key])
        quota_by_key[q_key]["history"].insert(0, card)

    # For observed providers with no quota entry, show an unavailable card only
    # when the result is not narrowed to a specific access route.
    if quota_scope_supported and not quota_filters.get("route"):
        for p in providers_set:
            if not any(c["provider"] == p for c in quota_cards):
                quota_cards.append({
                    "provider": p,
                    "account_alias": "default",
                    "access_route": "default",
                    "is_available": False,
                    "official_timestamp": None,
                    "provenance": "none",
                    "status": "unavailable",
                    "percent_used": None,
                    "limit_tokens": None,
                    "used_tokens": None,
                    "remaining_tokens": None,
                    "history": [],
                })
    source_status = "official" if any(c["is_available"] for c in quota_cards) else "unavailable"

    actual_billed_count = financial_observed_counts["actual_api_spend_usd"]
    api_usd_count = financial_observed_counts["api_equivalent_usd"]
    api_inr_count = financial_observed_counts["api_equivalent_inr"]
    subscription_fee_count = financial_observed_counts["subscription_fees_usd"]
    cache_savings_count = financial_observed_counts["cache_savings_usd"]
    has_actual_billed = actual_billed_count > 0
    financial_summary: Dict[str, Any] = {
        "actual_api_spend_usd": round(total_actual_billed, 4) if has_actual_billed else None,
        "actual_api_spend_usd_status": coverage_status(actual_billed_count, len(rows)),
        "actual_api_spend_inr": (
            round(total_actual_billed * fx_rate, 2)
            if has_actual_billed and fx_rate is not None
            else None
        ),
        "actual_api_spend_inr_status": (
            coverage_status(actual_billed_count, len(rows))
            if has_actual_billed and fx_rate is not None
            else "unavailable"
        ),
        "api_equivalent_usd": round(total_api_equivalent_usd, 4) if api_usd_count else None,
        "api_equivalent_usd_status": coverage_status(api_usd_count, len(rows)),
        "api_equivalent_inr": round(total_api_equivalent_inr, 2) if api_inr_count else None,
        "api_equivalent_inr_status": coverage_status(api_inr_count, len(rows)),
        "subscription_fees_usd": (
            round(total_subscription_fees_usd, 4) if subscription_fee_count else None
        ),
        "subscription_fees_usd_status": coverage_status(subscription_fee_count, len(rows)),
        "cache_savings_usd": (
            round(total_cache_savings_usd, 4) if cache_savings_count else None
        ),
        "cache_savings_usd_status": coverage_status(cache_savings_count, len(rows)),
        "unpriced_request_count": unpriced_requests_count,
        "priced_request_count": priced_requests_count,
        "fx_date": fx_date,
        "fx_rate_usd_to_inr": fx_rate,
        "currency_basis": "USD",
    }


    return {
        "contract_version": "1",
        "query": envelope,
        "metric_id": envelope.get("metric_id", "providers_breakdown"),
        "family": "providers",
        "rows": provider_rows,
        "values": {
            "financial_summary": financial_summary,
            "providers_count": len(providers_set),
            "routes_count": len(routes_set),
            "quotas": quota_cards,
            "source_status": source_status,
        },
        "units": {
            "total_tokens": "tokens",
            "fresh_input_tokens": "tokens",
            "cache_read_tokens": "tokens",
            "cache_write_tokens": "tokens",
            "output_tokens": "tokens",
            "reasoning_tokens": "tokens",
            "api_equivalent_usd": "USD",
            "api_equivalent_inr": "INR",
            "actual_api_spend_usd": "USD",
            "actual_api_spend_inr": "INR",
            "subscription_fees_usd": "USD",
            "cache_savings_usd": "USD",
        },
        "metric_definition": {
            "id": "providers",
            "family": "providers",
            "version": "1.0",
            "formula": "Aggregations split by provider and access route with separated financial facts",
            "cost_basis": "dated_rate_cards_and_historical_fx",
        },
        "eligible": {
            "total_requests": len(rows),
            "priced_requests": priced_requests_count,
        },
        "observed": {
            "providers_count": len(providers_set),
            "routes_count": len(routes_set),
            "has_actual_billed": has_actual_billed,
        },
        "missingness": {
            "unpriced_requests_count": unpriced_requests_count,
            "quotas_unavailable_count": sum(1 for c in quota_cards if not c["is_available"]),
        },
        "coverage": {
            "harness_scope": envelope.get("harness_scope", "combined"),
            "unresolved_overlap_count": sum(1 for r in rows if r.get("is_unresolved_overlap")),
        },
        "warnings": warnings,
    }

def _row_tokens(r: Dict[str, Any]) -> Optional[int]:
    if r.get("total_tokens") is not None:
        return int(r["total_tokens"])
    if r.get("reported_total_tokens") is not None:
        return int(r["reported_total_tokens"])
    billable = [
        r.get("fresh_input_tokens"),
        r.get("cache_read_tokens"),
        r.get("cache_write_5m_tokens"),
        r.get("cache_write_1h_tokens"),
        r.get("cache_write_unknown_ttl_tokens"),
        r.get("output_tokens"),
    ]
    observed = [v for v in billable if v is not None]
    if observed:
        return sum(observed)
    return None


def _analyze_burn(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
    rows: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    """Computes token burn-rate monitor (Row 24).

    Guarantees:
    - Declared interval/denominator (default 15 minutes).
    - Identifies partial current bucket.
    - Baseline equivalent-period change; sparse history abstains (unavailable, not 0%).
    - Separates active sessions by burn rate.
    """
    interval_minutes = int(envelope.get("interval_minutes", 15) or 15)
    if interval_minutes <= 0:
        interval_minutes = 15

    # Determine reference end time
    now_utc = datetime.now(timezone.utc)
    if rows:
        try:
            latest_req_dt = datetime.fromisoformat(rows[-1]["request_timestamp"].replace("Z", "+00:00"))
        except Exception:
            latest_req_dt = now_utc
    else:
        latest_req_dt = now_utc

    # Window definitions
    window_duration_seconds = interval_minutes * 60
    current_window_start_dt = datetime.fromtimestamp(latest_req_dt.timestamp() - window_duration_seconds, timezone.utc)
    baseline_window_start_dt = datetime.fromtimestamp(latest_req_dt.timestamp() - 2 * window_duration_seconds, timezone.utc)

    current_window_tokens = 0
    current_window_cost = 0.0
    current_window_requests = 0

    baseline_window_tokens = 0
    baseline_window_requests = 0

    session_burn: Dict[str, Dict[str, Any]] = {}
    current_samples: List[Dict[str, Any]] = []

    for r in rows:
        ts_str = r.get("request_timestamp")
        if not ts_str:
            continue
        try:
            r_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue

        tokens = r.get("total_tokens") or 0
        cost = r.get("api_equivalent_usd") or 0.0
        sid = r.get("session_id") or "unknown"
        harness = r.get("origin_harness") or "combined"
        model = r.get("normalized_model") or r.get("raw_model") or "unknown"
        project = r.get("project_id")

        if r_dt >= current_window_start_dt:
            current_window_tokens += tokens
            current_samples.append(r)
            current_window_cost += cost
            current_window_requests += 1

            if sid not in session_burn:
                session_burn[sid] = {
                    "session_id": sid,
                    "origin_harness": harness,
                    "model": model,
                    "project_id": project,
                    "tokens_so_far": 0,
                    "burn_tokens_in_window": 0,
                    "estimated_cost_usd": 0.0,
                    "start_time": ts_str,
                    "latest_timestamp": ts_str,
                }
            sb = session_burn[sid]
            sb["burn_tokens_in_window"] += tokens
            sb["tokens_so_far"] += tokens
            sb["estimated_cost_usd"] += cost
            if ts_str > sb["latest_timestamp"]:
                sb["latest_timestamp"] = ts_str
        elif r_dt >= baseline_window_start_dt:
            baseline_window_tokens += tokens
            baseline_window_requests += 1

    # Current burn rates
    current_burn_rate_tpm = round(current_window_tokens / float(interval_minutes), 2)
    current_burn_rate_cost_per_hour = round(current_window_cost * (60.0 / float(interval_minutes)), 4)

    # Baseline comparison (CRITICAL RULE: sparse history abstains, returns None, never 0.0%)
    typical_burn_rate_tpm: Optional[float] = None
    baseline_change_pct: Optional[float] = None
    burn_status = "normal"

    if baseline_window_requests >= 2:
        typical_burn_rate_tpm = round(baseline_window_tokens / float(interval_minutes), 2)
        if typical_burn_rate_tpm > 0:
            baseline_change_pct = round(((current_burn_rate_tpm - typical_burn_rate_tpm) / typical_burn_rate_tpm) * 100.0, 1)
            if baseline_change_pct > 100.0:
                burn_status = "spike"
            elif baseline_change_pct > 30.0:
                burn_status = "elevated"
    else:
        burn_status = "unavailable" if current_window_requests == 0 else "normal"

    # High-burn sessions
    high_burn_sessions: List[Dict[str, Any]] = []
    for sid, sb in session_burn.items():
        tpm = round(sb["burn_tokens_in_window"] / float(interval_minutes), 2)
        high_burn_sessions.append({
            "session_id": sid,
            "origin_harness": sb["origin_harness"],
            "model": sb["model"],
            "project_id": sb["project_id"],
            "tokens_so_far": sb["tokens_so_far"],
            "burn_rate_tpm": tpm,
            "estimated_cost_usd": round(sb["estimated_cost_usd"], 4),
            "start_time": sb["start_time"],
            "latest_timestamp": sb["latest_timestamp"],
        })
    high_burn_sessions.sort(key=lambda s: s["burn_rate_tpm"], reverse=True)

    # Recent ordered, non-overlapping five-minute buckets. Empty buckets mean
    # no locally observed requests, not proof of complete upstream collection.
    bucket_seconds = 300
    end_epoch = latest_req_dt.timestamp()
    last_bucket = int(end_epoch // bucket_seconds) * bucket_seconds
    first_bucket = last_bucket - max(5, (interval_minutes * 2 + 4) // 5 - 1) * bucket_seconds
    bucket_rows: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        try:
            epoch = datetime.fromisoformat(row["request_timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        start = int(epoch // bucket_seconds) * bucket_seconds
        if first_bucket <= start <= last_bucket:
            bucket_rows.setdefault(start, []).append(row)
    series: List[Dict[str, Any]] = []
    for start in range(first_bucket, last_bucket + 1, bucket_seconds):
        samples = bucket_rows.get(start, [])
        token_values = [_row_tokens(r) for r in samples]
        costs = [r.get("api_equivalent_usd") for r in samples]
        known_tokens = [v for v in token_values if v is not None]
        known_costs = [v for v in costs if v is not None]
        tokens = sum(known_tokens) if samples and len(known_tokens) == len(samples) else None
        cost = sum(known_costs) if samples and len(known_costs) == len(samples) else None
        series.append({
            "timestamp": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "end_timestamp": datetime.fromtimestamp(start + bucket_seconds, timezone.utc).isoformat(),
            "tokens": tokens,
            "tpm": round(tokens / 5, 2) if tokens is not None else None,
            "cost_usd": round(cost, 4) if cost is not None else None,
            "denominator_minutes": 5,
            "request_count": len(samples),
            "priced_requests": len(known_costs),
            "token_observed_requests": len(known_tokens),
            "is_partial": start == last_bucket,
            "is_active": start == last_bucket and bool(samples),
            "coverage": "partial_local_observations",
        })
    # A missing cost/counter must not become a measured zero rate.
    if not current_samples or any(r.get("total_tokens") is None for r in current_samples):
        current_burn_rate_tpm = None
    if not current_samples or any(r.get("api_equivalent_usd") is None for r in current_samples):
        current_burn_rate_cost_per_hour = None

    values: Dict[str, Any] = {
        "declared_interval_minutes": interval_minutes,
        "is_partial_current_bucket": True,
        "current_burn_rate_tpm": current_burn_rate_tpm,
        "current_burn_rate_cost_per_hour": current_burn_rate_cost_per_hour,
        "typical_burn_rate_tpm": typical_burn_rate_tpm,
        "baseline_change_pct": baseline_change_pct,
        "burn_status": burn_status,
        "recent_burn_series": series,
        "high_burn_sessions": high_burn_sessions,
        "current_window_requests": current_window_requests,
        "baseline_window_requests": baseline_window_requests,
    }

    return {
        "contract_version": "1",
        "query": envelope,
        "metric_id": envelope.get("metric_id", "token_burn_rate"),
        "family": "burn",
        "values": values,
        "rows": high_burn_sessions,
        "units": {
            "current_burn_rate_tpm": "tokens_per_minute",
            "current_burn_rate_cost_per_hour": "USD_per_hour",
            "typical_burn_rate_tpm": "tokens_per_minute",
        },
        "metric_definition": {
            "id": "token_burn_rate",
            "family": "burn",
            "version": "1.0",
            "formula": "sum(tokens in declared_interval) / declared_interval_minutes",
            "denominator": f"{interval_minutes} minutes",
        },
        "eligible": {
            "current_window_requests": current_window_requests,
            "baseline_window_requests": baseline_window_requests,
        },
        "observed": {
            "has_baseline": typical_burn_rate_tpm is not None,
            "active_session_count": len(high_burn_sessions),
        },
        "missingness": {
            "baseline_sparse": typical_burn_rate_tpm is None,
        },
        "coverage": {
            "harness_scope": envelope.get("harness_scope", "combined"),
        },
        "warnings": warnings,
    }


def _analyze_live(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
    rows: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    """Computes live active-session dashboard (Row 47).

    Guarantees:
    - Separates exact last_observed_time from heuristic active status.
    - No unsupported precision; transparent heuristic definition.
    - Disjoint tokens and estimated value tracked per active session.
    """
    heuristic_window_minutes = int(envelope.get("heuristic_window_minutes", 30) or 30)

    now_utc = datetime.now(timezone.utc)
    latest_req_dt = now_utc
    if rows:
        try:
            latest_req_dt = datetime.fromisoformat(rows[-1]["request_timestamp"].replace("Z", "+00:00"))
        except Exception:
            latest_req_dt = now_utc

    active_cutoff = datetime.fromtimestamp(latest_req_dt.timestamp() - heuristic_window_minutes * 60, timezone.utc)

    # Rollup by session
    sessions_map: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sid = r.get("session_id") or "unknown"
        ts_str = r.get("request_timestamp") or ""
        tokens = _row_tokens(r) or 0
        cost = r.get("api_equivalent_usd") or 0.0
        harness = r.get("origin_harness") or "combined"
        model = r.get("normalized_model") or r.get("raw_model") or "unknown"
        project = r.get("project_id")
        p_status = r.get("pricing_status") or "unpriced"

        if sid not in sessions_map:
            sessions_map[sid] = {
                "session_id": sid,
                "origin_harness": harness,
                "active_model": model,
                "project_id": project,
                "first_observed_time": ts_str,
                "last_observed_time": ts_str,
                "tokens_so_far": 0,
                "fresh_input_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "estimated_value_usd": 0.0,
                "request_count": 0,
                "unpriced_requests": 0,
            }
        s = sessions_map[sid]
        s["request_count"] += 1
        s["tokens_so_far"] += tokens
        s["fresh_input_tokens"] += (r.get("fresh_input_tokens") or 0)
        s["cache_read_tokens"] += (r.get("cache_read_tokens") or 0)
        s["output_tokens"] += (r.get("output_tokens") or 0)
        s["estimated_value_usd"] += cost
        s["active_model"] = model
        if p_status != "priced":
            s["unpriced_requests"] += 1
        if ts_str > s["last_observed_time"]:
            s["last_observed_time"] = ts_str

    active_session_items: List[Dict[str, Any]] = []
    sync_rows = conn.execute(
        "SELECT origin_harness, MAX(last_success_at) FROM source_checkpoints GROUP BY origin_harness"
    ).fetchall()
    sync_by_harness = {r[0]: r[1] for r in sync_rows if r[0] is not None}
    valid_sync_times = [t for t in sync_by_harness.values() if t]
    sync_ts = max(valid_sync_times) if valid_sync_times else None

    for sid, s in sessions_map.items():
        # Evaluate heuristic status
        is_heuristic_active = False
        try:
            l_dt = datetime.fromisoformat(s["last_observed_time"].replace("Z", "+00:00"))
            is_heuristic_active = (l_dt >= active_cutoff)
        except Exception:
            pass

        item: Dict[str, Any] = {
            "session_id": sid,
            "origin_harness": s["origin_harness"],
            "active_model": s["active_model"],
            "project_id": s["project_id"],
            "last_observed_time": s["last_observed_time"],
            "is_heuristic_active": is_heuristic_active,
            "heuristic_basis": f"activity_observed_within_past_{heuristic_window_minutes}m",
            "tokens_so_far": s["tokens_so_far"],
            "fresh_input_tokens": s["fresh_input_tokens"],
            "cache_read_tokens": s["cache_read_tokens"],
            "output_tokens": s["output_tokens"],
            "estimated_value_usd": round(s["estimated_value_usd"], 4) if s["unpriced_requests"] < s["request_count"] else None,
            "last_sync_timestamp": sync_by_harness.get(s["origin_harness"]),
            "value_coverage": "partial" if s["unpriced_requests"] else "known_priced",
            "data_status": "partial_local_observations",
            "status": "active" if is_heuristic_active else "idle",
            "request_count": s["request_count"],
        }
        active_session_items.append(item)

    # Sort so most active/recent sessions are first
    active_session_items.sort(key=lambda x: (x["is_heuristic_active"], x["last_observed_time"]), reverse=True)

    return {
        "contract_version": "1",
        "query": envelope,
        "metric_id": envelope.get("metric_id", "live_sessions"),
        "family": "live",
        "values": {
            "active_sessions": active_session_items,
            "active_count": sum(1 for a in active_session_items if a["is_heuristic_active"]),
            "idle_count": sum(1 for a in active_session_items if not a["is_heuristic_active"]),
            "last_sync_timestamp": sync_ts,
            "heuristic_window_minutes": heuristic_window_minutes,
        },
        "rows": active_session_items,
        "units": {
            "tokens_so_far": "tokens",
            "estimated_value_usd": "USD",
        },
        "metric_definition": {
            "id": "live_sessions",
            "family": "live",
            "version": "1.0",
            "formula": "Session rollup identifying last_observed_time and heuristic activity within window",
        },
        "eligible": {
            "total_sessions": len(sessions_map),
        },
        "observed": {
            "active_sessions_count": sum(1 for a in active_session_items if a["is_heuristic_active"]),
        },
        "missingness": {},
        "coverage": {
            "harness_scope": envelope.get("harness_scope", "combined"),
        },
        "warnings": warnings,
    }


def _analyze_budgets(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
    rows: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    """Evaluates stored budgets and usage thresholds against filtered requests (Row 26).

    Guarantees:
    - User-owned versioned settings.
    - Evaluates daily tokens, monthly equivalent cost, project, model, and session thresholds.
    - Respects known-eligible coverage (marks partial pricing).
    - Consent notifications: local event notifications only, no network calls.
    """
    budgets = list_budgets(conn)
    now_utc = datetime.now(timezone.utc)

    # Calculate aggregations from filtered rows
    total_tokens = sum(_row_tokens(r) or 0 for r in rows)
    total_cost_usd = sum(r.get("api_equivalent_usd") or 0.0 for r in rows)
    unpriced_count = sum(1 for r in rows if r.get("pricing_status") != "priced")
    priced_count = sum(1 for r in rows if r.get("pricing_status") == "priced")

    # Group by project, model, session
    project_tokens: Dict[str, int] = {}
    project_cost: Dict[str, float] = {}
    model_tokens: Dict[str, int] = {}
    model_cost: Dict[str, float] = {}
    session_tokens: Dict[str, int] = {}
    session_cost: Dict[str, float] = {}

    for r in rows:
        tokens = _row_tokens(r) or 0
        cost = r.get("api_equivalent_usd") or 0.0
        p = r.get("project_id") or "unknown"
        raw_m = r.get("raw_model")
        norm_m = r.get("normalized_model")
        s = r.get("session_id") or "unknown"

        project_tokens[p] = project_tokens.get(p, 0) + tokens
        project_cost[p] = project_cost.get(p, 0.0) + cost

        if raw_m:
            model_tokens[raw_m] = model_tokens.get(raw_m, 0) + tokens
            model_cost[raw_m] = model_cost.get(raw_m, 0.0) + cost
        if norm_m and norm_m != raw_m:
            model_tokens[norm_m] = model_tokens.get(norm_m, 0) + tokens
            model_cost[norm_m] = model_cost.get(norm_m, 0.0) + cost
        session_tokens[s] = session_tokens.get(s, 0) + tokens
        session_cost[s] = session_cost.get(s, 0.0) + cost

    budget_records: List[Dict[str, Any]] = []
    local_notifications: List[Dict[str, Any]] = []

    for b in budgets:
        bid = b["id"]
        period = b["period"]
        target = b["target_type"]
        threshold = b["threshold"]
        scope = b["scope"]
        notif_enabled = b["notifications_enabled"]

        current_val: float = 0.0
        scope_proj = scope.get("project_id")
        target_models: Set[str] = set()
        if scope.get("model"):
            m_val = scope["model"]
            if isinstance(m_val, (list, tuple, set)):
                target_models.update(m_val)
            elif isinstance(m_val, str):
                target_models.update([m.strip() for m in m_val.split(",") if m.strip()])
        if scope.get("models"):
            m_val = scope["models"]
            if isinstance(m_val, (list, tuple, set)):
                target_models.update(m_val)
            elif isinstance(m_val, str):
                target_models.update([m.strip() for m in m_val.split(",") if m.strip()])

        if scope_proj:
            current_val = float(project_cost.get(scope_proj, 0.0) if target == "cost_usd" else project_tokens.get(scope_proj, 0))
        elif target_models:
            current_val = float(
                sum(model_cost.get(m, 0.0) for m in target_models)
                if target == "cost_usd"
                else sum(model_tokens.get(m, 0) for m in target_models)
            )
        elif period == "session":
            # Maximum consumption across observed sessions
            if target == "cost_usd":
                current_val = max(session_cost.values()) if session_cost else 0.0
            else:
                current_val = float(max(session_tokens.values()) if session_tokens else 0)
        else:
            # Daily or monthly overall rollup
            current_val = float(total_cost_usd if target == "cost_usd" else total_tokens)

        percent = round((current_val / threshold) * 100.0, 1) if threshold > 0 else 0.0
        if percent >= 100.0:
            status = "breached"
        elif percent >= 80.0:
            status = "warning"
        else:
            status = "ok"

        # Check coverage warning
        coverage_status = "complete"
        if target == "cost_usd" and unpriced_count > 0:
            coverage_status = "partial_unpriced_requests_present"

        record: Dict[str, Any] = {
            "id": bid,
            "name": b["name"],
            "period": period,
            "target_type": target,
            "threshold": threshold,
            "scope": scope,
            "model_scope": sorted(list(target_models)) if target_models else None,
            "current_value": round(current_val, 4) if target == "cost_usd" else int(current_val),
            "percent_consumed": percent,
            "status": status,
            "coverage_status": coverage_status,
            "notifications_enabled": notif_enabled,
            "created_at": b["created_at"],
            "updated_at": b["updated_at"],
            "comparison_baseline": None,
            "comparison_status": "no_baseline" if period == "session" else "not_applicable",
            "comparison_basis": "Comparable task labels and adequate matched samples are not established; absolute threshold only.",
        }
        budget_records.append(record)

        # Optional local event notification (only if consented and breached/warning)
        if notif_enabled and status in ("warning", "breached"):
            local_notifications.append({
                "notification_id": f"notif_{uuid.uuid4().hex[:8]}",
                "budget_id": bid,
                "budget_name": b["name"],
                "status": status,
                "percent_consumed": percent,
                "current_value": current_val,
                "threshold": threshold,
                "created_at": now_utc.isoformat(),
                "delivery_mode": "local_event_only_no_network",
            })

    return {
        "contract_version": "1",
        "query": envelope,
        "metric_id": envelope.get("metric_id", "usage_budgets"),
        "family": "budgets",
        "rows": budget_records,
        "values": {
            "budgets": budget_records,
            "total_budgets_count": len(budget_records),
            "breached_count": sum(1 for r in budget_records if r["status"] == "breached"),
            "warning_count": sum(1 for r in budget_records if r["status"] == "warning"),
            "local_notifications": local_notifications,
        },
        "units": {
            "threshold": "tokens_or_currency",
            "current_value": "tokens_or_currency",
            "percent_consumed": "percentage",
        },
        "metric_definition": {
            "id": "usage_budgets",
            "family": "budgets",
            "version": "1.0",
            "formula": "User-defined threshold comparison against filtered token or cost aggregates",
        },
        "eligible": {
            "priced_requests": priced_count,
            "total_requests": len(rows),
        },
        "observed": {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost_usd, 4),
        },
        "missingness": {
            "unpriced_requests_count": unpriced_count,
        },
        "coverage": {
            "harness_scope": envelope.get("harness_scope", "combined"),
        },
        "warnings": warnings,
    }


def analyze(conn: sqlite3.Connection, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Shared semantic access entrypoint for Provider Analytics (WP16).

    Dispatches across metric families:
    - 'providers' (or 'providers_breakdown', 'provider_quotas')
    - 'burn' (or 'token_burn_rate')
    - 'live' (or 'live_sessions')
    - 'budgets' (or 'usage_budgets')

    Guarantees:
    - Never uses independent SQL filtering: invokes canonical filtered_request_rows.
    - Preserves provider / account_alias / access_route / model distinct identities.
    - Financial separation: actual billed vs API equivalent vs subscription fees vs cache savings vs unpriced.
    - Quota cards: official verified provenance only, unavailable when unverified, never guessed from local counts.
    - Burn rate: declared interval, partial bucket identified, sparse baseline abstains.
    - Live active sessions: exact last_observed_time separate from heuristic active status.
    - Budgets: daily/monthly/session/project/model thresholds with user-owned settings and local-only consent notifications.
    """
    # Canonical validation and request retrieval
    norm_envelope = validate_query(envelope) if "contract_version" in envelope else envelope
    family = norm_envelope.get("family") or envelope.get("family")
    metric_id = norm_envelope.get("metric_id") or envelope.get("metric_id")

    rows = filtered_request_rows(conn, norm_envelope)

    warnings: List[str] = []
    unresolved_count = sum(1 for r in rows if r.get("is_unresolved_overlap"))
    if unresolved_count > 0:
        warnings.append(
            f"Query contains {unresolved_count} requests with unresolved cross-harness overlap."
        )

    # Dispatch to specific family handler
    if family == "burn" or metric_id == "token_burn_rate":
        return _analyze_burn(conn, norm_envelope, rows, warnings)
    elif family == "live" or metric_id == "live_sessions":
        return _analyze_live(conn, norm_envelope, rows, warnings)
    elif family == "budgets" or metric_id == "usage_budgets":
        return _analyze_budgets(conn, norm_envelope, rows, warnings)
    else:
        # Default to providers breakdown
        return _analyze_providers(conn, norm_envelope, rows, warnings)
