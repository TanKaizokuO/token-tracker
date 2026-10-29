"""Recompute canonical_requests.is_unresolved_overlap from stored cross-row evidence.

A request is flagged only when stored evidence shows an overlap that cannot be resolved:

- a candidate pair sharing a provider_request_id that evaluate_cross_source_identity
  reports as unresolved (unverified/anonymous account, or unknown provider);
- a request whose provider is unknown;
- a request whose parent link is still staged in staging_unresolved_parent_links.

A missing account label on its own is not an overlap, and neither is a request that
shares an id with a request under a different, explicit account. The function resets every
flag and re-derives it, so it is idempotent and safe to run after backfills or ingest.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, List, Set

from token_ledger.ingest.identity import CrossSourceCandidate, evaluate_cross_source_identity


def _candidate(row: tuple) -> CrossSourceCandidate:
    canonical_id, origin_harness, provider, account_alias, provider_request_id, fallback_identity = row
    return CrossSourceCandidate(
        origin_harness=origin_harness or "",
        source_adapter=origin_harness or "",
        provider=provider or "unknown",
        account_alias=account_alias or "default",
        provider_request_id=provider_request_id,
        fallback_identity=fallback_identity,
    )


def recompute_overlap_flags(conn: sqlite3.Connection) -> Dict[str, int]:
    """Re-derive is_unresolved_overlap for every canonical request.

    Runs on the caller's transaction (no commit here). Returns counts for reporting.
    """
    flagged_before = conn.execute(
        "SELECT COUNT(*) FROM canonical_requests WHERE is_unresolved_overlap <> 0;"
    ).fetchone()[0]

    reasons: Dict[str, Set[str]] = {
        "duplicate_candidate": set(),
        "unknown_provider": set(),
        "unresolved_parent": set(),
    }

    # 1. Candidate pairs: same provider_request_id, evaluated by the identity rules.
    groups: Dict[str, List[tuple]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT cr.canonical_id, cr.origin_harness, cr.provider, cr.account_alias,
               cr.provider_request_id, cr.fallback_identity
        FROM canonical_requests cr
        WHERE cr.provider_request_id IN (
            SELECT provider_request_id FROM canonical_requests
            WHERE provider_request_id IS NOT NULL
            GROUP BY provider_request_id HAVING COUNT(*) > 1
        );
        """
    ):
        groups[row[4]].append(row)

    for rows in groups.values():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                _, unresolved, _ = evaluate_cross_source_identity(_candidate(rows[i]), _candidate(rows[j]))
                if unresolved:
                    reasons["duplicate_candidate"].add(rows[i][0])
                    reasons["duplicate_candidate"].add(rows[j][0])

    # 2. Unknown provider cannot establish identity.
    for (canonical_id,) in conn.execute(
        "SELECT canonical_id FROM canonical_requests WHERE provider IS NULL OR provider IN ('', 'unknown');"
    ):
        reasons["unknown_provider"].add(canonical_id)

    # 3. Parent link still staged (parent observation not yet seen). Ingest creates this
    # table lazily, so a database that never saw an unresolved parent may not have it.
    has_staging = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'staging_unresolved_parent_links';"
    ).fetchone()
    if has_staging:
        for (canonical_id,) in conn.execute(
            "SELECT child_canonical_id FROM staging_unresolved_parent_links;"
        ):
            reasons["unresolved_parent"].add(canonical_id)

    flagged: Set[str] = set().union(*reasons.values())

    conn.execute("UPDATE canonical_requests SET is_unresolved_overlap = 0 WHERE is_unresolved_overlap <> 0;")
    conn.executemany(
        "UPDATE canonical_requests SET is_unresolved_overlap = 1 WHERE canonical_id = ?;",
        [(cid,) for cid in sorted(flagged)],
    )

    flagged_after = len(flagged)
    return {
        "rows_flagged_before": flagged_before,
        "rows_flagged_after": flagged_after,
        "flagged_duplicate_candidate": len(reasons["duplicate_candidate"]),
        "flagged_unknown_provider": len(reasons["unknown_provider"]),
        "flagged_unresolved_parent": len(reasons["unresolved_parent"]),
    }
