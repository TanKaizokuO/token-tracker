"""Incremental ingestion runner for Token Ledger.

Provides durable checkpoint-based log file ingestion, rotation and truncation detection,
incomplete-line reread handling, atomic database transactions, sensitive data redaction,
and sync health diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from token_ledger.ingest.identity import (
    CanonicalIdentity,
    build_canonical_id,
    build_fallback_identity,
    create_canonical_identity,
    normalize_model_name,
)
from token_ledger.ingest.normalize import (
    NORMALIZATION_RULE_VERSION,
    PARSER_VERSION,
    NormalizedObservationRecord,
    normalize_observation,
)
from token_ledger.privacy import (
    is_opt_in_enabled,
    redact_secrets,
    sanitize_account_alias,
    sanitize_file_locator,
    sanitize_metadata,
)


@dataclass
class IngestResult:
    """Summary of an ingestion run on a single log file."""
    file_locator: str
    lines_read: int = 0
    observations_inserted: int = 0
    canonical_requests_inserted: int = 0
    canonical_requests_updated: int = 0
    events_inserted: int = 0
    parse_errors: int = 0
    bytes_processed: int = 0
    file_generation: str = "gen_1"
    rotation_or_truncation_detected: bool = False
    status: str = "ok"  # 'ok' | 'partial' | 'error'
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SyncHealth:
    """Diagnostic health status for an ingested log file checkpoint."""
    source_adapter: str
    file_locator: str
    origin_harness: str
    account_alias: str
    file_generation: Optional[str]
    file_size: int
    last_byte_offset: int
    last_line_index: int
    last_scan_at: str
    last_success_at: Optional[str]
    status: str
    error_count: int
    last_error: Optional[str]
    total_observations: int = 0
    newest_observation_time: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_sync_health(
    conn: sqlite3.Connection,
    source_adapter: Optional[str] = None,
    file_locator: Optional[str] = None,
) -> List[SyncHealth]:
    """Inspects source_checkpoints and joins observation statistics for sync health."""
    query = """
        SELECT
            c.source_adapter,
            c.file_locator,
            c.origin_harness,
            c.account_alias,
            c.file_generation,
            c.file_size,
            c.last_byte_offset,
            c.last_line_index,
            c.last_scan_at,
            c.last_success_at,
            c.status,
            c.error_count,
            c.last_error,
            COUNT(o.observation_id) AS total_obs,
            MAX(o.observed_at) AS newest_obs_time
        FROM source_checkpoints c
        LEFT JOIN source_observations o
            ON c.source_adapter = o.source_adapter
            AND c.file_locator = o.file_locator
    """
    params: List[Any] = []
    clauses: List[str] = []
    if source_adapter:
        clauses.append("c.source_adapter = ?")
        params.append(source_adapter)
    if file_locator:
        sanitized_loc = sanitize_file_locator(file_locator, hash_locator=True)
        clauses.append("(c.file_locator = ? OR c.file_locator = ?)")
        params.extend([file_locator, sanitized_loc])

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " GROUP BY c.source_adapter, c.file_locator ORDER BY c.last_scan_at DESC;"

    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    results: List[SyncHealth] = []
    for r in rows:
        results.append(
            SyncHealth(
                source_adapter=r[0],
                file_locator=r[1],
                origin_harness=r[2],
                account_alias=r[3],
                file_generation=r[4],
                file_size=r[5] or 0,
                last_byte_offset=r[6] or 0,
                last_line_index=r[7] or 0,
                last_scan_at=r[8],
                last_success_at=r[9],
                status=r[10],
                error_count=r[11] or 0,
                last_error=r[12],
                total_observations=r[13] or 0,
                newest_observation_time=r[14],
            )
        )
    return results


def _detect_origin_harness(source_adapter: str, explicit_harness: Optional[str] = None) -> str:
    if explicit_harness:
        return explicit_harness.strip().lower()
    adapter = (source_adapter or "").strip().lower()
    if adapter in ("claude", "claude_code"):
        return "claude-code"
    if adapter in ("omp", "oh_my_pi", "oh-my-pi"):
        return "oh-my-pi"
    return adapter or "unknown"


def _sanitize_project_identifier(raw_project: Optional[str]) -> Optional[str]:
    """Ensures no raw absolute path is persisted as project ID or project name."""
    if not raw_project:
        return None
    cleaned = raw_project.strip()
    if not cleaned:
        return None
    # Idempotent: already a sanitized safe project or account identifier
    if cleaned.startswith(("proj_", "acct_")):
        return cleaned
    # Strip path separators, home directories, or arbitrary names to safe opaque hash
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    return f"proj_{digest}"


def _ensure_project_and_session(
    conn: sqlite3.Connection,
    project_id: Optional[str],
    session_id: Optional[str],
    origin_harness: str,
    account_alias: str,
    timestamp: str,
) -> None:
    """Guarantees parent foreign key records exist before child request/event insertion."""
    safe_project = _sanitize_project_identifier(project_id) if project_id else None

    if safe_project:
        conn.execute(
            """
            INSERT OR IGNORE INTO projects (
                project_id, project_name, project_alias, account_alias, is_active, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, 1, ?, '{}');
            """,
            (safe_project, safe_project, safe_project, account_alias, timestamp),
        )

    if session_id:
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (
                session_id, origin_harness, project_id, account_alias, is_subagent, status, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, 0, 'completed', ?, '{}');
            """,
            (session_id, origin_harness, safe_project, account_alias, timestamp),
        )


def ingest_file(
    conn: sqlite3.Connection,
    path: str,
    source_adapter: str,
    account_alias: Optional[str] = None,
    provider: Optional[str] = None,
    access_route: Optional[str] = None,
    origin_harness: Optional[str] = None,
) -> IngestResult:
    """Ingest one opened file generation; pathname replacements cannot change its identity."""
    try:
        source = open(path, "rb")
    except FileNotFoundError:
        return IngestResult(
            file_locator=sanitize_file_locator(path, hash_locator=True),
            status="error", error_message="Source file not found",
        )
    with source:
        return _ingest_open_file(
            conn, path, source, source_adapter, account_alias, provider,
            access_route, origin_harness,
        )


def _ingest_open_file(
    conn, path, source, source_adapter, account_alias, provider, access_route, origin_harness,
) -> IngestResult:
    """Ingests a source log file incrementally into Token Ledger SQLite database.

    Guarantees:
    1. Durable byte/line checkpointing in source_checkpoints.
    2. Rotation and truncation generation detection.
    3. Incomplete-line reread safety (leaves partial trailing bytes for next scan).
    4. Idempotent canonical request and usage upserts.
    5. Observation provenance and many-to-many request-observation linking.
    6. Non-silent parse error logging in source_observations without raw snippet leakage.
    7. Foreign key integrity enforcement for sessions and projects.
    8. Account and file locator privacy sanitization prior to persistence.
    9. Never trusts unverified provider inference from display model.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    # Canonical locator hashing: hashes path once to opaque loc_<sha256[:16]>
    sanitized_locator = sanitize_file_locator(path, hash_locator=True)

    explicit_account = False
    if account_alias and account_alias.strip() and account_alias.strip().lower() != "default":
        clean_account = sanitize_account_alias(account_alias)
        explicit_account = True
    else:
        # Default/absent account: normalizer will scope to anon_{source_adapter}_{loc_hash}
        loc_digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        clean_account = f"anon_{source_adapter}_{loc_digest}"

    harness = _detect_origin_harness(source_adapter, origin_harness)

    result = IngestResult(file_locator=sanitized_locator)

    file_stat = os.fstat(source.fileno())
    current_size = file_stat.st_size
    current_mtime = datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc).isoformat()

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT file_generation, file_size, last_byte_offset, last_line_index, status,
               error_count, account_alias, file_device, file_inode
        FROM source_checkpoints
        WHERE source_adapter = ? AND file_locator = ?;
        """,
        (source_adapter, sanitized_locator),
    )
    checkpoint = cursor.fetchone()

    last_offset = 0
    last_line = 0
    error_count = 0
    generation = f"gen_1_{int(file_stat.st_mtime)}"
    rotation_or_truncation = False

    if checkpoint:
        (prev_gen, prev_size, prev_offset, prev_line, prev_status, prev_errs,
         prev_acct, prev_device, prev_inode) = checkpoint
        last_offset = prev_offset or 0
        last_line = prev_line or 0
        error_count = prev_errs or 0
        generation = prev_gen or generation
        if not explicit_account and prev_acct:
            clean_account = prev_acct

        # Legacy offsets have no verifiable connection to the current file.
        # Do not invent continuity or replay fallback identities as new requests.
        if ((prev_device is None or prev_inode is None)
                and (last_offset or last_line or prev_size)):
            result.file_generation = generation
            result.status = "error"
            result.error_message = "Checkpoint file identity unverified; authenticated historical identity required"
            with conn:
                conn.execute(
                    """UPDATE source_checkpoints
                       SET status = 'identity_unverified', last_scan_at = ?, last_error = ?
                       WHERE source_adapter = ? AND file_locator = ?""",
                    (now_utc, result.error_message, source_adapter, sanitized_locator),
                )
            return result

        if ((prev_device, prev_inode) != (file_stat.st_dev, file_stat.st_ino)
                or current_size < max(last_offset, prev_size or 0)):
            rotation_or_truncation = True
            gen_idx = 1
            if prev_gen and prev_gen.startswith("gen_"):
                try:
                    parts = prev_gen.split("_")
                    gen_idx = int(parts[1]) + 1
                except (IndexError, ValueError):
                    gen_idx = 2
            generation = f"gen_{gen_idx}_{int(file_stat.st_mtime)}"
            last_offset = 0
            last_line = 0

    result.file_generation = generation
    result.rotation_or_truncation_detected = rotation_or_truncation

    # If already up to date, record scan and return
    if current_size == last_offset and not rotation_or_truncation:
        with conn:
            conn.execute(
                """
                INSERT INTO source_checkpoints (
                    source_adapter, origin_harness, account_alias, file_locator,
                    file_generation, file_size, file_mtime, last_byte_offset,
                    last_line_index, last_scan_at, last_success_at, status,
                    error_count, last_error, file_device, file_inode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?)
                ON CONFLICT(source_adapter, file_locator) DO UPDATE SET
                    last_scan_at = excluded.last_scan_at,
                    file_size = excluded.file_size,
                    file_mtime = excluded.file_mtime,
                    file_device = excluded.file_device,
                    file_inode = excluded.file_inode;
                """,
                (
                    source_adapter, harness, clean_account, sanitized_locator,
                    generation, current_size, current_mtime, last_offset,
                    last_line, now_utc, now_utc, error_count, file_stat.st_dev, file_stat.st_ino,
                ),
            )
        return result

    # Read from the same descriptor whose identity was checked above.
    source.seek(last_offset)
    raw_bytes = source.read(current_size - last_offset)

    # Empty replacement/truncation must also durably commit the new generation.

    # Incomplete line check: must find the last newline character
    last_newline_idx = raw_bytes.rfind(b"\n")
    if last_newline_idx == -1:
        # File has new bytes, but no complete line yet: do not advance offset
        with conn:
            conn.execute(
                """
                INSERT INTO source_checkpoints (
                    source_adapter, origin_harness, account_alias, file_locator,
                    file_generation, file_size, file_mtime, last_byte_offset,
                    last_line_index, last_scan_at, last_success_at, status,
                    error_count, last_error, file_device, file_inode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?)
                ON CONFLICT(source_adapter, file_locator) DO UPDATE SET
                    last_scan_at = excluded.last_scan_at,
                    file_size = excluded.file_size,
                    file_mtime = excluded.file_mtime,
                    file_generation = excluded.file_generation,
                    last_byte_offset = excluded.last_byte_offset,
                    last_line_index = excluded.last_line_index,
                    file_device = excluded.file_device,
                    file_inode = excluded.file_inode,
                    status = 'active', last_error = NULL;
                """,
                (
                    source_adapter, harness, clean_account, sanitized_locator,
                    generation, current_size, current_mtime, last_offset,
                    last_line, now_utc, now_utc, error_count, file_stat.st_dev, file_stat.st_ino,
                ),
            )
        return result

    valid_bytes = raw_bytes[: last_newline_idx + 1]
    consumed_bytes = len(valid_bytes)
    next_offset = last_offset + consumed_bytes
    result.bytes_processed = consumed_bytes

    text_content = valid_bytes.decode("utf-8", errors="replace")
    raw_lines = text_content.splitlines()

    current_line_idx = last_line
    observations_to_insert: List[Dict[str, Any]] = []
    canonical_to_upsert: List[Dict[str, Any]] = []
    usage_to_upsert: List[Dict[str, Any]] = []
    events_to_insert: List[Dict[str, Any]] = []
    links_to_insert: List[Dict[str, Any]] = []
    project_session_pairs: List[Tuple[Optional[str], Optional[str], str]] = []

    last_error_str = None

    for line_str in raw_lines:
        current_line_idx += 1
        result.lines_read += 1

        if not line_str.strip():
            continue

        record: Optional[NormalizedObservationRecord] = None
        try:
            session_ctx: Dict[str, Any] = {}
            if explicit_account:
                session_ctx["account_alias"] = clean_account
            if provider:
                session_ctx["provider"] = provider
            if access_route:
                session_ctx["access_route"] = access_route

            # Pass unhashed path: normalize_observation hashes path once to match sanitized_locator
            norm_adapter = "claude_code" if source_adapter in ("claude-code", "claude_code", "claude") else source_adapter
            record = normalize_observation(
                raw_record=line_str,
                source_adapter=norm_adapter,
                file_locator=path,
                line_index=current_line_idx,
                file_generation=generation,
                session_context=session_ctx,
            )
        except Exception as exc:
            result.parse_errors += 1
            error_count += 1
            last_error_str = f"Line {current_line_idx}: Parse failure"
            obs_id = f"obs_err_{uuid.uuid4().hex[:12]}"
            # Safe provenance without raw content or path leakage
            fallback_obs = {
                "observation_id": obs_id,
                "source_adapter": source_adapter,
                "origin_harness": harness,
                "account_alias": clean_account,
                "file_locator": sanitized_locator,
                "file_generation": generation,
                "line_index": current_line_idx,
                "event_id": None,
                "observed_at": now_utc,
                "raw_timestamp": now_utc,
                "parser_version": PARSER_VERSION,
                "raw_counters_json": "{}",
                "source_reported_total": None,
                "provenance_json": json.dumps({"error_kind": "malformed_json_or_parse_failure", "line_index": current_line_idx}),
                "parse_status": "corrupt",
                "error_message": last_error_str,
                "created_at": now_utc,
            }
            observations_to_insert.append(fallback_obs)
            continue

        if record.parse_status in ("corrupt", "parse_error"):
            result.parse_errors += 1
            error_count += 1
            last_error_str = record.error_message or "Unknown parse error"

        # Apply privacy sanitization before database persistence
        obs = record.observation
        clean_account = obs.get("account_alias") or clean_account
        obs["raw_counters_json"] = json.dumps(
            sanitize_metadata(json.loads(obs.get("raw_counters_json") or "{}"))
        )
        obs["provenance_json"] = json.dumps(
            sanitize_metadata(json.loads(obs.get("provenance_json") or "{}"))
        )
        observations_to_insert.append(obs)

        if record.canonical_request:
            req = record.canonical_request
            if "pricing_version_id" in line_str:
                try:
                    raw_dict = json.loads(line_str)
                    if raw_dict.get("pricing_version_id") and not req.get("pricing_version_id"):
                        req["pricing_version_id"] = raw_dict["pricing_version_id"]
                except Exception:
                    pass
            if req.get("project_id"):
                req["project_id"] = _sanitize_project_identifier(req["project_id"])
            req["raw_payload_json"] = json.dumps(
                sanitize_metadata(json.loads(req.get("raw_payload_json") or "{}"))
            )
            canonical_to_upsert.append(req)
            project_session_pairs.append((req.get("project_id"), req.get("session_id"), req.get("request_timestamp") or now_utc))

        if record.normalized_usage:
            usage_to_upsert.append(record.normalized_usage)

        if record.events:
            for ev in record.events:
                ev["details_json"] = json.dumps(
                    sanitize_metadata(json.loads(ev.get("details_json") or "{}"))
                )
                events_to_insert.append(ev)
                project_session_pairs.append((None, ev.get("session_id"), ev.get("started_at") or now_utc))

        if record.link:
            links_to_insert.append(record.link)

    # Perform atomic transactional write
    with conn:
        # 1. Ensure foreign key dependencies (projects, sessions) exist
        for p_id, s_id, t_stamp in project_session_pairs:
            _ensure_project_and_session(
                conn=conn,
                project_id=p_id,
                session_id=s_id,
                origin_harness=harness,
                account_alias=clean_account,
                timestamp=t_stamp,
            )

        # 2. Insert source observations
        for obs in observations_to_insert:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_observations (
                    observation_id, source_adapter, origin_harness, account_alias,
                    file_locator, file_generation, line_index, event_id,
                    observed_at, raw_timestamp, parser_version, raw_counters_json,
                    source_reported_total, provenance_json, parse_status,
                    error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    obs["observation_id"], obs["source_adapter"], obs["origin_harness"],
                    obs["account_alias"], obs["file_locator"], obs["file_generation"],
                    obs["line_index"], obs["event_id"], obs["observed_at"],
                    obs["raw_timestamp"], obs["parser_version"], obs["raw_counters_json"],
                    obs["source_reported_total"], obs["provenance_json"], obs["parse_status"],
                    obs["error_message"], obs["created_at"],
                ),
            )
            result.observations_inserted += 1

        # 3. Create staging relation for unresolved parent linkages if needed
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS staging_unresolved_parent_links (
                child_canonical_id TEXT PRIMARY KEY,
                parent_external_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                account_alias TEXT NOT NULL,
                origin_harness TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        # Build provider_request_id -> canonical_id mapping for resolving parent linkages
        prov_to_canonical: Dict[Union[str, Tuple[str, str, str]], str] = {}
        known_canonical_ids: Set[str] = set()
        cur_map = conn.cursor()
        cur_map.execute(
            "SELECT provider_request_id, canonical_id, provider, account_alias FROM canonical_requests WHERE provider_request_id IS NOT NULL;"
        )
        for r in cur_map.fetchall():
            prov_to_canonical[(r[2], r[3], r[0])] = r[1]
            prov_to_canonical[r[0]] = r[1]
            known_canonical_ids.add(r[1])

        for r in canonical_to_upsert:
            known_canonical_ids.add(r["canonical_id"])
            if r.get("provider_request_id"):
                prov_to_canonical[(r["provider"], r["account_alias"], r["provider_request_id"])] = r["canonical_id"]
                prov_to_canonical[r["provider_request_id"]] = r["canonical_id"]

        for req in canonical_to_upsert:
            # Preserve prompt linkage without foreign key violation
            if req.get("prompt_id"):
                s_id = req.get("session_id") or "default_session"
                _ensure_project_and_session(
                    conn=conn,
                    project_id=req.get("project_id"),
                    session_id=s_id,
                    origin_harness=harness,
                    account_alias=clean_account,
                    timestamp=req.get("request_timestamp") or now_utc,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO prompts (
                        prompt_id, session_id, prompt_group, created_at, metadata_json
                    ) VALUES (?, ?, 'default', ?, '{}');
                    """,
                    (req["prompt_id"], s_id, req.get("request_timestamp") or now_utc),
                )

            # Validate pricing version: NEVER fabricate rate card or pricing version from source
            if req.get("pricing_version_id"):
                cur_map.execute(
                    "SELECT 1 FROM pricing_versions WHERE version_id = ?;",
                    (req["pricing_version_id"],),
                )
                if cur_map.fetchone() is None:
                    # Stored version not validated in DB; retain in raw payload metadata, FK stays NULL (unpriced)
                    p_meta = json.loads(req.get("raw_payload_json") or "{}")
                    p_meta["unresolved_pricing_version_id"] = req["pricing_version_id"]
                    req["raw_payload_json"] = json.dumps(p_meta)
                    req["pricing_version_id"] = None

            # Handle parent request linkage without manufacturing placeholder requests
            parent = req.get("parent_request_id")
            if parent:
                resolved_parent = None
                if (req["provider"], req["account_alias"], parent) in prov_to_canonical:
                    resolved_parent = prov_to_canonical[(req["provider"], req["account_alias"], parent)]
                elif parent in prov_to_canonical:
                    resolved_parent = prov_to_canonical[parent]
                elif parent in known_canonical_ids:
                    resolved_parent = parent
                else:
                    cur_map.execute(
                        "SELECT canonical_id FROM canonical_requests WHERE canonical_id = ?;", (parent,)
                    )
                    r_canon = cur_map.fetchone()
                    if r_canon:
                        resolved_parent = r_canon[0]

                if resolved_parent:
                    req["parent_request_id"] = resolved_parent
                    conn.execute(
                        "DELETE FROM staging_unresolved_parent_links WHERE child_canonical_id = ?;",
                        (req["canonical_id"],),
                    )
                else:
                    # Real parent observation has not arrived yet.
                    # Never fabricate a canonical request. Leave FK NULL and store in safe metadata/staging.
                    req["parent_request_id"] = None
                    req["is_unresolved_overlap"] = 1
                    p_meta = json.loads(req.get("raw_payload_json") or "{}")
                    p_meta["unresolved_parent_request_id"] = parent
                    p_meta["unresolved_parent_scope"] = {
                        "provider": req["provider"],
                        "account_alias": req["account_alias"],
                    }
                    req["raw_payload_json"] = json.dumps(p_meta)

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO staging_unresolved_parent_links (
                            child_canonical_id, parent_external_id, provider, account_alias, origin_harness, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        (req["canonical_id"], parent, req["provider"], req["account_alias"], harness, now_utc),
                    )

        # 4. Upsert canonical requests
        # 3. Upsert canonical requests
        for req in canonical_to_upsert:
            cursor.execute(
                "SELECT canonical_id FROM canonical_requests WHERE canonical_id = ?;",
                (req["canonical_id"],),
            )
            exists = cursor.fetchone() is not None
            conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id,
                    fallback_identity, origin_harness, access_route, raw_model,
                    normalized_model, session_id, project_id, prompt_id,
                    parent_request_id, retry_attempt, request_status,
                    request_timestamp, timing_basis, duration_ms, ttft_ms,
                    generation_duration_ms, generation_speed_tps,
                    is_unresolved_overlap, pricing_version_id, created_at,
                    raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    provider = excluded.provider,
                    account_alias = excluded.account_alias,
                    provider_request_id = excluded.provider_request_id,
                    raw_model = excluded.raw_model,
                    normalized_model = excluded.normalized_model,
                    request_status = excluded.request_status,
                    request_timestamp = excluded.request_timestamp,
                    timing_basis = excluded.timing_basis,
                    duration_ms = excluded.duration_ms,
                    ttft_ms = excluded.ttft_ms,
                    generation_duration_ms = excluded.generation_duration_ms,
                    generation_speed_tps = excluded.generation_speed_tps,
                    is_unresolved_overlap = excluded.is_unresolved_overlap,
                    raw_payload_json = excluded.raw_payload_json;
                """,
                (
                    req["canonical_id"], req["provider"], req["account_alias"],
                    req["provider_request_id"], req["fallback_identity"], req["origin_harness"],
                    req["access_route"], req["raw_model"], req["normalized_model"],
                    req["session_id"], req["project_id"], req["prompt_id"],
                    req["parent_request_id"], req["retry_attempt"], req["request_status"],
                    req["request_timestamp"], req["timing_basis"], req["duration_ms"],
                    req["ttft_ms"], req["generation_duration_ms"], req["generation_speed_tps"],
                    req["is_unresolved_overlap"], req["pricing_version_id"], req["created_at"],
                    req["raw_payload_json"],
                ),
            )
            if exists:
                result.canonical_requests_updated += 1
            else:
                result.canonical_requests_inserted += 1

        # Resolve any staged child linkages whose parent has now arrived in this or prior transaction
        cur_map.execute(
            """
            SELECT s.child_canonical_id, s.parent_external_id, s.provider, s.account_alias
            FROM staging_unresolved_parent_links s;
            """
        )
        staged_children = cur_map.fetchall()
        for child_id, ext_parent_id, prov, acct in staged_children:
            parent_canon = None
            if (prov, acct, ext_parent_id) in prov_to_canonical:
                parent_canon = prov_to_canonical[(prov, acct, ext_parent_id)]
            elif ext_parent_id in prov_to_canonical:
                parent_canon = prov_to_canonical[ext_parent_id]
            else:
                cur_map.execute(
                    """
                    SELECT canonical_id FROM canonical_requests
                    WHERE (provider = ? AND account_alias = ? AND provider_request_id = ?)
                       OR (provider_request_id = ?)
                       OR (canonical_id = ?)
                    LIMIT 1;
                    """,
                    (prov, acct, ext_parent_id, ext_parent_id, ext_parent_id),
                )
                p_match = cur_map.fetchone()
                if p_match:
                    parent_canon = p_match[0]

            if parent_canon and parent_canon != child_id:
                conn.execute(
                    "UPDATE canonical_requests SET parent_request_id = ? WHERE canonical_id = ?;",
                    (parent_canon, child_id),
                )
                conn.execute(
                    "DELETE FROM staging_unresolved_parent_links WHERE child_canonical_id = ?;",
                    (child_id,),
                )

        # 5. Upsert normalized usage
        for usg in usage_to_upsert:
            conn.execute(
                """
                INSERT INTO normalized_usage (
                    canonical_id, fresh_input_tokens, cache_read_tokens,
                    cache_write_5m_tokens, cache_write_1h_tokens,
                    cache_write_unknown_ttl_tokens, output_tokens,
                    reasoning_tokens, reported_total_tokens,
                    counter_inclusion_semantics, normalization_rule_version,
                    is_partial, missing_counters_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    fresh_input_tokens = excluded.fresh_input_tokens,
                    cache_read_tokens = excluded.cache_read_tokens,
                    cache_write_5m_tokens = excluded.cache_write_5m_tokens,
                    cache_write_1h_tokens = excluded.cache_write_1h_tokens,
                    cache_write_unknown_ttl_tokens = excluded.cache_write_unknown_ttl_tokens,
                    output_tokens = excluded.output_tokens,
                    reasoning_tokens = excluded.reasoning_tokens,
                    reported_total_tokens = excluded.reported_total_tokens,
                    is_partial = excluded.is_partial,
                    missing_counters_json = excluded.missing_counters_json;
                """,
                (
                    usg["canonical_id"], usg["fresh_input_tokens"], usg["cache_read_tokens"],
                    usg["cache_write_5m_tokens"], usg["cache_write_1h_tokens"],
                    usg["cache_write_unknown_ttl_tokens"], usg["output_tokens"],
                    usg["reasoning_tokens"], usg["reported_total_tokens"],
                    usg["counter_inclusion_semantics"], usg["normalization_rule_version"],
                    usg["is_partial"], usg["missing_counters_json"],
                ),
            )

        # 5. Insert events
        for ev in events_to_insert:
            if ev.get("canonical_id") and ev["canonical_id"] in prov_to_canonical:
                ev["canonical_id"] = prov_to_canonical[ev["canonical_id"]]
            if ev.get("parent_event_id"):
                cur_map.execute("SELECT 1 FROM events WHERE event_id = ?;", (ev["parent_event_id"],))
                if cur_map.fetchone() is None:
                    d_meta = json.loads(ev.get("details_json") or "{}")
                    d_meta["unresolved_parent_event_id"] = ev["parent_event_id"]
                    ev["details_json"] = json.dumps(d_meta)
                    ev["parent_event_id"] = None
            conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, session_id, canonical_id, parent_event_id,
                    event_kind, event_name, started_at, ended_at,
                    duration_ms, output_size_bytes, status, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    ev["event_id"], ev["session_id"], ev.get("canonical_id"),
                    ev.get("parent_event_id"), ev["event_kind"], ev["event_name"],
                    ev["started_at"], ev.get("ended_at"), ev.get("duration_ms"),
                    ev.get("output_size_bytes"), ev.get("status", "ok"),
                    ev["details_json"],
                ),
            )
            result.events_inserted += 1
        # Resolve source-observed event parents that arrived later in this or a prior batch.
        cur_map.execute(
            """
            SELECT event_id, details_json FROM events
            WHERE parent_event_id IS NULL
              AND details_json LIKE '%"unresolved_parent_event_id"%';
            """
        )
        unresolved_events = cur_map.fetchall()
        for child_event_id, details_json in unresolved_events:
            try:
                details = json.loads(details_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(details, dict):
                continue
            parent_event_id = details.get("unresolved_parent_event_id")
            if not parent_event_id or parent_event_id == child_event_id:
                continue
            cur_map.execute("SELECT 1 FROM events WHERE event_id = ?;", (parent_event_id,))
            if cur_map.fetchone() is None:
                continue
            details.pop("unresolved_parent_event_id", None)
            conn.execute(
                """
                UPDATE events
                SET parent_event_id = ?, details_json = ?
                WHERE event_id = ? AND parent_event_id IS NULL;
                """,
                (parent_event_id, json.dumps(details), child_event_id),
            )


        # 6. Insert links
        for lnk in links_to_insert:
            conn.execute(
                """
                INSERT OR IGNORE INTO request_observation_links (
                    canonical_id, observation_id, linked_at
                ) VALUES (?, ?, ?);
                """,
                (lnk["canonical_id"], lnk["observation_id"], lnk["linked_at"]),
            )

        # 7. Update checkpoint atomically
        status = ("error" if result.parse_errors == result.observations_inserted else "partial") if result.parse_errors else "active"
        conn.execute(
            """
            INSERT INTO source_checkpoints (
                source_adapter, origin_harness, account_alias, file_locator,
                file_generation, file_size, file_mtime, last_byte_offset,
                last_line_index, last_scan_at, last_success_at, status,
                error_count, last_error, file_device, file_inode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_adapter, file_locator) DO UPDATE SET
                origin_harness = excluded.origin_harness,
                account_alias = excluded.account_alias,
                file_generation = excluded.file_generation,
                file_size = excluded.file_size,
                file_mtime = excluded.file_mtime,
                last_byte_offset = excluded.last_byte_offset,
                last_line_index = excluded.last_line_index,
                last_scan_at = excluded.last_scan_at,
                last_success_at = excluded.last_success_at,
                status = excluded.status,
                error_count = excluded.error_count,
                last_error = excluded.last_error,
                file_device = excluded.file_device,
                file_inode = excluded.file_inode;
            """,
            (
                source_adapter, harness, clean_account, sanitized_locator,
                generation, current_size, current_mtime, next_offset,
                current_line_idx, now_utc, now_utc, status,
                error_count, last_error_str, file_stat.st_dev, file_stat.st_ino,
            ),
        )

    result.status = "partial" if result.parse_errors > 0 else "ok"
    result.error_message = last_error_str
    return result
