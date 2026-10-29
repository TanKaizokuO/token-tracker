"""Backfill session ids for oh-my-pi canonical requests ingested before header ids were read.

The oh-my-pi header line (type == "session") is the only place the session id lives.
Its observation is stored in source_observations with provenance_json.session_id.
A request's session is resolved through request_observation_links to that file's header
observation. Only NULL session_id values are written, and sessions rows are created the
same way ingest creates them. The function is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Dict, Optional, Set, Tuple

from token_ledger.ingest.runner import _ensure_project_and_session

OMP_HARNESS = "oh-my-pi"


def _header_session_ids(conn: sqlite3.Connection) -> Tuple[
    Dict[Tuple[str, Optional[str]], Set[str]], Dict[str, Set[str]]
]:
    """Map header session ids by (file_locator, file_generation) and by file_locator."""
    by_gen: Dict[Tuple[str, Optional[str]], Set[str]] = defaultdict(set)
    by_locator: Dict[str, Set[str]] = defaultdict(set)
    rows = conn.execute(
        """
        SELECT file_locator, file_generation, provenance_json
        FROM source_observations
        WHERE source_adapter = 'omp' AND provenance_json LIKE '%session%';
        """
    ).fetchall()
    for locator, generation, provenance_json in rows:
        try:
            provenance: Dict[str, Any] = json.loads(provenance_json or "{}")
        except ValueError:
            continue
        if provenance.get("entry_type") != "session":
            continue
        session_id = provenance.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        by_gen[(locator, generation)].add(session_id)
        by_locator[locator].add(session_id)
    return by_gen, by_locator


def _resolve_session(
    locator: str,
    generation: Optional[str],
    by_gen: Dict[Tuple[str, Optional[str]], Set[str]],
    by_locator: Dict[str, Set[str]],
) -> Optional[str]:
    """Pick the single header session id for a file, or None when it is absent or ambiguous."""
    same_gen = by_gen.get((locator, generation), set())
    if len(same_gen) == 1:
        return next(iter(same_gen))
    if same_gen:
        return None
    any_gen = by_locator.get(locator, set())
    if len(any_gen) == 1:
        return next(iter(any_gen))
    return None


def backfill_omp_session_ids(conn: sqlite3.Connection) -> Dict[str, int]:
    """Fill NULL session_id on oh-my-pi canonical_requests from their file header.

    Only canonical_requests.session_id is updated, plus missing sessions rows
    (and projects rows already required by the FK). Returns counts for reporting.
    """
    by_gen, by_locator = _header_session_ids(conn)

    candidates = conn.execute(
        """
        SELECT cr.canonical_id, cr.account_alias, cr.project_id, cr.request_timestamp,
               o.file_locator, o.file_generation
        FROM canonical_requests cr
        JOIN request_observation_links l ON l.canonical_id = cr.canonical_id
        JOIN source_observations o ON o.observation_id = l.observation_id
        WHERE cr.origin_harness = ? AND cr.session_id IS NULL AND o.source_adapter = 'omp'
        ORDER BY cr.request_timestamp, cr.canonical_id;
        """,
        (OMP_HARNESS,),
    ).fetchall()

    # canonical_id -> (session_id or None on conflict, request metadata)
    resolved: Dict[str, Optional[str]] = {}
    meta: Dict[str, Tuple[Optional[str], str, str]] = {}
    for canonical_id, account_alias, project_id, request_ts, locator, generation in candidates:
        session_id = _resolve_session(locator, generation, by_gen, by_locator)
        if canonical_id in resolved:
            # Observations of one request disagreeing on the session makes it ambiguous.
            if resolved[canonical_id] != session_id:
                resolved[canonical_id] = None
            continue
        resolved[canonical_id] = session_id
        meta[canonical_id] = (project_id, account_alias, request_ts)

    sessions_before = conn.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0]
    updated = 0
    unresolved = 0
    with conn:
        for canonical_id, session_id in resolved.items():
            if session_id is None:
                unresolved += 1
                continue
            project_id, account_alias, request_ts = meta[canonical_id]
            _ensure_project_and_session(
                conn=conn,
                project_id=project_id,
                session_id=session_id,
                origin_harness=OMP_HARNESS,
                account_alias=account_alias,
                timestamp=request_ts,
            )
            cur = conn.execute(
                """
                UPDATE canonical_requests SET session_id = ?
                WHERE canonical_id = ? AND session_id IS NULL;
                """,
                (session_id, canonical_id),
            )
            updated += cur.rowcount
    sessions_after = conn.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0]

    remaining_null = conn.execute(
        "SELECT COUNT(*) FROM canonical_requests WHERE origin_harness = ? AND session_id IS NULL;",
        (OMP_HARNESS,),
    ).fetchone()[0]
    return {
        "requests_considered": len(resolved),
        "requests_updated": updated,
        "requests_unresolved": unresolved,
        "sessions_inserted": sessions_after - sessions_before,
        "omp_requests_still_null": remaining_null,
    }
