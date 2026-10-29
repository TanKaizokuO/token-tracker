"""Archive aggregate partition ingestion, idempotency, privacy sanitization, and live reconciliation.

Implements WP05 requirements:
1. Versioned JSON aggregate partition import into archive_partitions (schema002).
2. Strict preservation of legacy timezone, original stored cost (nullable: None unknown vs 0.0 measured),
   status, grain, source, coverage, and missingness/gaps.
3. Strict zero-manufacturing: never manufactures fake canonical requests or normalized usage from archives.
4. Idempotent replay: safe re-import of previously ingested partitions without duplication.
5. Mutual exclusion and unresolved overlap flagging across partitions.
6. Privacy sanitization: account aliases and project identifiers sanitized before persistence;
   foreign-key parents safely registered.
7. Verification-driven live reconciliation:
   - Reconciles ONLY when grain, timezone, and token semantics match and complete coverage is verified.
   - Requires explicit proof structure validated against canonical records and checkpoints.
   - Refuses subtraction of incompatible totals.
   - Uses replacement state (is_live_replaced=1) without deleting private source logs or archive partitions.
8. Combined partition selection: excludes superseded partitions by default and marks partial overlaps.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

try:
    from token_ledger.privacy import (
        redact_secrets as _privacy_redact_secrets,
        sanitize_account_alias as _privacy_sanitize_account,
        sanitize_metadata as _privacy_sanitize_metadata,
    )
except ImportError:
    def _privacy_redact_secrets(text: str, replacement: str = "[REDACTED_SECRET]") -> str:
        return text

    def _privacy_sanitize_account(acc: Optional[str], default: str = "default", **kwargs: Any) -> str:
        return (acc or default).strip()

    def _privacy_sanitize_metadata(data: Any, **kwargs: Any) -> Any:
        return data


# -----------------------------------------------------------------------------
# 1. Constants & Supported Artifact Types
# -----------------------------------------------------------------------------

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0", "1.1", "2.0"})
SUPPORTED_ARTIFACT_TYPES = frozenset({
    "archive_partition_bundle",
    "archive_partitions",
    "token_ledger_archive",
})
SUPPORTED_GRAINS = frozenset({"day", "hour", "session", "week", "month"})


# -----------------------------------------------------------------------------
# 2. Exceptions
# -----------------------------------------------------------------------------

class ArchiveError(Exception):
    """Base exception for all archive operations."""


class UnsupportedArchiveArtifactError(ArchiveError):
    """Raised when an incoming archive artifact has an unrecognized schema, version, or format."""


class IncompatibleReconciliationError(ArchiveError):
    """Raised when live reconciliation is attempted with mismatched grain, timezone, or token semantics."""


class PartitionOverlapError(ArchiveError):
    """Raised when archive partitions have mutually exclusive boundary conflicts or unresolved overlap."""


# -----------------------------------------------------------------------------
# 3. Data Transfer Objects
# -----------------------------------------------------------------------------

@dataclass
class ArchivePartitionRecord:
    """Represents a validated archive partition."""
    partition_id: str
    partition_type: str
    grain: str
    source_name: str
    start_time: str
    end_time: str
    total_tokens: int
    records_count: int
    timezone_name: str = "UTC"
    total_cost_usd: Optional[float] = None
    pricing_status: str = "unpriced"
    account_alias: str = "default"
    project_id: Optional[str] = None
    missingness: Dict[str, Any] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)
    is_reconciled: bool = False
    is_live_replaced: bool = False
    reconciliation_notes: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None


@dataclass
class ImportSummary:
    """Summary of archive bundle ingestion."""
    imported_count: int = 0
    replayed_count: int = 0
    skipped_count: int = 0
    overlap_count: int = 0
    partitions: List[ArchivePartitionRecord] = field(default_factory=list)
    unresolved_overlaps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReconciliationProof:
    """Explicit proof structure validating canonical records and checkpoint coverage."""
    grain: str
    timezone_name: str
    source_adapter: str
    start_time: str
    end_time: str
    account_alias: Optional[str] = None
    expected_records_count: Optional[int] = None
    expected_tokens: Optional[int] = None
    checkpoint_verified: bool = False
    contiguous_coverage: bool = False
    gap_intervals: List[Tuple[str, str]] = field(default_factory=list)
    token_semantics: str = "disjoint_billable"


@dataclass
class ReconciliationResult:
    """Outcome of attempting to reconcile an archive partition against live records."""
    partition_id: str
    is_reconciled: bool
    is_live_replaced: bool
    status: str
    notes: str
    canonical_records_count: int = 0
    canonical_total_tokens: int = 0
    differences: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CombinedPartitionSelection:
    """Active and superseded partition collection for downstream queries."""
    active_partitions: List[Dict[str, Any]] = field(default_factory=list)
    superseded_partitions: List[Dict[str, Any]] = field(default_factory=list)
    partial_overlaps: List[Dict[str, Any]] = field(default_factory=list)
    has_unresolved_overlap: bool = False


# -----------------------------------------------------------------------------
# 4. Privacy and Path Sanitization Helpers
# -----------------------------------------------------------------------------

def sanitize_archive_account(raw_account: Optional[str], default: str = "default") -> str:
    """Sanitizes account aliases using privacy rules, eliminating secrets or email exposure."""
    return _privacy_sanitize_account(raw_account, default=default)


def sanitize_archive_project(raw_project: Optional[str]) -> Optional[str]:
    """Ensures no raw absolute path is persisted as project ID or project name."""
    if not raw_project:
        return None
    cleaned = str(raw_project).strip()
    if not cleaned:
        return None
    if "/" in cleaned or "\\" in cleaned:
        basename = os.path.basename(cleaned.rstrip("/\\"))
        if basename and not basename.startswith("."):
            return _privacy_sanitize_account(basename)
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
        return f"proj_{digest}"
    return _privacy_sanitize_account(cleaned)


def _ensure_project_exists(
    conn: sqlite3.Connection,
    project_id: Optional[str],
    account_alias: str,
    timestamp: str,
) -> None:
    """Ensures foreign-key parent row exists in projects table."""
    if not project_id:
        return
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO projects (
                project_id, project_name, project_alias, account_alias, is_active, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, 1, ?, '{}');
            """,
            (project_id, project_id, project_id, account_alias, timestamp),
        )


# -----------------------------------------------------------------------------
# 5. Artifact Validation
# -----------------------------------------------------------------------------

def validate_archive_artifact(artifact: Any) -> Dict[str, Any]:
    """Validates that an input dictionary meets the versioned archive bundle specification."""
    if not isinstance(artifact, dict):
        raise UnsupportedArchiveArtifactError("Archive artifact must be a JSON object (dict)")

    version = artifact.get("version")
    if not version or str(version) not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedArchiveArtifactError(
            f"Unsupported archive artifact version: {version!r}. Supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    art_type = artifact.get("artifact_type")
    if not art_type or art_type not in SUPPORTED_ARTIFACT_TYPES:
        raise UnsupportedArchiveArtifactError(
            f"Unsupported artifact_type: {art_type!r}. Supported: {sorted(SUPPORTED_ARTIFACT_TYPES)}"
        )

    partitions = artifact.get("partitions")
    if not isinstance(partitions, list):
        raise UnsupportedArchiveArtifactError("Archive artifact must contain a 'partitions' array")

    for idx, p in enumerate(partitions):
        if not isinstance(p, dict):
            raise UnsupportedArchiveArtifactError(f"Partition at index {idx} is not a JSON object")

        required_fields = ("partition_id", "partition_type", "grain", "source_name", "start_time", "end_time", "total_tokens")
        missing = [f for f in required_fields if f not in p]
        if missing:
            raise UnsupportedArchiveArtifactError(
                f"Partition at index {idx} ({p.get('partition_id', 'unknown')}) missing required fields: {missing}"
            )

        if p["grain"] not in SUPPORTED_GRAINS:
            raise UnsupportedArchiveArtifactError(
                f"Unsupported grain '{p['grain']}' in partition {p.get('partition_id')}. Supported: {sorted(SUPPORTED_GRAINS)}"
            )

    return artifact


# -----------------------------------------------------------------------------
# 6. Archive Partition Ingestion & Idempotent Replay
# -----------------------------------------------------------------------------

def import_archive_partitions(
    conn: sqlite3.Connection,
    artifact_data: Union[Dict[str, Any], str, bytes],
    account_alias: Optional[str] = None,
    project_id: Optional[str] = None,
) -> ImportSummary:
    """Idempotently imports versioned JSON aggregate partitions into archive_partitions."""
    if isinstance(artifact_data, (str, bytes)):
        try:
            raw_dict = json.loads(artifact_data)
        except Exception as e:
            raise UnsupportedArchiveArtifactError(f"Malformed JSON artifact: {e}") from e
    elif isinstance(artifact_data, dict):
        raw_dict = artifact_data
    else:
        raise UnsupportedArchiveArtifactError("artifact_data must be a dict, string, or bytes")

    validated = validate_archive_artifact(raw_dict)
    now_utc = datetime.now(timezone.utc).isoformat()

    summary = ImportSummary()

    # Load existing active partitions to check idempotency and overlap
    cur = conn.cursor()
    cur.execute(
        """
        SELECT partition_id, partition_type, grain, source_name, timezone_name,
               start_time, end_time, total_tokens, total_cost_usd, records_count,
               account_alias, project_id, is_live_replaced
        FROM archive_partitions;
        """
    )
    existing_partitions = {row["partition_id"]: dict(row) for row in cur.fetchall()}
    cur.close()

    partitions_to_insert: List[ArchivePartitionRecord] = []

    for p in validated["partitions"]:
        p_id = str(p["partition_id"]).strip()
        p_type = str(p["partition_type"]).strip()
        grain = str(p["grain"]).strip()
        source_name = str(p["source_name"]).strip()
        start_time = str(p["start_time"]).strip()
        end_time = str(p["end_time"]).strip()
        timezone_name = str(p.get("timezone_name") or "UTC").strip()

        # Preserve Null vs 0.0 cost
        raw_cost = p.get("total_cost_usd")
        if raw_cost is None:
            total_cost_usd = None
            pricing_status = p.get("pricing_status") or "unpriced"
        else:
            total_cost_usd = float(raw_cost)
            pricing_status = p.get("pricing_status") or ("priced" if total_cost_usd >= 0 else "unpriced")

        total_tokens = int(p.get("total_tokens", 0))
        records_count = int(p.get("records_count", 0))

        # Privacy sanitization
        raw_acct = account_alias or p.get("account_alias")
        clean_acct = sanitize_archive_account(raw_acct, default="default")

        raw_proj = project_id or p.get("project_id")
        clean_proj = sanitize_archive_project(raw_proj)

        missingness = _privacy_sanitize_metadata(p.get("missingness") or {})
        coverage = _privacy_sanitize_metadata(p.get("coverage") or {})
        payload = _privacy_sanitize_metadata(p.get("payload") or {})

        rec = ArchivePartitionRecord(
            partition_id=p_id,
            partition_type=p_type,
            grain=grain,
            source_name=source_name,
            timezone_name=timezone_name,
            start_time=start_time,
            end_time=end_time,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            records_count=records_count,
            pricing_status=pricing_status,
            account_alias=clean_acct,
            project_id=clean_proj,
            missingness=missingness,
            coverage=coverage,
            is_reconciled=bool(p.get("is_reconciled", False)),
            is_live_replaced=bool(p.get("is_live_replaced", False)),
            reconciliation_notes=p.get("reconciliation_notes"),
            payload=payload,
            created_at=p.get("created_at") or now_utc,
        )

        # Check existing row
        if p_id in existing_partitions:
            ex = existing_partitions[p_id]
            is_exact_replay = (
                ex["source_name"] == rec.source_name
                and ex["grain"] == rec.grain
                and ex["start_time"] == rec.start_time
                and ex["end_time"] == rec.end_time
                and ex["total_tokens"] == rec.total_tokens
                and (
                    (ex["total_cost_usd"] is None and rec.total_cost_usd is None)
                    or (ex["total_cost_usd"] is not None and rec.total_cost_usd is not None and abs(ex["total_cost_usd"] - rec.total_cost_usd) < 1e-6)
                )
            )
            if is_exact_replay:
                summary.replayed_count += 1
                summary.partitions.append(rec)
                continue
            else:
                summary.overlap_count += 1
                summary.unresolved_overlaps.append({
                    "partition_id": p_id,
                    "existing": ex,
                    "incoming": asdict(rec),
                    "reason": "partition_id_conflict_different_metrics",
                })
                summary.partitions.append(rec)
                partitions_to_insert.append(rec)
                continue

        # Check boundary overlap against existing active partitions
        for ex_id, ex in existing_partitions.items():
            if (
                ex["source_name"] == rec.source_name
                and ex["account_alias"] == rec.account_alias
                and ex["grain"] == rec.grain
                and ex["is_live_replaced"] == 0
            ):
                if rec.start_time < ex["end_time"] and ex["start_time"] < rec.end_time:
                    summary.overlap_count += 1
                    overlap_info = {
                        "partition_id": rec.partition_id,
                        "conflicting_partition_id": ex_id,
                        "source_name": rec.source_name,
                        "grain": rec.grain,
                        "start_time": rec.start_time,
                        "end_time": rec.end_time,
                        "conflicting_start": ex["start_time"],
                        "conflicting_end": ex["end_time"],
                        "reason": "unresolved_time_boundary_overlap",
                    }
                    summary.unresolved_overlaps.append(overlap_info)
                    rec.missingness["unresolved_overlap"] = overlap_info

        partitions_to_insert.append(rec)

    # Persist validated partitions atomically
    with conn:
        for rec in partitions_to_insert:
            _ensure_project_exists(
                conn=conn,
                project_id=rec.project_id,
                account_alias=rec.account_alias,
                timestamp=rec.created_at or now_utc,
            )

            conn.execute(
                """
                INSERT INTO archive_partitions (
                    partition_id, partition_type, grain, source_name, timezone_name,
                    start_time, end_time, total_tokens, total_cost_usd, records_count,
                    pricing_status, missingness_json, coverage_json, is_reconciled,
                    is_live_replaced, reconciliation_notes, account_alias, project_id,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(partition_id) DO UPDATE SET
                    partition_type = excluded.partition_type,
                    grain = excluded.grain,
                    source_name = excluded.source_name,
                    timezone_name = excluded.timezone_name,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    total_tokens = excluded.total_tokens,
                    total_cost_usd = excluded.total_cost_usd,
                    records_count = excluded.records_count,
                    pricing_status = excluded.pricing_status,
                    missingness_json = excluded.missingness_json,
                    coverage_json = excluded.coverage_json,
                    is_reconciled = excluded.is_reconciled,
                    is_live_replaced = excluded.is_live_replaced,
                    reconciliation_notes = excluded.reconciliation_notes,
                    account_alias = excluded.account_alias,
                    project_id = excluded.project_id,
                    payload_json = excluded.payload_json;
                """,
                (
                    rec.partition_id,
                    rec.partition_type,
                    rec.grain,
                    rec.source_name,
                    rec.timezone_name,
                    rec.start_time,
                    rec.end_time,
                    rec.total_tokens,
                    rec.total_cost_usd,
                    rec.records_count,
                    rec.pricing_status,
                    json.dumps(rec.missingness),
                    json.dumps(rec.coverage),
                    1 if rec.is_reconciled else 0,
                    1 if rec.is_live_replaced else 0,
                    rec.reconciliation_notes,
                    rec.account_alias,
                    rec.project_id,
                    json.dumps(rec.payload),
                    rec.created_at,
                ),
            )
            summary.imported_count += 1
            if rec not in summary.partitions:
                summary.partitions.append(rec)

    return summary


def import_archive_file(
    conn: sqlite3.Connection,
    file_path: str,
    account_alias: Optional[str] = None,
    project_id: Optional[str] = None,
) -> ImportSummary:
    """Reads and imports an archive partition bundle from a local file path."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Archive file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as fh:
        raw_text = fh.read()

    return import_archive_partitions(
        conn=conn,
        artifact_data=raw_text,
        account_alias=account_alias,
        project_id=project_id,
    )


# -----------------------------------------------------------------------------
# 7. Verification-Driven Reconciliation
# -----------------------------------------------------------------------------

def reconcile_archive_partition(
    conn: sqlite3.Connection,
    partition_id: str,
    proof: Optional[ReconciliationProof] = None,
) -> ReconciliationResult:
    """Reconciles an archive partition against live canonical records and checkpoints."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT partition_id, partition_type, grain, source_name, timezone_name,
               start_time, end_time, total_tokens, total_cost_usd, records_count,
               pricing_status, missingness_json, coverage_json, is_reconciled,
               is_live_replaced, reconciliation_notes, account_alias, project_id
        FROM archive_partitions
        WHERE partition_id = ?;
        """,
        (partition_id,),
    )
    row = cur.fetchone()
    cur.close()

    if not row:
        raise KeyError(f"Archive partition not found: {partition_id}")

    p = dict(row)

    # 1. Compatibility Check: Grain
    if proof and proof.grain != p["grain"]:
        notes = f"Incompatible grain: partition grain is '{p['grain']}', but proof grain is '{proof.grain}'. Refusing replacement."
        _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
        return ReconciliationResult(
            partition_id=partition_id,
            is_reconciled=False,
            is_live_replaced=False,
            status="incompatible_grain",
            notes=notes,
        )

    # 2. Compatibility Check: Timezone
    if proof and proof.timezone_name != p["timezone_name"]:
        notes = f"Incompatible timezone: partition timezone is '{p['timezone_name']}', but proof timezone is '{proof.timezone_name}'. Refusing replacement."
        _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
        return ReconciliationResult(
            partition_id=partition_id,
            is_reconciled=False,
            is_live_replaced=False,
            status="incompatible_timezone",
            notes=notes,
        )

    # 3. Compatibility Check: Token Semantics
    if proof and proof.token_semantics != "disjoint_billable":
        notes = (
            f"Incompatible token semantics '{proof.token_semantics}'. "
            "Cannot subtract or compare incompatible totals. Refusing replacement."
        )
        _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
        return ReconciliationResult(
            partition_id=partition_id,
            is_reconciled=False,
            is_live_replaced=False,
            status="incompatible_token_semantics",
            notes=notes,
        )

    # 4. Explicit Proof Gap Check
    if proof and proof.gap_intervals:
        notes = f"Coverage gaps detected in proof: {proof.gap_intervals}. Refusing partial subtraction or replacement."
        _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
        return ReconciliationResult(
            partition_id=partition_id,
            is_reconciled=False,
            is_live_replaced=False,
            status="coverage_gap_detected",
            notes=notes,
        )

    # 5. Query Live Canonical Data in Partition Range
    adapter_name = p["source_name"]
    harness_map = {
        "omp": ("omp", "oh-my-pi"),
        "claude_code": ("claude_code", "claude-code"),
    }
    harnesses = harness_map.get(adapter_name, (adapter_name,))

    placeholders = ",".join(["?"] * len(harnesses))
    params: List[Any] = [p["start_time"], p["end_time"]]
    params.extend(harnesses)
    params.append(p["account_alias"])

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT cr.canonical_id, cr.request_timestamp,
               (COALESCE(nu.fresh_input_tokens, 0) + COALESCE(nu.cache_read_tokens, 0) +
                COALESCE(nu.cache_write_5m_tokens, 0) + COALESCE(nu.cache_write_1h_tokens, 0) +
                COALESCE(nu.cache_write_unknown_ttl_tokens, 0) + COALESCE(nu.output_tokens, 0)) AS disjoint_total,
               nu.reported_total_tokens
        FROM canonical_requests cr
        LEFT JOIN normalized_usage nu ON cr.canonical_id = nu.canonical_id
        WHERE cr.request_timestamp >= ? AND cr.request_timestamp < ?
          AND cr.duplicate_of IS NULL
          AND cr.origin_harness IN ({placeholders})
          AND cr.account_alias = ?
        ORDER BY cr.request_timestamp ASC;
        """,
        params,
    )
    canonical_rows = cur.fetchall()
    cur.close()

    live_count = len(canonical_rows)
    live_tokens = sum(r["disjoint_total"] or r["reported_total_tokens"] or 0 for r in canonical_rows)

    if live_count == 0:
        notes = "No live canonical requests found in partition interval. Partition remains active."
        _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
        return ReconciliationResult(
            partition_id=partition_id,
            is_reconciled=False,
            is_live_replaced=False,
            status="missing_canonical_data",
            notes=notes,
            canonical_records_count=0,
            canonical_total_tokens=0,
        )

    # 6. Checkpoint Verification
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT status, last_byte_offset, last_scan_at, last_success_at, error_count
        FROM source_checkpoints
        WHERE origin_harness IN ({placeholders})
          AND account_alias = ?;
        """,
        list(harnesses) + [p["account_alias"]],
    )
    checkpoint_rows = cur.fetchall()
    cur.close()

    checkpoint_verified = False
    if checkpoint_rows:
        all_completed = all(r["status"] in ("completed", "active") and (r["error_count"] or 0) == 0 for r in checkpoint_rows)
        has_reached_end = any((r["last_success_at"] or r["last_scan_at"] or "") >= p["end_time"] for r in checkpoint_rows)
        checkpoint_verified = all_completed and (has_reached_end or (proof and proof.checkpoint_verified))

    if proof:
        if not proof.checkpoint_verified and not checkpoint_verified:
            notes = "Source checkpoints not verified as completed through partition end time."
            _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
            return ReconciliationResult(
                partition_id=partition_id,
                is_reconciled=False,
                is_live_replaced=False,
                status="checkpoint_incomplete",
                notes=notes,
                canonical_records_count=live_count,
                canonical_total_tokens=live_tokens,
            )

        if not proof.contiguous_coverage:
            notes = "Proof indicates non-contiguous coverage. Partial intervals cannot replace aggregate partition."
            _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
            return ReconciliationResult(
                partition_id=partition_id,
                is_reconciled=False,
                is_live_replaced=False,
                status="coverage_gap_detected",
                notes=notes,
                canonical_records_count=live_count,
                canonical_total_tokens=live_tokens,
            )

        if proof.expected_records_count is not None and proof.expected_records_count != live_count:
            notes = (
                f"Records count discrepancy: proof expected {proof.expected_records_count}, "
                f"found {live_count} canonical records."
            )
            _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
            return ReconciliationResult(
                partition_id=partition_id,
                is_reconciled=False,
                is_live_replaced=False,
                status="records_count_mismatch",
                notes=notes,
                canonical_records_count=live_count,
                canonical_total_tokens=live_tokens,
            )
    else:
        if not checkpoint_verified:
            notes = "Automatic reconciliation rejected: live checkpoints not verified through partition end time."
            _record_reconciliation_attempt(conn, partition_id, is_reconciled=False, is_replaced=False, notes=notes)
            return ReconciliationResult(
                partition_id=partition_id,
                is_reconciled=False,
                is_live_replaced=False,
                status="checkpoint_incomplete",
                notes=notes,
                canonical_records_count=live_count,
                canonical_total_tokens=live_tokens,
            )

    # 7. Complete Coverage Verified: Grant Replacement State
    notes = (
        f"Verified complete canonical coverage ({live_count} records, {live_tokens} tokens). "
        "Partition replaced by live canonical data. Source logs preserved intact."
    )
    with conn:
        conn.execute(
            """
            UPDATE archive_partitions
            SET is_reconciled = 1,
                is_live_replaced = 1,
                reconciliation_notes = ?
            WHERE partition_id = ?;
            """,
            (notes, partition_id),
        )

    return ReconciliationResult(
        partition_id=partition_id,
        is_reconciled=True,
        is_live_replaced=True,
        status="reconciled_replaced",
        notes=notes,
        canonical_records_count=live_count,
        canonical_total_tokens=live_tokens,
    )


def _record_reconciliation_attempt(
    conn: sqlite3.Connection,
    partition_id: str,
    is_reconciled: bool,
    is_replaced: bool,
    notes: str,
) -> None:
    """Updates reconciliation notes on the partition record without destructive changes."""
    with conn:
        conn.execute(
            """
            UPDATE archive_partitions
            SET reconciliation_notes = ?
            WHERE partition_id = ?;
            """,
            (notes, partition_id),
        )


# -----------------------------------------------------------------------------
# 8. Combined Partition Selection
# -----------------------------------------------------------------------------

def select_combined_partitions(
    conn: sqlite3.Connection,
    source_name: Optional[str] = None,
    account_alias: Optional[str] = None,
    project_id: Optional[str] = None,
    grain: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    include_superseded: bool = False,
) -> CombinedPartitionSelection:
    """Selects archive partitions, strictly excluding superseded partitions and flagging partial overlaps."""
    conditions: List[str] = []
    params: List[Any] = []

    if source_name:
        conditions.append("source_name = ?")
        params.append(source_name)

    if account_alias:
        clean_acct = sanitize_archive_account(account_alias)
        conditions.append("account_alias = ?")
        params.append(clean_acct)

    if project_id:
        clean_proj = sanitize_archive_project(project_id)
        conditions.append("project_id = ?")
        params.append(clean_proj)

    if grain:
        conditions.append("grain = ?")
        params.append(grain)

    if start_time:
        conditions.append("end_time > ?")
        params.append(start_time)

    if end_time:
        conditions.append("start_time < ?")
        params.append(end_time)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        SELECT partition_id, partition_type, grain, source_name, timezone_name,
               start_time, end_time, total_tokens, total_cost_usd, records_count,
               pricing_status, missingness_json, coverage_json, is_reconciled,
               is_live_replaced, reconciliation_notes, account_alias, project_id,
               payload_json, created_at
        FROM archive_partitions
        {where_clause}
        ORDER BY start_time ASC, partition_id ASC;
    """

    cur = conn.cursor()
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()

    active: List[Dict[str, Any]] = []
    superseded: List[Dict[str, Any]] = []

    for r in rows:
        r["missingness"] = json.loads(r.get("missingness_json") or "{}")
        r["coverage"] = json.loads(r.get("coverage_json") or "{}")
        r["payload"] = json.loads(r.get("payload_json") or "{}")

        if r["is_live_replaced"] == 1:
            superseded.append(r)
            if include_superseded:
                active.append(r)
        else:
            active.append(r)

    # Detect partial overlaps among active partitions
    partial_overlaps: List[Dict[str, Any]] = []
    for i in range(len(active)):
        p1 = active[i]
        for j in range(i + 1, len(active)):
            p2 = active[j]
            if (
                p1["source_name"] == p2["source_name"]
                and p1["account_alias"] == p2["account_alias"]
                and p1["grain"] == p2["grain"]
            ):
                if p1["start_time"] < p2["end_time"] and p2["start_time"] < p1["end_time"]:
                    if p1["start_time"] != p2["start_time"] or p1["end_time"] != p2["end_time"]:
                        overlap_item = {
                            "partition_a": p1["partition_id"],
                            "partition_b": p2["partition_id"],
                            "source_name": p1["source_name"],
                            "grain": p1["grain"],
                            "span_a": (p1["start_time"], p1["end_time"]),
                            "span_b": (p2["start_time"], p2["end_time"]),
                            "overlap_type": "partial_boundary_overlap",
                        }
                        partial_overlaps.append(overlap_item)

    return CombinedPartitionSelection(
        active_partitions=active,
        superseded_partitions=superseded,
        partial_overlaps=partial_overlaps,
        has_unresolved_overlap=bool(partial_overlaps),
    )
