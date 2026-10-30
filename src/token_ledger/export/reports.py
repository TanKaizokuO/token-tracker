"""Local report definitions and calendar-aware report scheduling."""

from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from token_ledger.privacy import delete_snapshot_file, redact_for_export, sanitize_account_alias
from token_ledger.query import execute_query
from token_ledger.metrics.forecast import compare_periods
from token_ledger.export.snapshot import (
    ALLOWED_EXPORT_FORMATS,
    MAX_EXPORT_ROWS,
    ExportRecord,
    generate_csv_export,
    generate_html_export,
    generate_json_export,
    generate_pdf_export,
    generate_png_export,
    record_export_inventory,
)


FREQUENCIES = frozenset({"manual", "daily", "weekly", "monthly"})
DEFAULT_EXPORT_FORMATS = ("csv", "json", "html", "pdf", "png")
_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PATH_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(?:~[/\\]|/(?:home|Users|tmp|private|var|mnt|workspace|root)/)[^\s\"'<>]+"
    r"|(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'<>]+"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Optional[Union[str, datetime]], *, default_now: bool = False) -> Optional[datetime]:
    if value is None:
        return _utc_now() if default_now else None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc
    else:
        raise ValueError("Timestamp must be an ISO-8601 string or datetime.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_report_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.casefold()
            if normalized in {"path", "file_path", "source_path", "source_file", "file_locator", "source_locator", "working_directory", "cwd", "project_path", "log_path"} or normalized.endswith(("_path", "_locator")):
                result[key] = "[REDACTED_PATH]"
            else:
                result[key] = _safe_report_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_report_value(child) for child in value]
    if isinstance(value, str):
        return _PATH_TEXT.sub("[REDACTED_PATH]", value)
    return value


def _safe_object(value: Any, label: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    redacted = _safe_report_value(redact_for_export(dict(value)))
    if not isinstance(redacted, dict):
        raise ValueError(f"{label} must be a JSON object after redaction.")
    try:
        json.dumps(redacted, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON-compatible values.") from exc
    return redacted


@dataclass
class ReportDefinition:
    item_id: Optional[str] = None
    name: str = ""
    frequency: str = "manual"
    filter_state: Dict[str, Any] = field(default_factory=dict)
    threshold_config: Dict[str, Any] = field(default_factory=dict)
    pricing_assumptions: Dict[str, Any] = field(default_factory=dict)
    project_id: Optional[str] = None
    account_alias: str = "default"
    last_generated_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReportDefinition":
        return cls(
            item_id=value.get("item_id"),
            name=value.get("name", ""),
            frequency=value.get("frequency", "manual"),
            filter_state=value.get("filter_state", {}),
            threshold_config=value.get("threshold_config", {}),
            pricing_assumptions=value.get("pricing_assumptions", {}),
            project_id=value.get("project_id"),
            account_alias=value.get("account_alias", "default"),
            last_generated_at=value.get("last_generated_at"),
            created_at=value.get("created_at"),
            updated_at=value.get("updated_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_type": "report",
            "name": self.name,
            "frequency": self.frequency,
            "filter_state": self.filter_state,
            "threshold_config": self.threshold_config,
            "pricing_assumptions": self.pricing_assumptions,
            "project_id": self.project_id,
            "account_alias": self.account_alias,
            "last_generated_at": self.last_generated_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ReportExecutionResult:
    report_id: str
    generated_at: str
    exports: List[ExportRecord]
    coverage: Dict[str, Any]
    query: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "exports": [record.to_dict() for record in self.exports],
            "coverage": self.coverage,
            "query": self.query,
        }


def _definition_from_row(row: sqlite3.Row) -> ReportDefinition:
    def load_object(column: str) -> Dict[str, Any]:
        try:
            value = json.loads(row[column] or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    return ReportDefinition(
        item_id=row["item_id"],
        name=row["name"],
        frequency=row["frequency"] or "manual",
        filter_state=load_object("filter_state_json"),
        threshold_config=load_object("threshold_config_json"),
        pricing_assumptions=load_object("pricing_assumptions_json"),
        project_id=row["project_id"],
        account_alias=row["account_alias"],
        last_generated_at=row["last_generated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def save_report_definition(conn: sqlite3.Connection, definition: ReportDefinition) -> ReportDefinition:
    """Create or update a report definition in ``alerts_and_reports``."""
    if isinstance(definition, Mapping):
        definition = ReportDefinition.from_dict(definition)
    if not isinstance(definition, ReportDefinition):
        raise TypeError("definition must be a ReportDefinition or mapping.")
    name = str(definition.name or "").strip()
    if not name or len(name) > 256:
        raise ValueError("Report name must contain 1 to 256 characters.")
    frequency = str(definition.frequency or "").strip().lower()
    if frequency not in FREQUENCIES:
        raise ValueError(f"frequency must be one of {sorted(FREQUENCIES)}.")
    item_id = str(definition.item_id or f"report_{uuid.uuid4().hex}")
    if not _REPORT_ID.fullmatch(item_id):
        raise ValueError("Report item_id must be a safe identifier of at most 128 characters.")
    project_id = definition.project_id
    if project_id is not None:
        project_id = str(project_id).strip()
        if not project_id or len(project_id) > 256:
            raise ValueError("project_id must be a non-empty string of at most 256 characters or null.")
    account_alias = sanitize_account_alias(definition.account_alias or "default")
    filter_state = _safe_object(definition.filter_state, "filter_state")
    threshold_config = _safe_object(definition.threshold_config, "threshold_config")
    pricing_assumptions = _safe_object(definition.pricing_assumptions, "pricing_assumptions")
    now = _iso_utc(_utc_now())
    requested_last = _timestamp(definition.last_generated_at)
    requested_created = _timestamp(definition.created_at)

    with conn:
        existing = conn.execute(
            "SELECT item_type, created_at, last_generated_at FROM alerts_and_reports WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if existing is not None and existing["item_type"] != "report":
            raise ValueError(f"item_id {item_id!r} belongs to a non-report item.")
        created_at = existing["created_at"] if existing is not None else None
        if not created_at:
            created_at = _iso_utc(requested_created or _timestamp(now))
        last_generated_at = requested_last
        if last_generated_at is None and existing is not None:
            last_generated_at = _timestamp(existing["last_generated_at"])
        last_generated_text = _iso_utc(last_generated_at) if last_generated_at else None
        conn.execute(
            """
            INSERT INTO alerts_and_reports (
                item_id, item_type, name, frequency, threshold_config_json,
                filter_state_json, pricing_assumptions_json, last_generated_at,
                created_at, updated_at, project_id, account_alias
            ) VALUES (?, 'report', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                item_type = 'report', name = excluded.name, frequency = excluded.frequency,
                threshold_config_json = excluded.threshold_config_json,
                filter_state_json = excluded.filter_state_json,
                pricing_assumptions_json = excluded.pricing_assumptions_json,
                last_generated_at = excluded.last_generated_at,
                updated_at = excluded.updated_at, project_id = excluded.project_id,
                account_alias = excluded.account_alias
            """,
            (
                item_id,
                name,
                frequency,
                json.dumps(threshold_config, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                json.dumps(filter_state, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                json.dumps(pricing_assumptions, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                last_generated_text,
                created_at,
                now,
                project_id,
                account_alias,
            ),
        )
    return get_report_definition(conn, item_id)  # type: ignore[return-value]


def get_report_definition(conn: sqlite3.Connection, item_id: str) -> Optional[ReportDefinition]:
    row = conn.execute(
        "SELECT * FROM alerts_and_reports WHERE item_id = ? AND item_type = 'report'",
        (item_id,),
    ).fetchone()
    return _definition_from_row(row) if row is not None else None


def list_report_definitions(
    conn: sqlite3.Connection,
    project_id: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> List[ReportDefinition]:
    clauses = ["item_type = 'report'"]
    parameters: List[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        parameters.append(project_id)
    if account_alias is not None:
        clauses.append("account_alias = ?")
        parameters.append(sanitize_account_alias(account_alias))
    cursor = conn.execute(
        "SELECT * FROM alerts_and_reports WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC, item_id",
        parameters,
    )
    return [_definition_from_row(row) for row in cursor.fetchall()]


def delete_report_definition(conn: sqlite3.Connection, item_id: str) -> bool:
    with conn:
        cursor = conn.execute(
            "DELETE FROM alerts_and_reports WHERE item_id = ? AND item_type = 'report'",
            (item_id,),
        )
    return cursor.rowcount > 0


def _schedule_timezone(definition: ReportDefinition) -> ZoneInfo:
    state = definition.filter_state
    name = state.get("schedule_timezone") or state.get("timezone")
    if not name and isinstance(state.get("time_range"), Mapping):
        name = state["time_range"].get("timezone")
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid report schedule timezone for {definition.item_id}: {name!r}") from exc


def _month_after(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _monthly_occurrence(anchor: datetime, year: int, month: int) -> datetime:
    day_of_month = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day_of_month)


def _next_due(definition: ReportDefinition) -> Optional[datetime]:
    if definition.frequency == "manual" or not definition.last_generated_at:
        return None
    zone = _schedule_timezone(definition)
    last_generated = _timestamp(definition.last_generated_at)
    if last_generated is None:
        return None
    anchor_utc = _timestamp(definition.created_at) or last_generated
    last_local = last_generated.astimezone(zone)
    anchor = anchor_utc.astimezone(zone)

    if definition.frequency == "daily":
        day_offset = max(0, (last_local.date() - anchor.date()).days)
        candidate_date = anchor.date() + timedelta(days=day_offset)
        candidate = datetime.combine(candidate_date, anchor.timetz(), tzinfo=zone)
        if candidate.astimezone(timezone.utc) <= last_generated:
            candidate = datetime.combine(candidate_date + timedelta(days=1), anchor.timetz(), tzinfo=zone)
    elif definition.frequency == "weekly":
        elapsed_days = (last_local.date() - anchor.date()).days
        week_offset = max(0, elapsed_days // 7)
        candidate_date = anchor.date() + timedelta(days=week_offset * 7)
        candidate = datetime.combine(candidate_date, anchor.timetz(), tzinfo=zone)
        if candidate.astimezone(timezone.utc) <= last_generated:
            candidate = datetime.combine(candidate_date + timedelta(days=7), anchor.timetz(), tzinfo=zone)
    else:
        candidate_year, candidate_month = anchor.year, anchor.month
        candidate = _monthly_occurrence(anchor, candidate_year, candidate_month)
        while candidate.astimezone(timezone.utc) <= last_generated:
            candidate_year, candidate_month = _month_after(candidate_year, candidate_month)
            candidate = _monthly_occurrence(anchor, candidate_year, candidate_month)

    # Normalize nonexistent local wall times across DST gaps to the next valid local instant.
    return candidate.astimezone(timezone.utc).astimezone(zone).astimezone(timezone.utc)


def get_due_reports(conn: sqlite3.Connection, now_iso: Optional[str] = None) -> List[ReportDefinition]:
    """Return scheduled reports whose next local calendar occurrence has arrived."""
    now = _timestamp(now_iso, default_now=True)
    if now is None:
        return []
    due: List[tuple[datetime, ReportDefinition]] = []
    for definition in list_report_definitions(conn):
        if definition.frequency == "manual":
            continue
        if not definition.last_generated_at:
            due.append((now, definition))
            continue
        next_time = _next_due(definition)
        if next_time is not None and now >= next_time:
            due.append((next_time, definition))
    due.sort(key=lambda item: (item[0], item[1].item_id or ""))
    return [definition for _, definition in due]


def _query_envelope(definition: ReportDefinition) -> Dict[str, Any]:
    state = definition.filter_state
    raw_query = state.get("query") if isinstance(state.get("query"), Mapping) else state
    envelope = dict(raw_query)
    envelope.pop("schedule_timezone", None)
    envelope.setdefault("contract_version", "1")
    envelope.setdefault("filters", {})
    pagination = envelope.get("pagination") or {}
    if not isinstance(pagination, Mapping):
        raise ValueError("Report query pagination must be an object.")
    normalized_pagination = dict(pagination)
    try:
        limit = int(normalized_pagination.get("limit", MAX_EXPORT_ROWS))
    except (TypeError, ValueError) as exc:
        raise ValueError("Report query pagination limit must be an integer.") from exc
    normalized_pagination["limit"] = min(limit, MAX_EXPORT_ROWS)
    normalized_pagination.setdefault("offset", 0)
    envelope["pagination"] = normalized_pagination

    if definition.project_id:
        filters = envelope.get("filters")
        if not isinstance(filters, Mapping):
            raise ValueError("Report query filters must be an object.")
        scoped_filters = dict(filters)
        projects = scoped_filters.get("project")
        if projects is not None:
            values = [projects] if isinstance(projects, str) else list(projects) if isinstance(projects, (list, tuple, set)) else []
            if definition.project_id not in {str(value) for value in values}:
                raise ValueError("Report project scope conflicts with the query's project filter.")
        scoped_filters["project"] = [definition.project_id]
        envelope["filters"] = scoped_filters
    return envelope


def _report_periods(definition: ReportDefinition, envelope: Dict[str, Any], generated: datetime) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    """Calendar-aligned defaults; explicit bounded ranges retain their wall-clock shape."""
    saved_range = envelope.get("time_range") or {}
    if definition.frequency == "manual":
        if not saved_range.get("start") or not saved_range.get("end"):
            return None
        zone = ZoneInfo(saved_range.get("timezone") or str(_schedule_timezone(definition)))
        from token_ledger.query.engine import parse_and_validate_iso_instant
        def _instant(val: str) -> datetime:
            _, utc = parse_and_validate_iso_instant(val, zone)
            return _timestamp(utc).astimezone(zone)
        start = _instant(saved_range["start"])
        end = _instant(saved_range["end"])
        duration = end - start
        if duration <= timedelta(0):
            return None
        prior_end = start
        prior_start = start - duration
        current = {"start": _iso_utc(start), "end": _iso_utc(end), "timezone": str(zone), "label": "Current"}
        prior = {"start": _iso_utc(prior_start), "end": _iso_utc(prior_end), "timezone": str(zone), "label": "Equivalent prior"}
        return prior, current
    saved_range = envelope.get("time_range") or {}
    zone = ZoneInfo(saved_range.get("timezone") or str(_schedule_timezone(definition)))
    local = generated.astimezone(zone)
    boundary = datetime.combine(local.date(), time.min, tzinfo=zone)
    if definition.frequency == "weekly":
        boundary -= timedelta(days=boundary.weekday())
    elif definition.frequency == "monthly":
        boundary = boundary.replace(day=1)

    def previous(value: datetime) -> datetime:
        if definition.frequency == "monthly":
            year, month = (value.year - 1, 12) if value.month == 1 else (value.year, value.month - 1)
            shifted = _monthly_occurrence(value, year, month)
        else:
            shifted = value - timedelta(days=7 if definition.frequency == "weekly" else 1)
        return shifted.astimezone(timezone.utc).astimezone(zone)

    # The query contract parses naive ISO values in the selected timezone.
    from token_ledger.query.engine import parse_and_validate_iso_instant
    def instant(value: str) -> datetime:
        _, utc = parse_and_validate_iso_instant(value, zone)
        return _timestamp(utc).astimezone(zone)

    end = instant(saved_range["end"]) if saved_range.get("end") else (local if saved_range.get("start") else boundary)
    start = instant(saved_range["start"]) if saved_range.get("start") else previous(end)
    prior_start, prior_end = previous(start), previous(end)
    current = {"start": _iso_utc(start), "end": _iso_utc(end), "timezone": str(zone), "label": "Current"}
    prior = {"start": _iso_utc(prior_start), "end": _iso_utc(prior_end), "timezone": str(zone), "label": "Equivalent prior"}
    return prior, current


def _managed_root(managed_root: Optional[Union[str, os.PathLike[str]]]) -> str:
    if managed_root is None:
        project_root = Path(__file__).resolve().parents[3]
        root = project_root / "data" / "exports"
    else:
        root = Path(managed_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(root.resolve())


def _export_function(export_format: str):
    return {
        "csv": generate_csv_export,
        "json": generate_json_export,
        "html": generate_html_export,
        "pdf": generate_pdf_export,
        "png": generate_png_export,
    }[export_format]


def _execute_report(
    conn: sqlite3.Connection,
    report_def_or_id: Union[ReportDefinition, str],
    formats: Optional[Sequence[str]] = None,
    managed_root: Optional[Union[str, os.PathLike[str]]] = None,
    pricing_fx: Optional[Mapping[str, Any]] = None,
    now_iso: Optional[str] = None,
) -> ReportExecutionResult:
    """Execute the stored versioned query and write selected local exports."""
    if isinstance(report_def_or_id, ReportDefinition):
        definition = get_report_definition(conn, report_def_or_id.item_id or "")
        if definition is None:
            definition = save_report_definition(conn, report_def_or_id)
    else:
        definition = get_report_definition(conn, str(report_def_or_id))
    if definition is None:
        raise KeyError(f"Report definition not found: {report_def_or_id!r}")

    selected_formats = DEFAULT_EXPORT_FORMATS if formats is None else formats
    if isinstance(selected_formats, (str, bytes)) or not isinstance(selected_formats, Sequence) or not selected_formats:
        raise ValueError("formats must be a non-empty sequence of supported formats.")
    chosen: List[str] = []
    for item in selected_formats:
        if not isinstance(item, str) or item not in ALLOWED_EXPORT_FORMATS:
            raise ValueError(f"Unsupported report export format: {item!r}")
        if item not in chosen:
            chosen.append(item)
    worker_pricing = _safe_object(pricing_fx, "pricing_fx")
    generated = _timestamp(now_iso, default_now=True)
    assert generated is not None
    generated_at = _iso_utc(generated)

    envelope = _query_envelope(definition)
    periods = _report_periods(definition, envelope, generated)
    if periods:
        envelope["time_range"] = {key: value for key, value in periods[1].items() if key != "label"}
    query_response = execute_query(conn, envelope)
    comparison = compare_periods(conn, query_response["query"], *periods) if periods else None
    if comparison:
        prior_envelope = dict(envelope)
        prior_envelope["time_range"] = {key: value for key, value in periods[0].items() if key != "label"}
        prior_response = execute_query(conn, prior_envelope)
        prior_observed = prior_response.get("observed", 0)
        if prior_observed == 0 or comparison.get("period_a", {}).get("metrics", {}).get("requests_count", 0) == 0:
            comparison = "unavailable"
        else:
            for label, response in (("prior_query_result", prior_response), ("current_query_result", query_response)):
                comparison[label] = {key: response.get(key) for key in
                                     ("query", "values", "coverage", "missingness", "pricing_fx", "warnings", "eligible", "observed")}
            comparison["assumptions"] = [
                "Half-open local calendar periods; prior boundaries shift one report frequency, with month-end clamping and DST normalization.",
                "Absent bounds default to the last completed calendar day, Monday-based week, or month; a start-only range ends at generation time.",
                "Explicit partial ranges retain local wall-clock boundaries; unequal month lengths and DST durations are disclosed, not normalized.",
                "Identical shared filters, pricing, deduplication and archive policy apply; comparison metrics use request-level evidence only.",
                "No observed requests does not prove zero usage or complete collection; missing prices and counters remain unavailable.",
                comparison["causal_claim_disclaimer"],
            ]
    result_rows = query_response.get("rows") or []
    if len(result_rows) > MAX_EXPORT_ROWS:
        raise ValueError(f"The shared query returned more than the {MAX_EXPORT_ROWS}-row report limit.")
    query = query_response.get("query")
    if not isinstance(query, dict):
        raise ValueError("Shared query engine returned no normalized query envelope.")
    report_data = {
        "report_id": definition.item_id,
        "title": definition.name,
        "generated_at": generated_at,
        "project_id": definition.project_id,
        "account_alias": definition.account_alias,
        "query": query,
        "values": query_response.get("values", {}),
        "rows": result_rows,
        "units": query_response.get("units", {}),
        "metric_definition": query_response.get("metric_definition", {}),
        "eligible": query_response.get("eligible", 0),
        "observed": query_response.get("observed", 0),
        "missingness": query_response.get("missingness", {}),
        "coverage": query_response.get("coverage", {}),
        "pricing_fx": query_response.get("pricing_fx", {}),
        "pricing_assumptions": definition.pricing_assumptions,
        "worker_pricing_fx": worker_pricing,
        "warnings": query_response.get("warnings", []),
        "comparison": comparison,
    }
    root = _managed_root(managed_root)
    generated_records: List[ExportRecord] = []
    try:
        for export_format in chosen:
            export_id = uuid.uuid4().hex
            output_path = os.path.join(root, f"{export_id}.{export_format}")
            generator = _export_function(export_format)
            record = generator(report_data, output_path, managed_root=root)
            generated_records.append(record)

        with conn:
            for record in generated_records:
                record_export_inventory(conn, record)
            cursor = conn.execute(
                "UPDATE alerts_and_reports SET last_generated_at = ?, updated_at = ? WHERE item_id = ? AND item_type = 'report'",
                (generated_at, generated_at, definition.item_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Report definition not found: {definition.item_id}")
    except Exception:
        for record in generated_records:
            try:
                delete_snapshot_file(record.file_path, root)
            except Exception:
                # Preserve the generation failure; inventory is transactionally rolled back.
                pass
        raise

    coverage = query_response.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    return ReportExecutionResult(
        report_id=definition.item_id or "",
        generated_at=generated_at,
        exports=generated_records,
        coverage=coverage,
        query=query,
    )


def execute_report(
    conn: sqlite3.Connection,
    report_def_or_id: Union[ReportDefinition, str],
    formats: Optional[Sequence[str]] = None,
    managed_root: Optional[Union[str, os.PathLike[str]]] = None,
    pricing_fx: Optional[Mapping[str, Any]] = None,
    now_iso: Optional[str] = None,
) -> ReportExecutionResult:
    """Generate atomically and persist a safe, truthful last-attempt state."""
    definition = (get_report_definition(conn, report_def_or_id.item_id or "")
                  if isinstance(report_def_or_id, ReportDefinition)
                  else get_report_definition(conn, str(report_def_or_id)))
    if definition is None and isinstance(report_def_or_id, ReportDefinition):
        definition = save_report_definition(conn, report_def_or_id)
    if definition is None:
        raise KeyError("Report definition not found.")
    def record_state(status: str, error: Optional[Exception] = None) -> None:
        config = dict(definition.threshold_config)
        config["generation_state"] = {
            "status": status, "attempted_at": _iso_utc(_timestamp(now_iso, default_now=True)),
            "error_type": type(error).__name__ if error else None,
        }
        with conn:
            conn.execute("UPDATE alerts_and_reports SET threshold_config_json = ? WHERE item_id = ?",
                         (json.dumps(config), definition.item_id))
    try:
        result = _execute_report(conn, definition, formats, managed_root, pricing_fx, now_iso)
    except Exception as exc:
        record_state("failed", exc)
        raise
    record_state("completed")
    return result


def run_due_reports_scheduler(
    conn: sqlite3.Connection,
    managed_root: Optional[Union[str, os.PathLike[str]]] = None,
    pricing_fx: Optional[Mapping[str, Any]] = None,
    now_iso: Optional[str] = None,
) -> List[ReportExecutionResult]:
    """Run every due scheduled report locally; no network work is performed."""
    due_reports = get_due_reports(conn, now_iso)
    return [
        execute_report(
            conn,
            definition.item_id or "",
            managed_root=managed_root,
            pricing_fx=pricing_fx,
            now_iso=now_iso,
        )
        for definition in due_reports
    ]
