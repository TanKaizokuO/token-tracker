"""Recompute canonical_requests.is_unresolved_overlap and duplicate_of from stored evidence.

Same-call duplicates:

- rows in the same harness and provider that share a non-null provider_request_id are one
  provider call, whatever account_alias each row was given (aliases are derived per session
  file). The row with the largest output_tokens is canonical (ties: smallest canonical_id);
  every other row points at it through duplicate_of and is excluded from aggregates. The whole
  group is unflagged.

A request is flagged only when stored evidence shows an overlap that cannot be resolved:

- a candidate pair sharing a provider_request_id across different harnesses (or outside a
  same-provider duplicate group) that evaluate_cross_source_identity reports as unresolved
  (unverified/anonymous account, or unknown provider);
- a pair sharing a provider_request_id across different providers;
- a request whose provider is unknown;
- a request whose parent link is still staged in staging_unresolved_parent_links.

A missing account label on its own is not an overlap, and neither is a request that
shares an id with a request under a different, explicit account within one harness and
provider. The function resets every flag and duplicate_of and re-derives them, so it is
idempotent and safe to run after backfills or ingest.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

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


def _norm_provider(provider: Optional[str]) -> str:
    return (provider or "").strip().lower()


def _known_provider(provider: Optional[str]) -> bool:
    return _norm_provider(provider) not in ("", "unknown")


def recompute_overlap_flags(conn: sqlite3.Connection) -> Dict[str, int]:
    """Re-derive is_unresolved_overlap and duplicate_of for every canonical request.

    Runs on the caller's transaction (no commit here). Returns counts for reporting.
    """
    flagged_before = conn.execute(
        "SELECT COUNT(*) FROM canonical_requests WHERE is_unresolved_overlap <> 0;"
    ).fetchone()[0]

    conn.execute("UPDATE canonical_requests SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL;")

    reasons: Dict[str, Set[str]] = {
        "duplicate_candidate": set(),
        "unknown_provider": set(),
        "unresolved_parent": set(),
    }

    # 1. Same-call duplicates: same harness + known provider + provider_request_id.
    dup_groups: Dict[Tuple[str, str, str], List[tuple]] = defaultdict(list)
    dup_key_by_id: Dict[str, Tuple[str, str, str]] = {}
    for row in conn.execute(
        """
        SELECT cr.canonical_id, cr.origin_harness, cr.provider, cr.account_alias,
               cr.provider_request_id, cr.fallback_identity, nu.output_tokens
        FROM canonical_requests cr
        LEFT JOIN normalized_usage nu ON nu.canonical_id = cr.canonical_id
        WHERE cr.provider_request_id IS NOT NULL;
        """
    ):
        canonical_id, harness, provider, _alias, request_id, _fallback, output_tokens = row
        if not _known_provider(provider):
            continue
        key = (harness or "", _norm_provider(provider), request_id)
        dup_groups[key].append((canonical_id, output_tokens))
        dup_key_by_id[canonical_id] = key

    duplicate_rows = 0
    duplicate_groups = 0
    marks: List[Tuple[str, str]] = []
    for members in dup_groups.values():
        if len(members) < 2:
            continue
        duplicate_groups += 1
        # Largest output_tokens wins; NULL counts as lowest; ties go to the smallest canonical_id.
        ordered = sorted(members, key=lambda m: (-(m[1] if m[1] is not None else -1), m[0]))
        canonical = ordered[0][0]
        for member_id, _ in ordered[1:]:
            marks.append((canonical, member_id))
            duplicate_rows += 1
    conn.executemany(
        "UPDATE canonical_requests SET duplicate_of = ? WHERE canonical_id = ?;",
        marks,
    )

    # 2. Candidate pairs sharing a request id, excluding pairs inside one same-call group.
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
                a, b = rows[i], rows[j]
                key_a = dup_key_by_id.get(a[0])
                if key_a is not None and key_a == dup_key_by_id.get(b[0]):
                    continue  # same-call duplicates are resolved above
                if _norm_provider(a[2]) != _norm_provider(b[2]):
                    # Same request id under different providers is a genuine conflict.
                    reasons["duplicate_candidate"].add(a[0])
                    reasons["duplicate_candidate"].add(b[0])
                    continue
                _, unresolved, _ = evaluate_cross_source_identity(_candidate(a), _candidate(b))
                if unresolved:
                    reasons["duplicate_candidate"].add(a[0])
                    reasons["duplicate_candidate"].add(b[0])

    # 3. Unknown provider cannot establish identity.
    for (canonical_id,) in conn.execute(
        "SELECT canonical_id FROM canonical_requests WHERE provider IS NULL OR provider IN ('', 'unknown');"
    ):
        reasons["unknown_provider"].add(canonical_id)

    # 4. Parent link still staged (parent observation not yet seen). Ingest creates this
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
        "same_call_duplicate_groups": duplicate_groups,
        "same_call_duplicate_rows": duplicate_rows,
    }
