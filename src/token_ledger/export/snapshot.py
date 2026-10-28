"""Safe, bounded snapshots for local Token Ledger reports."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from token_ledger.privacy import redact_for_export, verify_path_within_root


MAX_EXPORT_BYTES = 2 * 1024 * 1024
MAX_EXPORT_ROWS = 1000
MAX_IMAGE_ROWS = 8
ALLOWED_EXPORT_FORMATS = frozenset({"csv", "json", "html", "pdf", "png"})

_PATH_VALUE_KEYS = frozenset({
    "path", "file_path", "source_path", "source_file", "file_locator",
    "source_locator", "working_directory", "cwd", "project_path", "log_path",
})
_PATH_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(?:~[/\\]|/(?:home|Users|tmp|private|var|mnt|workspace|root)/)[^\s\"'<>]+"
    r"|(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'<>]+"
)


class ExportSizeError(ValueError):
    """Raised when an export would exceed the configured hard size or row cap."""


@dataclass
class ExportRecord:
    export_id: str
    export_format: str
    file_path: str
    sha256_hash: str
    byte_size: int
    query_filter: Dict[str, Any]
    pricing_fx_assumptions: Dict[str, Any]
    redaction_applied: bool
    created_at: str
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    account_alias: str = "default"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe representation of the inventory row."""
        return asdict(self)


def _redact_paths(value: Any, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for raw_key, item in value.items():
            safe_key = str(raw_key)
            normalized_key = safe_key.casefold()
            if normalized_key in _PATH_VALUE_KEYS or normalized_key.endswith(("_path", "_locator")):
                cleaned[safe_key] = "[REDACTED_PATH]"
            elif normalized_key == "search" and item not in (None, "", [], {}):
                cleaned[safe_key] = "[REDACTED_FILTER_TEXT]"
            else:
                cleaned[safe_key] = _redact_paths(item, safe_key)
        return cleaned
    if isinstance(value, list):
        return [_redact_paths(item, key) for item in value]
    if isinstance(value, tuple):
        return [_redact_paths(item, key) for item in value]
    if isinstance(value, str):
        return _PATH_TEXT.sub("[REDACTED_PATH]", value)
    return value


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Report data must contain finite JSON-compatible values.") from exc
    encoded = rendered.encode("utf-8")
    if len(encoded) > MAX_EXPORT_BYTES:
        raise ExportSizeError(f"Report data exceeds the {MAX_EXPORT_BYTES}-byte export limit.")
    return encoded


def _prepare_data(report_data: Any) -> Dict[str, Any]:
    if isinstance(report_data, Mapping):
        payload = dict(report_data)
    elif isinstance(report_data, (list, tuple)):
        payload = {"rows": list(report_data)}
    else:
        raise TypeError("Report data must be an object or a list of rows.")

    safe_payload = _redact_paths(redact_for_export(payload))
    if not isinstance(safe_payload, dict):
        raise TypeError("Redacted report data must be an object.")
    rows = safe_payload.get("rows", [])
    if rows is None:
        rows = []
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise TypeError("Report rows must be a list of objects.")
    if len(rows) > MAX_EXPORT_ROWS:
        raise ExportSizeError(f"Report contains more than the {MAX_EXPORT_ROWS}-row export limit.")
    safe_payload["rows"] = [dict(row) for row in rows]
    _json_bytes(safe_payload)
    return safe_payload


def _managed_output_path(output_path: str, managed_root: Optional[str]) -> Tuple[str, str]:
    target = os.path.abspath(os.fspath(output_path))
    root = os.path.abspath(os.fspath(managed_root)) if managed_root is not None else os.path.dirname(target)
    if not root:
        raise ValueError("An output path must have a parent managed directory.")
    os.makedirs(root, mode=0o700, exist_ok=True)
    # The privacy helper performs descriptor-relative, no-follow checks. The target
    # parent must already exist, so exporters never create directories through a path.
    safe_target = verify_path_within_root(target, root)
    return safe_target, os.path.realpath(root)


def _write_bytes(output_path: str, content: bytes, managed_root: Optional[str]) -> Tuple[str, str, int]:
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportSizeError(f"Export exceeds the {MAX_EXPORT_BYTES}-byte file limit.")
    target, root = _managed_output_path(output_path, managed_root)
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".token-ledger-export-",
            suffix=".tmp",
            dir=os.path.dirname(target),
        )
        verify_path_within_root(temporary_path, root)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        verify_path_within_root(target, root)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    return target, hashlib.sha256(content).hexdigest(), len(content)


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return str(value)


def _csv_safe(value: Any) -> str:
    text = _format_value(value)
    significant = text.lstrip(" \t\r\n\v\f")
    if significant.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _result_rows(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = data.get("rows") or []
    if rows:
        return [dict(row) for row in rows]
    values = data.get("values", data.get("summary", {}))
    if isinstance(values, Mapping) and values:
        return [{"metric": key, "value": value} for key, value in values.items()]
    return []


def _columns(rows: Sequence[Mapping[str, Any]], requested: Optional[Sequence[str]]) -> List[str]:
    if requested is not None:
        if isinstance(requested, (str, bytes)) or any(not isinstance(column, str) for column in requested):
            raise ValueError("CSV columns must be a sequence of strings.")
        return list(dict.fromkeys(requested))
    found: Dict[str, None] = {}
    for row in rows:
        for name in row:
            found.setdefault(str(name), None)
    return list(found)


def _scope(data: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str], str]:
    query = data.get("query") if isinstance(data.get("query"), Mapping) else {}
    filters = query.get("filters") if isinstance(query.get("filters"), Mapping) else {}

    def one_value(value: Any) -> Optional[str]:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        return None

    session_id = data.get("session_id") or one_value(filters.get("session"))
    project_id = data.get("project_id") or one_value(filters.get("project"))
    account_alias = data.get("account_alias") or one_value(filters.get("account_alias")) or "default"
    return session_id, project_id, str(account_alias)


def _record(
    data: Mapping[str, Any],
    export_format: str,
    file_path: str,
    digest: str,
    byte_size: int,
) -> ExportRecord:
    safe_data = _prepare_data(data)
    created_at = safe_data.get("generated_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    session_id, project_id, account_alias = _scope(safe_data)
    query_filter = safe_data.get("query", {})
    if not isinstance(query_filter, dict):
        query_filter = {}
    pricing = {
        "query_result": safe_data.get("pricing_fx", {}),
        "report_assumptions": safe_data.get("pricing_assumptions", {}),
        "worker_assumptions": safe_data.get("worker_pricing_fx", {}),
    }
    pricing = _redact_paths(redact_for_export(pricing))
    return ExportRecord(
        export_id=uuid.uuid4().hex,
        export_format=export_format,
        file_path=file_path,
        sha256_hash=digest,
        byte_size=byte_size,
        query_filter=query_filter,
        pricing_fx_assumptions=pricing,
        redaction_applied=True,
        created_at=created_at,
        session_id=session_id,
        project_id=project_id,
        account_alias=account_alias,
    )


def generate_csv_export(
    dataset: Any,
    output_path: str,
    columns: Optional[Sequence[str]] = None,
    formula_safe: bool = True,
    *,
    managed_root: Optional[str] = None,
) -> ExportRecord:
    """Write redacted CSV rows with spreadsheet formula injection protection."""
    if not formula_safe:
        raise ValueError("Formula protection is mandatory for CSV exports.")
    data = _prepare_data(dataset)
    rows = _result_rows(data)
    column_names = _columns(rows, columns)
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    if column_names:
        writer.writerow([_csv_safe(name) for name in column_names])
        for row in rows:
            writer.writerow([_csv_safe(row.get(name)) for name in column_names])
    if data.get("comparison"):
        writer.writerow([])
        writer.writerow(["report_metadata", "value"])
        for key in ("comparison", "coverage", "missingness", "pricing_fx", "pricing_assumptions"):
            writer.writerow([key, _csv_safe(data.get(key))])
    payload = text.getvalue().encode("utf-8")
    path, digest, size = _write_bytes(output_path, payload, managed_root)
    return _record(data, "csv", path, digest, size)


def generate_json_export(
    report_data: Any,
    output_path: str,
    redact_secrets: bool = True,
    *,
    managed_root: Optional[str] = None,
) -> ExportRecord:
    """Write the complete report envelope after mandatory privacy redaction."""
    if not redact_secrets:
        raise ValueError("Privacy redaction is mandatory for JSON exports.")
    data = _prepare_data(report_data)
    payload = _json_bytes(data, pretty=True)
    path, digest, size = _write_bytes(output_path, payload, managed_root)
    return _record(data, "json", path, digest, size)


def _display(value: Any) -> str:
    return _format_value(value)


def _html_text(value: Any) -> str:
    return html.escape(_display(value), quote=True)


def _chart_points(data: Mapping[str, Any]) -> Tuple[str, List[Tuple[str, float]]]:
    rows = data.get("rows") or []
    if rows:
        if not rows:
            return "", []
        preferred = ("total_tokens", "api_equivalent_usd", "requests_count", "count", "value")
        numeric_key = next(
            (key for key in preferred if any(isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool) for row in rows)),
            None,
        )
        if numeric_key is None:
            numeric_key = next(
                (
                    key
                    for row in rows
                    for key, value in row.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                ),
                None,
            )
        if numeric_key is None:
            return "", []
        label_key = next(
            (key for key in ("bucket", "period", "date", "model", "provider", "project_name", "group_key") if any(row.get(key) is not None for row in rows)),
            None,
        )
        points = []
        for index, row in enumerate(rows[:24]):
            value = row.get(numeric_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                label = _display(row.get(label_key)) if label_key else str(index + 1)
                points.append((label, float(value)))
        return str(numeric_key), points

    summary = data.get("values", data.get("summary", {}))
    if not isinstance(summary, Mapping):
        return "", []
    for key in ("total_tokens", "api_equivalent_usd", "requests_count", "request_count", "count"):
        value = summary.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return str(key), [(str(key), float(value))]
    for key, value in summary.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return str(key), [(str(key), float(value))]
    return "", []


def _svg_chart(data: Mapping[str, Any]) -> str:
    metric, points = _chart_points(data)
    if not points:
        return '<p class="empty-chart">No numeric values were returned for this query.</p>'
    width, height = 860, 240
    left, right, top, bottom = 58, 16, 18, 62
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum = max(max(abs(value) for _, value in points), 1.0)
    step = plot_width / len(points)
    baseline = top + plot_height / 2 if any(value < 0 for _, value in points) else top + plot_height
    zero_to_top = baseline - top
    zero_to_bottom = top + plot_height - baseline
    pieces = [
        f'<svg class="report-chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(metric, quote=True)} chart">',
        f'<text class="axis-title" x="{left}" y="14">{html.escape(metric)}</text>',
        f'<line class="axis" x1="{left}" y1="{baseline:.2f}" x2="{width-right}" y2="{baseline:.2f}" />',
    ]
    for index, (label, value) in enumerate(points):
        bar_height = abs(value) / maximum * min(zero_to_top, zero_to_bottom if any(v < 0 for _, v in points) else plot_height)
        x = left + index * step + step * 0.16
        bar_width = max(step * 0.68, 1.0)
        y = baseline - bar_height if value >= 0 else baseline
        pieces.append(f'<rect class="bar" x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{bar_height:.2f}" rx="3">')
        pieces.append(f'<title>{html.escape(label)}: {value:g}</title></rect>')
        pieces.append(f'<text class="bar-label" x="{x + bar_width / 2:.2f}" y="{height-bottom+17}" text-anchor="middle">{html.escape(label[:20])}</text>')
        pieces.append(f'<text class="bar-value" x="{x + bar_width / 2:.2f}" y="{max(top+12, y-5):.2f}" text-anchor="middle">{value:g}</text>')
    pieces.append("</svg>")
    return "".join(pieces)


def _summary_rows(data: Mapping[str, Any]) -> List[Tuple[str, Any]]:
    summary = data.get("values", data.get("summary", {}))
    if isinstance(summary, Mapping):
        return [(str(key), value) for key, value in summary.items()]
    return []


def _filter_summary(data: Mapping[str, Any]) -> Dict[str, Any]:
    query = data.get("query") if isinstance(data.get("query"), Mapping) else {}
    keys = ("contract_version", "family", "metric_id", "harness_scope", "time_range", "filters", "group_by", "pagination")
    return {key: query[key] for key in keys if key in query}


def _table_columns(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    found: Dict[str, None] = {}
    for row in rows:
        for key in row:
            found.setdefault(str(key), None)
    return list(found)


def _html_document(data: Mapping[str, Any]) -> bytes:
    detail_rows = data.get("rows") or []
    rows = [dict(row) for row in detail_rows] if detail_rows else _result_rows(data)
    is_summary = not detail_rows and bool(rows)
    columns = _table_columns(rows)
    if not columns:
        columns = []
    table_head = "".join(f"<th scope=\"col\">{_html_text(column)}</th>" for column in columns)
    table_body = "".join(
        "<tr>" + "".join(f"<td>{_html_text(row.get(column))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    observed = data.get("observed")
    row_count = len(detail_rows)
    if isinstance(observed, int) and observed >= 0:
        row_summary = f"{row_count} returned rows of {observed} observed"
    else:
        row_summary = f"{row_count} returned rows"
    if is_summary:
        row_summary += "; table shows aggregate summary values, not detail rows"
    elif not detail_rows:
        row_summary += "; the query returned no detail rows"
    elif isinstance(observed, int) and observed > row_count:
        row_summary += f"; showing the query's first {row_count} rows"

    title = data.get("title") or "Token Ledger report"
    summary = _summary_rows(data)
    summary_html = "".join(
        f'<div class="summary-value"><span>{_html_text(key)}</span><strong>{_html_text(value)}</strong></div>'
        for key, value in summary
    ) or '<p class="empty">No summary values were returned.</p>'
    filters = _filter_summary(data)
    filter_text = json.dumps(filters, ensure_ascii=False, allow_nan=False, indent=2) if filters else "No query filters were applied."
    coverage = data.get("coverage", {})
    pricing = data.get("pricing_fx", {})
    warnings = data.get("warnings") or []
    warning_html = "".join(f"<li>{_html_text(item)}</li>" for item in warnings)
    document = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_text(title)}</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 36px clamp(18px, 5vw, 64px); background: #0f172a; color: #e2e8f0; line-height: 1.55; }}
main {{ max-width: 1180px; margin: 0 auto; }}
h1 {{ margin: 0 0 4px; color: #f8fafc; font-size: clamp(1.8rem, 3vw, 2.6rem); letter-spacing: -0.025em; line-height: 1.15; overflow-wrap: anywhere; }}
h2 {{ margin: 38px 0 12px; color: #f8fafc; font-size: 1.15rem; letter-spacing: -0.01em; }}
.muted, .section-note {{ color: #94a3b8; }}
.generated {{ margin: 0 0 26px; color: #94a3b8; font-size: .9rem; }}
.summary {{ display: flex; flex-wrap: wrap; gap: 12px 30px; padding: 18px 0; border-top: 1px solid #334155; border-bottom: 1px solid #334155; }}
.summary-value {{ min-width: 150px; display: grid; gap: 3px; }}
.summary-value span {{ color: #94a3b8; font-size: .82rem; }}
.summary-value strong {{ color: #f8fafc; font-size: 1.1rem; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }}
.chart-wrap {{ overflow-x: auto; padding: 12px 0; }}
.report-chart {{ display: block; width: 100%; min-width: 560px; height: auto; }}
.report-chart .axis {{ stroke: #475569; stroke-width: 1; }}
.report-chart .axis-title {{ fill: #94a3b8; font-size: 12px; }}
.report-chart .bar {{ fill: #38bdf8; }}
.report-chart .bar-label {{ fill: #cbd5e1; font-size: 11px; }}
.report-chart .bar-value {{ fill: #f8fafc; font-size: 11px; }}
.empty-chart, .empty {{ color: #94a3b8; padding: 18px 0; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #334155; border-radius: 12px; }}
table {{ width: 100%; border-collapse: collapse; font-size: .88rem; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #263548; vertical-align: top; overflow-wrap: anywhere; }}
th {{ position: sticky; top: 0; background: #1e293b; color: #cbd5e1; font-weight: 600; }}
td {{ color: #e2e8f0; }}
tr:last-child td {{ border-bottom: 0; }}
pre {{ max-width: 100%; margin: 0; padding: 16px; overflow: auto; border-radius: 10px; background: #1e293b; color: #cbd5e1; font: .82rem/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }}
ul {{ color: #cbd5e1; }}
@media print {{ :root, body {{ background: #fff; color: #0f172a; }} h1, h2, .summary-value strong {{ color: #0f172a; }} .generated, .muted, .section-note, .summary-value span {{ color: #475569; }} }}
</style>
</head>
<body><main>
<header><h1>{_html_text(title)}</h1><p class="generated">Generated {_html_text(data.get("generated_at", "unknown time"))}</p></header>
<section aria-labelledby="summary-heading"><h2 id="summary-heading">Summary</h2><div class="summary">{summary_html}</div></section>
<section aria-labelledby="chart-heading"><h2 id="chart-heading">Chart</h2><div class="chart-wrap">{_svg_chart(data)}</div></section>
<section aria-labelledby="rows-heading"><h2 id="rows-heading">Report rows</h2><p class="section-note">{_html_text(row_summary)}</p><div class="table-wrap"><table><thead><tr>{table_head}</tr></thead><tbody>{table_body}</tbody></table></div></section>
<section aria-labelledby="filters-heading"><h2 id="filters-heading">Query filters</h2><pre>{html.escape(filter_text, quote=True)}</pre></section>
<section aria-labelledby="coverage-heading"><h2 id="coverage-heading">Coverage and pricing provenance</h2><p class="muted">Coverage</p><pre>{_html_text(coverage)}</pre><p class="muted">Pricing and FX used by the shared query</p><pre>{_html_text(pricing)}</pre></section>
<section aria-labelledby="comparison-heading"><h2 id="comparison-heading">Equivalent prior-period comparison</h2><pre>{_html_text(data.get("comparison") or "Not scheduled; no automatic comparison.")}</pre></section>
<section aria-labelledby="warnings-heading"><h2 id="warnings-heading">Warnings</h2>{f'<ul>{warning_html}</ul>' if warning_html else '<p class="empty">No query warnings.</p>'}</section>
</main></body></html>'''
    encoded = document.encode("utf-8")
    if len(encoded) > MAX_EXPORT_BYTES:
        raise ExportSizeError(f"HTML export exceeds the {MAX_EXPORT_BYTES}-byte file limit.")
    return encoded


def generate_html_export(
    report_data: Any,
    output_path: str,
    *,
    managed_root: Optional[str] = None,
) -> ExportRecord:
    """Write a standalone offline report with escaped text and inline SVG/CSS."""
    data = _prepare_data(report_data)
    payload = _html_document(data)
    path, digest, size = _write_bytes(output_path, payload, managed_root)
    return _record(data, "html", path, digest, size)


def _image_rows(data: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    details = data.get("rows") or []
    if details:
        return [dict(row) for row in details[:MAX_IMAGE_ROWS]], len(details) > MAX_IMAGE_ROWS
    return _result_rows(data), False


def _render_image(data: Mapping[str, Any], fmt: str) -> bytes:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.backends.backend_pdf import FigureCanvasPdf
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise RuntimeError("Local matplotlib is required to generate real PNG and PDF exports.") from exc

    background = "#0f172a"
    foreground = "#e2e8f0"
    subdued = "#94a3b8"
    figure = Figure(figsize=(12, 8), dpi=140, facecolor=background)
    canvas = FigureCanvasPdf(figure) if fmt == "pdf" else FigureCanvasAgg(figure)
    title = str(data.get("title") or "Token Ledger report")[:120]
    figure.text(0.06, 0.94, title, color="#f8fafc", fontsize=19, weight="bold", va="top")
    generated = str(data.get("generated_at", "unknown time"))
    figure.text(0.06, 0.89, f"Generated {generated[:100]}", color=subdued, fontsize=9, va="top")

    filters = _filter_summary(data)
    filter_text = json.dumps(filters, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if filters else "No query filters"
    filter_was_clipped = len(filter_text) > 280
    figure.text(0.06, 0.84, f"Filters: {filter_text[:280]}{'…' if filter_was_clipped else ''}", color=subdued, fontsize=8, va="top", wrap=True)

    summary = _summary_rows(data)
    summary_lines = [f"{key}: {_display(value)}" for key, value in summary[:8]]
    if len(summary) > 8:
        summary_lines.append(f"{len(summary) - 8} additional summary values are in the JSON/HTML export")
    figure.text(0.06, 0.76, "  ·  ".join(summary_lines)[:500] or "No summary values returned.", color=foreground, fontsize=9, va="top", wrap=True)

    metric, points = _chart_points(data)
    chart = figure.add_axes([0.08, 0.42, 0.84, 0.27], facecolor=background)
    if points:
        chart.bar(range(len(points)), [value for _, value in points], color="#38bdf8", width=0.66)
        chart.set_title(f"{metric} · values returned by the query", color=foreground, fontsize=10, loc="left", pad=9)
        chart.set_xticks(range(len(points)))
        chart.set_xticklabels([label[:16] for label, _ in points], color=subdued, fontsize=7, rotation=24, ha="right")
        chart.tick_params(axis="y", colors=subdued, labelsize=8)
        chart.grid(axis="y", color="#334155", linewidth=0.55, alpha=0.8)
        chart.set_axisbelow(True)
        for spine in chart.spines.values():
            spine.set_color("#334155")
    else:
        chart.set_axis_off()
        chart.text(0.5, 0.5, "No numeric query values to chart", color=subdued, ha="center", va="center", transform=chart.transAxes)

    rows, omitted = _image_rows(data)
    comparison = data.get("comparison")
    if isinstance(comparison, Mapping) and "period_a" in comparison and "period_b" in comparison:
        prior = comparison["period_a"]["metrics"]
        current = comparison["period_b"]["metrics"]
        rows = [
            {"metric": key, "prior": prior.get(key), "current": current.get(key),
             "delta": comparison["deltas"][key]["delta"],
             "coverage A/B": f'{prior["coverage"].get(key, "observed")}/{current["coverage"].get(key, "observed")}'}
            for key in ("requests_count", "total_tokens", "cache_hit_rate", "api_equivalent_usd", "duration_ms_mean", "retry_rate")
        ]
        rows += [
            {"metric": "Periods A/B", "prior": comparison["period_a"]["start"], "current": comparison["period_b"]["start"],
             "delta": "Half-open calendar", "coverage A/B": comparison["matched_status"]},
            {"metric": "Assumptions", "prior": "Missing is not zero", "current": "No causal claim",
             "delta": "Month clamp; DST", "coverage A/B": "Collection gaps unknown"},
        ]
        omitted = True
    elif comparison == "unavailable" or (isinstance(comparison, Mapping) and comparison.get("status") == "unavailable"):
        rows = [
            {"metric": "Equivalent prior-period comparison", "prior": "unavailable", "current": "observed",
             "delta": "unavailable", "coverage A/B": "unavailable"}
        ]
        omitted = True
    columns = _table_columns(rows)
    visible_columns = columns[:6]
    table_rows = []
    for row in rows:
        table_rows.append([_display(row.get(column))[:70] for column in visible_columns])
    if not table_rows:
        table_rows = [["No detail rows returned"]]
        visible_columns = ["Report rows"]
    elif len(columns) > len(visible_columns):
        omitted = True
    table_ax = figure.add_axes([0.05, 0.07, 0.9, 0.26])
    table_ax.set_axis_off()
    table = table_ax.table(
        cellText=table_rows,
        colLabels=[column[:32] for column in visible_columns],
        loc="center",
        cellLoc="left",
        colLoc="left",
        bbox=[0, 0, 1, 1],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(7)
    for (row_index, _), cell in table.get_celld().items():
        cell.set_edgecolor("#334155")
        cell.set_facecolor("#1e293b" if row_index == 0 else background)
        cell.get_text().set_color("#cbd5e1" if row_index == 0 else foreground)
    returned = len(data.get("rows") or [])
    preview_note = f"Preview: {len(rows)} of {returned} returned detail rows; use the CSV, JSON, or HTML export for complete data."
    if not returned and _summary_rows(data):
        preview_note = "No detail rows returned; the table shows aggregate summary values."
    if omitted:
        preview_note += " Some rows or columns are omitted from this fixed-size visual preview."
    figure.text(0.06, 0.035, preview_note, color=subdued, fontsize=7, va="bottom")

    buffer = io.BytesIO()
    if fmt == "pdf":
        canvas.print_pdf(buffer, metadata={"Subject": _format_value({"comparison": data.get("comparison"), "coverage": data.get("coverage"), "missingness": data.get("missingness")})})
    else:
        canvas.print_png(buffer, metadata={"Description": _format_value({"comparison": data.get("comparison"), "coverage": data.get("coverage"), "missingness": data.get("missingness")})})
    payload = buffer.getvalue()
    if len(payload) > MAX_EXPORT_BYTES:
        raise ExportSizeError(f"{fmt.upper()} export exceeds the {MAX_EXPORT_BYTES}-byte file limit.")
    return payload


def generate_pdf_export(
    report_data: Any,
    output_path: str,
    *,
    managed_root: Optional[str] = None,
) -> ExportRecord:
    """Render a genuine local PDF preview with summary, chart, filters, and rows."""
    data = _prepare_data(report_data)
    payload = _render_image(data, "pdf")
    path, digest, size = _write_bytes(output_path, payload, managed_root)
    return _record(data, "pdf", path, digest, size)


def generate_png_export(
    report_data: Any,
    output_path: str,
    *,
    managed_root: Optional[str] = None,
) -> ExportRecord:
    """Render a genuine local PNG preview with summary, chart, filters, and rows."""
    data = _prepare_data(report_data)
    payload = _render_image(data, "png")
    path, digest, size = _write_bytes(output_path, payload, managed_root)
    return _record(data, "png", path, digest, size)


def _record_from_row(row: sqlite3.Row) -> ExportRecord:
    def load_json(name: str) -> Dict[str, Any]:
        try:
            value = json.loads(row[name] or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    return ExportRecord(
        export_id=row["export_id"],
        export_format=row["export_format"],
        file_path=row["file_path"],
        sha256_hash=row["sha256_hash"],
        byte_size=row["byte_size"],
        query_filter=load_json("query_filter_json"),
        pricing_fx_assumptions=load_json("pricing_fx_assumptions_json"),
        redaction_applied=bool(row["redaction_applied"]),
        created_at=row["created_at"],
        session_id=row["session_id"],
        project_id=row["project_id"],
        account_alias=row["account_alias"],
    )


def record_export_inventory(conn: sqlite3.Connection, record: ExportRecord) -> None:
    """Persist a redacted export's identity, digest, size, and retention scope."""
    if not record.redaction_applied:
        raise ValueError("Unredacted exports cannot be added to inventory.")
    if not re.fullmatch(r"[0-9a-f]{64}", record.sha256_hash):
        raise ValueError("Export inventory requires a lowercase SHA-256 digest.")
    if record.byte_size < 0 or record.byte_size > MAX_EXPORT_BYTES:
        raise ValueError("Export inventory byte size is outside the permitted range.")
    safe_filter = _redact_paths(redact_for_export(record.query_filter))
    safe_pricing = _redact_paths(redact_for_export(record.pricing_fx_assumptions))
    conn.execute(
        """
        INSERT INTO export_inventory (
            export_id, export_format, file_path, query_filter_json,
            pricing_fx_assumptions_json, redaction_applied, created_at,
            session_id, project_id, account_alias, sha256_hash, byte_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.export_id,
            record.export_format,
            record.file_path,
            json.dumps(safe_filter, ensure_ascii=False, separators=(",", ":")),
            json.dumps(safe_pricing, ensure_ascii=False, separators=(",", ":")),
            1,
            record.created_at,
            record.session_id,
            record.project_id,
            record.account_alias,
            record.sha256_hash,
            record.byte_size,
        ),
    )


def list_exports(
    conn: sqlite3.Connection,
    export_format: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 100,
    account_alias: Optional[str] = None,
) -> List[ExportRecord]:
    """List recent local exports, optionally scoped by format, project, and account."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Export inventory limit must be between 1 and 1000.")
    clauses: List[str] = []
    parameters: List[Any] = []
    if export_format is not None:
        if export_format not in ALLOWED_EXPORT_FORMATS:
            raise ValueError(f"Unsupported export format: {export_format}")
        clauses.append("export_format = ?")
        parameters.append(export_format)
    if project_id is not None:
        clauses.append("project_id = ?")
        parameters.append(project_id)
    if account_alias is not None:
        clauses.append("account_alias = ?")
        parameters.append(account_alias)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    cursor = conn.execute(
        "SELECT * FROM export_inventory" + where + " ORDER BY created_at DESC, export_id LIMIT ?",
        (*parameters, limit),
    )
    return [_record_from_row(row) for row in cursor.fetchall()]


def get_export_by_id(conn: sqlite3.Connection, export_id: str) -> Optional[ExportRecord]:
    """Retrieve one export inventory record by its generated identifier."""
    row = conn.execute("SELECT * FROM export_inventory WHERE export_id = ?", (export_id,)).fetchone()
    return _record_from_row(row) if row is not None else None
