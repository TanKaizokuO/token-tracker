"""Trace attribution, session hierarchy, and context analytics module.

Implements WP15 backend features and Rebuild Plan rows 10, 11, 12, 14, 29, 32, 33, 34, 40, 41:
- Exclusive ownership counts vs inclusive rollups for sessions and subagents.
- User prompt grouping and attribution without double-counting.
- Context window occupancy vs distinct cumulative input proxy.
- Compaction timeline and reduction tracking (with nullable before/after semantics).
- Tool usage, overhead, status, duration, and output size (never bodies or arguments).
- Error, retry, and loop detection (suspected signals with evidence links, never proven waste).
- Opt-in repetition and file-read analysis with change-state tracking.
- Source coverage transparency disclosing unavailable adapter event classes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from token_ledger.privacy import (
    OPTIN_CONTENT_FINGERPRINTS,
    is_opt_in_enabled,
    sanitize_file_locator,
)
from token_ledger.query import filtered_request_rows, validate_query

# Canonical context window limits by model prefix / identifier
KNOWN_CONTEXT_WINDOWS: Dict[str, int] = {
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "claude-code": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
}


def lookup_context_window(model_identity: Optional[str]) -> Optional[int]:
    """Resolves known model context window token limit, or returns None if unverified."""
    if not model_identity:
        return None
    cleaned = model_identity.lower().strip()
    for prefix, window in KNOWN_CONTEXT_WINDOWS.items():
        if prefix in cleaned:
            return window
    return None


def _safe_json_loads(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, str) and data.strip():
        try:
            val = json.loads(data)
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_session_name(metadata: Dict[str, Any], session_id: str) -> str:
    """Uses an explicit metadata label when present, otherwise the opaque session ID."""
    label = metadata.get("session_name")
    return label.strip() if isinstance(label, str) and label.strip() else session_id


def build_session_tree(
    conn: sqlite3.Connection,
    filtered_requests: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Constructs session hierarchy tree with separate exclusive and inclusive metrics.

    Enforces that exclusive ownership counts are never conflated with inclusive rollups.
    """
    session_ids = list({r["session_id"] for r in filtered_requests if r.get("session_id")})
    if not session_ids:
        return {
            "root_nodes": [],
            "all_nodes": {},
            "root_sessions_count": 0,
            "subagent_sessions_count": 0,
            "missing_parent_sessions_count": 0,
        }

    # Query sessions table for all sessions in scope
    placeholders = ",".join("?" for _ in session_ids)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT session_id, parent_session_id, project_id, started_at, duration_ms, metadata_json
        FROM sessions
        WHERE session_id IN ({placeholders})
        """,
        session_ids,
    )
    raw_sessions = cur.fetchall()
    session_meta: Dict[str, Dict[str, Any]] = {}
    for r in raw_sessions:
        metadata = _safe_json_loads(r[5])
        session_meta[r[0]] = {
            "session_id": r[0],
            "parent_session_id": r[1],
            "project_id": r[2],
            "session_name": _safe_session_name(metadata, r[0]),
            "started_at": r[3],
            "duration_ms": r[4],
            "metadata": metadata,
        }

    # If any sessions are referenced but not present in sessions table, synthesize minimal entry
    for s_id in session_ids:
        if s_id not in session_meta:
            session_meta[s_id] = {
                "session_id": s_id,
                "parent_session_id": None,
                "project_id": None,
                "session_name": s_id,
                "started_at": None,
                "duration_ms": None,
                "metadata": {},
            }

    # Check for parent sessions that might exist outside the direct request list
    parent_ids_to_check = {
        m["parent_session_id"]
        for m in session_meta.values()
        if m["parent_session_id"] and m["parent_session_id"] not in session_meta
    }
    missing_parent_sessions_count = 0
    if parent_ids_to_check:
        p_placeholders = ",".join("?" for _ in parent_ids_to_check)
        cur.execute(
            f"""
            SELECT session_id, parent_session_id, project_id, started_at, duration_ms, metadata_json
            FROM sessions
            WHERE session_id IN ({p_placeholders})
            """,
            list(parent_ids_to_check),
        )
        for r in cur.fetchall():
            metadata = _safe_json_loads(r[5])
            session_meta[r[0]] = {
                "session_id": r[0],
                "parent_session_id": r[1],
                "project_id": r[2],
                "session_name": _safe_session_name(metadata, r[0]),
                "started_at": r[3],
                "duration_ms": r[4],
                "metadata": metadata,
            }
        for p_id in parent_ids_to_check:
            if p_id not in session_meta:
                missing_parent_sessions_count += 1

    # Map exclusive request metrics to sessions
    session_nodes: Dict[str, Dict[str, Any]] = {}
    for s_id, meta in session_meta.items():
        session_nodes[s_id] = {
            "session_id": s_id,
            "parent_session_id": meta["parent_session_id"],
            "is_subagent": meta["parent_session_id"] is not None,
            "session_name": meta["session_name"],
            "project_id": meta["project_id"],
            "started_at": meta["started_at"],
            "duration_ms": meta["duration_ms"],
            # Exclusive metrics (direct ownership only)
            "exclusive_requests_count": 0,
            "exclusive_tokens": 0,
            "exclusive_fresh_input_tokens": 0,
            "exclusive_output_tokens": 0,
            "exclusive_reasoning_tokens": 0,
            "exclusive_cost_usd": 0.0,
            # Inclusive rollups (own + all children)
            "inclusive_requests_count": 0,
            "inclusive_tokens": 0,
            "inclusive_fresh_input_tokens": 0,
            "inclusive_output_tokens": 0,
            "inclusive_reasoning_tokens": 0,
            "inclusive_cost_usd": 0.0,
            "children_session_ids": [],
        }

    for req in filtered_requests:
        s_id = req.get("session_id")
        if s_id and s_id in session_nodes:
            node = session_nodes[s_id]
            node["exclusive_requests_count"] += 1
            node["exclusive_tokens"] += req.get("total_tokens") or 0
            node["exclusive_fresh_input_tokens"] += req.get("fresh_input_tokens") or 0
            node["exclusive_output_tokens"] += req.get("output_tokens") or 0
            node["exclusive_reasoning_tokens"] += req.get("reasoning_tokens") or 0
            cost = req.get("api_equivalent_usd")
            if cost is not None:
                node["exclusive_cost_usd"] += cost

    # Build adjacency tree
    children_map: Dict[str, List[str]] = {s_id: [] for s_id in session_nodes}
    roots: List[str] = []

    for s_id, node in session_nodes.items():
        p_id = node["parent_session_id"]
        if p_id and p_id in session_nodes:
            children_map[p_id].append(s_id)
            session_nodes[p_id]["children_session_ids"].append(s_id)
        else:
            roots.append(s_id)

    # Compute inclusive rollups via post-order traversal
    visited_inclusive: Set[str] = set()

    def rollup(sid: str) -> None:
        if sid in visited_inclusive:
            return
        node = session_nodes[sid]
        # Start with exclusive metrics
        node["inclusive_requests_count"] = node["exclusive_requests_count"]
        node["inclusive_tokens"] = node["exclusive_tokens"]
        node["inclusive_fresh_input_tokens"] = node["exclusive_fresh_input_tokens"]
        node["inclusive_output_tokens"] = node["exclusive_output_tokens"]
        node["inclusive_reasoning_tokens"] = node["exclusive_reasoning_tokens"]
        node["inclusive_cost_usd"] = node["exclusive_cost_usd"]

        for c_id in children_map.get(sid, []):
            rollup(c_id)
            c_node = session_nodes[c_id]
            node["inclusive_requests_count"] += c_node["inclusive_requests_count"]
            node["inclusive_tokens"] += c_node["inclusive_tokens"]
            node["inclusive_fresh_input_tokens"] += c_node["inclusive_fresh_input_tokens"]
            node["inclusive_output_tokens"] += c_node["inclusive_output_tokens"]
            node["inclusive_reasoning_tokens"] += c_node["inclusive_reasoning_tokens"]
            node["inclusive_cost_usd"] += c_node["inclusive_cost_usd"]

        visited_inclusive.add(sid)

    for r_id in roots:
        rollup(r_id)

    subagent_count = sum(1 for n in session_nodes.values() if n["is_subagent"])

    return {
        "root_nodes": [session_nodes[r_id] for r_id in roots],
        "all_nodes": session_nodes,
        "root_sessions_count": len(roots),
        "subagent_sessions_count": subagent_count,
        "missing_parent_sessions_count": missing_parent_sessions_count,
    }


def group_prompts_attribution(
    conn: sqlite3.Connection,
    filtered_requests: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Groups request tokens and costs by user prompt, guaranteeing no double-counting."""
    prompt_ids = list({r["prompt_id"] for r in filtered_requests if r.get("prompt_id")})
    prompt_meta: Dict[str, Dict[str, Any]] = {}

    if prompt_ids:
        placeholders = ",".join("?" for _ in prompt_ids)
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT prompt_id, session_id, prompt_group, created_at, metadata_json
            FROM prompts
            WHERE prompt_id IN ({placeholders})
            """,
            prompt_ids,
        )
        for r in cur.fetchall():
            prompt_meta[r[0]] = {
                "prompt_id": r[0],
                "session_id": r[1],
                "prompt_group": r[2] or "default",
                "created_at": r[3],
                "metadata": _safe_json_loads(r[4]),
            }

    prompt_buckets: Dict[str, Dict[str, Any]] = {}
    unlinked_requests_count = 0
    unlinked_tokens = 0

    for req in filtered_requests:
        p_id = req.get("prompt_id")
        if not p_id:
            unlinked_requests_count += 1
            unlinked_tokens += req.get("total_tokens") or 0
            continue

        meta = prompt_meta.get(p_id, {
            "prompt_id": p_id,
            "session_id": req.get("session_id"),
            "prompt_group": "unregistered",
            "created_at": req.get("request_timestamp"),
            "metadata": {},
        })

        if p_id not in prompt_buckets:
            prompt_buckets[p_id] = {
                "prompt_id": p_id,
                "session_id": meta["session_id"],
                "prompt_group": meta["prompt_group"],
                "created_at": meta["created_at"],
                "requests_count": 0,
                "total_tokens": 0,
                "fresh_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": 0.0,
            }

        b = prompt_buckets[p_id]
        b["requests_count"] += 1
        b["total_tokens"] += req.get("total_tokens") or 0
        b["fresh_input_tokens"] += req.get("fresh_input_tokens") or 0
        b["output_tokens"] += req.get("output_tokens") or 0
        b["reasoning_tokens"] += req.get("reasoning_tokens") or 0
        cost = req.get("api_equivalent_usd")
        if cost is not None:
            b["cost_usd"] += cost

    # Categories and parent links must be explicitly declared, never inferred from
    # session ancestry, model, request order or the presence of reasoning tokens.
    declarations: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        if event.get("event_kind") == "prompt_attribution":
            declarations.setdefault(event.get("canonical_id"), []).append(event)
    categories = ("primary", "tool_followup", "subagent", "unknown")
    for bucket in prompt_buckets.values():
        bucket["parent_prompt_id"] = prompt_meta.get(bucket["prompt_id"], {}).get("metadata", {}).get("parent_prompt_id")
        bucket["categories"] = {category: {"requests_count": 0, "total_tokens": 0, "reasoning_tokens": 0}
                                for category in categories}
        bucket["reasoning_basis"] = "nonadditive_subset_of_output"
        bucket["exclusive_requests_count"] = bucket["requests_count"]
        bucket["exclusive_tokens"] = bucket["total_tokens"]
    unknown_categories = 0
    for request in filtered_requests:
        bucket = prompt_buckets.get(request.get("prompt_id"))
        records = declarations.get(request.get("canonical_id"), [])
        declared = {
            record["details"].get("prompt_category")
            for record in records
            if record["details"].get("prompt_id") == request.get("prompt_id")
        }
        category = next(iter(declared)) if len(declared) == 1 else "unknown"
        if category not in categories:
            category = "unknown"
        if category == "unknown" or bucket is None:
            unknown_categories += 1
        if bucket:
            target = bucket["categories"][category]
            target["requests_count"] += 1
            target["total_tokens"] += request.get("total_tokens") or 0
            target["reasoning_tokens"] += request.get("reasoning_tokens") or 0
    for bucket in prompt_buckets.values():
        ancestors = {bucket["prompt_id"]}
        parent = bucket["parent_prompt_id"]
        status = "root" if parent is None else "resolved"
        while parent is not None:
            if not isinstance(parent, str) or parent not in prompt_buckets:
                status = "missing_parent_in_filtered_scope"
                break
            if parent in ancestors:
                status = "cycle"
                break
            ancestors.add(parent)
            parent = prompt_buckets[parent]["parent_prompt_id"]
        bucket["parent_link_status"] = status
    # Resolved children are indexed by parent once so each closure walks edges, not every prompt.
    resolved_children: Dict[str, List[str]] = {}
    for child in prompt_buckets.values():
        if child["parent_link_status"] == "resolved":
            resolved_children.setdefault(child["parent_prompt_id"], []).append(child["prompt_id"])
    for bucket in prompt_buckets.values():
        members = {bucket["prompt_id"]}
        pending = [bucket["prompt_id"]]
        while pending:
            parent = pending.pop()
            for child_id in resolved_children.get(parent, ()):
                if child_id not in members:
                    members.add(child_id)
                    pending.append(child_id)
        bucket["inclusive_prompt_ids"] = sorted(members)
        bucket["inclusive_requests_count"] = sum(prompt_buckets[p]["requests_count"] for p in members)
        bucket["inclusive_tokens"] = sum(prompt_buckets[p]["total_tokens"] for p in members)
        bucket["inclusive_categories"] = {
            category: {key: sum(prompt_buckets[p]["categories"][category][key] for p in members)
                       for key in ("requests_count", "total_tokens", "reasoning_tokens")}
            for category in categories
        }

    return {
        "prompts": list(prompt_buckets.values()),
        "total_prompts_count": len(prompt_buckets),
        "unlinked_requests_count": unlinked_requests_count,
        "unlinked_tokens": unlinked_tokens,
        "unknown_category_requests_count": unknown_categories,
        "reasoning_basis": "nonadditive_subset_of_output",
        "totals_basis": "exclusive_requests_once_inclusive_prompt_rows_must_not_be_added",
    }


def _token_count(value: Any) -> Optional[int]:
    """Accept explicit nonnegative integral measurements, never bools or strings."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def calculate_context_occupancy(
    filtered_requests: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Keep explicit source measurements separate from disjoint billed-input proxies.

    Context events are normalized source evidence, not a claim that either current
    native adapter emits occupancy. Model catalog limits are not reported windows.
    """
    evidence: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        if event.get("event_kind") == "context":
            evidence.setdefault(event.get("canonical_id"), []).append(event)
    history: List[Dict[str, Any]] = []
    cumulative: Dict[str, int] = {}
    turn_counts: Dict[Any, int] = {}
    input_keys = ("fresh_input_tokens", "cache_read_tokens", "cache_write_5m_tokens",
                  "cache_write_1h_tokens", "cache_write_unknown_ttl_tokens")
    for request in sorted(filtered_requests, key=lambda r: (r.get("request_timestamp") or "", r.get("canonical_id") or "")):
        sid = request.get("session_id")
        values = [_token_count(request.get(key)) for key in input_keys]
        present = [value for value in values if value is not None]
        proxy = sum(present) if present else None
        cumulative[sid] = cumulative.get(sid, 0) + (proxy or 0)
        records = evidence.get(request.get("canonical_id"), [])
        # Multiple differing measurements have no unambiguous current value.
        measurements = {
            (_token_count(e["details"].get("reported_context_tokens")),
             _token_count(e["details"].get("context_window_size")))
            for e in records
        }
        occupancy, window = next(iter(measurements)) if len(measurements) == 1 else (None, None)
        window = window if window else None
        turn_index = turn_counts.get(sid, 0)
        history.append({
            "canonical_id": request.get("canonical_id"), "session_id": sid,
            "turn_index": turn_index,
            "timestamp": request.get("request_timestamp"), "model": request.get("normalized_model"),
            "reported_context_tokens": occupancy, "context_window_size": window,
            "occupancy_basis": "explicit_source_context_event" if occupancy is not None else "unavailable",
            "evidence_event_ids": [event["event_id"] for event in records],
            "conflicting_measurements": len(measurements) > 1,
            "occupancy_ratio": occupancy / window if occupancy is not None and window else None,
            "remaining_tokens": max(0, window - occupancy) if occupancy is not None and window else None,
            "input_volume_proxy_tokens": proxy,
            "input_volume_proxy_basis": "sum_available_disjoint_input_counters",
            "input_counter_coverage": len(present),
            "cumulative_input_proxy": cumulative[sid],
            "input_proxy_growth_tokens": proxy,
        })
        turn_counts[sid] = turn_index + 1
    measured = [turn for turn in history if turn["reported_context_tokens"] is not None]
    ratios = [turn["occupancy_ratio"] for turn in history if turn["occupancy_ratio"] is not None]
    windows = {turn["context_window_size"] for turn in history if turn["context_window_size"]}
    headrooms = [turn["remaining_tokens"] for turn in history if turn["remaining_tokens"] is not None]
    return {
        "window_size": next(iter(windows)) if len(windows) == 1 else None,
        "peak_occupancy_tokens": max((turn["reported_context_tokens"] for turn in measured), default=None),
        "peak_occupancy_ratio": max(ratios, default=None),
        "remaining_headroom_tokens": min(headrooms, default=None),
        "cumulative_input_tokens": sum(cumulative.values()),
        "cumulative_input_basis": "sum_available_disjoint_input_counters_not_occupancy",
        "avg_context_occupancy_ratio": sum(ratios) / len(ratios) if ratios else None,
        "per_turn_history": history,
        "reported_occupancy_requests_count": len(measured),
        "unobserved_occupancy_requests_count": len(history) - len(measured),
        "unobserved_window_requests_count": sum(turn["context_window_size"] is None for turn in history),
    }


def analyze_carry_forward(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Only an explicit context-carry event establishes that output was reused."""
    by_id = {event["event_id"]: event for event in events}
    rows = []
    for event in events:
        if event.get("event_kind") != "context_carry":
            continue
        source_id = event.get("parent_event_id")
        source = by_id.get(source_id)
        count = _token_count(event["details"].get("carried_output_bytes"))
        rows.append({
            "event_id": event["event_id"], "canonical_id": event.get("canonical_id"),
            "source_event_id": source_id,
            "source_canonical_id": source.get("canonical_id") if source else None,
            "link_status": "resolved" if source else "missing_source_in_filtered_scope",
            "carried_output_bytes": count,
            "source_output_size_bytes": source.get("output_size_bytes") if source else None,
            "basis": "explicit_context_carry_event", "estimated_avoidable_overhead": None,
        })
    return {
        "status": "observed" if rows else "unavailable",
        "events": rows,
        "resolved_links_count": sum(row["link_status"] == "resolved" for row in rows),
        "missing_links_count": sum(row["link_status"] != "resolved" for row in rows),
        "estimated_avoidable_overhead": None,
        "disclosure": "Output size, adjacency and repeated content alone do not establish carry-forward or avoidable overhead.",
    }


_SAFE_EVENT_DETAIL_KEYS = frozenset({
    "tool_id", "tool_name", "name", "command_type", "before_tokens",
    "after_tokens", "reduction_tokens", "turns_compacted",
    "parent_tool_use_id", "is_error", "content_changed",
    "reported_context_tokens", "context_window_size", "carried_output_bytes",
    "prompt_category", "prompt_id",
    "changed_since_previous_read", "source_change_state",
})


def _sanitized_locator(raw_path: str, cache: Dict[str, str]) -> str:
    """Unhashed locator alias, memoized per analysis pass (the sanitizer is pure for a fixed environment)."""
    alias = cache.get(raw_path)
    if alias is None:
        alias = cache[raw_path] = sanitize_file_locator(raw_path, hash_locator=False)
    return alias


def analyze_events(
    conn: sqlite3.Connection,
    filtered_requests: Sequence[Dict[str, Any]],
    opt_in_fingerprints: bool = False,
    session_level_only: bool = False,
    locator_cache: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Analyze either request-linked events or separately scoped session-level events.

    Preserves safe tool metadata without bodies or arguments.
    """
    if locator_cache is None:
        locator_cache = {}
    canonical_ids = [r["canonical_id"] for r in filtered_requests if r.get("canonical_id")]
    session_ids = list({r["session_id"] for r in filtered_requests if r.get("session_id")})

    if not canonical_ids and not session_ids:
        return {
            "all_events": [],
            "tools_breakdown": [],
            "compaction": {"status": "unavailable", "compaction_events_count": 0, "total_reduction_tokens": 0, "events": []},
            "subagent_events": [],
            "mcp_events": [],
            "skills_events": [],
            "hooks_events": [],
            "counts_by_kind": {},
        }

    clauses: List[str] = []
    params: List[Any] = []
    if session_level_only and session_ids:
        s_placeholders = ",".join("?" for _ in session_ids)
        clauses.append(f"(e.canonical_id IS NULL AND e.session_id IN ({s_placeholders}))")
        params.extend(session_ids)
    elif not session_level_only and canonical_ids:
        c_placeholders = ",".join("?" for _ in canonical_ids)
        clauses.append(f"e.canonical_id IN ({c_placeholders})")
        params.extend(canonical_ids)
    if not clauses:
        return {"all_events": [], "tools_breakdown": [], "compaction": {"status": "unavailable", "compaction_events_count": 0, "total_reduction_tokens": 0, "events": []}, "subagent_events": [], "mcp_events": [], "skills_events": [], "hooks_events": [], "counts_by_kind": {}}

    where_sql = " OR ".join(clauses)
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
        e.status,
        e.details_json
    FROM events e
    WHERE {where_sql}
    ORDER BY e.started_at ASC
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()

    events_list: List[Dict[str, Any]] = []
    counts_by_kind: Dict[str, int] = {}
    tools_map: Dict[str, Dict[str, Any]] = {}
    tool_event_names: Dict[str, str] = {}
    compaction_events: List[Dict[str, Any]] = []
    subagent_events: List[Dict[str, Any]] = []
    mcp_events: List[Dict[str, Any]] = []
    skills_events: List[Dict[str, Any]] = []
    hooks_events: List[Dict[str, Any]] = []

    for r in rows:
        kind = (r[4] or "unknown").lower()
        name = r[5] or "unknown"
        details = _safe_json_loads(r[11])
        # Strictly sanitize: never preserve bodies, commands, or outputs in trace views.
        safe_details = {k: v for k, v in details.items() if k in _SAFE_EVENT_DETAIL_KEYS}
        raw_path = details.get("path")
        if isinstance(raw_path, str) and raw_path:
            safe_details["path"] = _sanitized_locator(raw_path, locator_cache)
        if opt_in_fingerprints:
            digest = next(
                (details.get(key) for key in ("content_fingerprint", "source_content_digest", "content_digest")
                 if details.get(key) is not None),
                None,
            )
            if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower()):
                safe_details["content_fingerprint"] = digest.lower()

        ev = {
            "event_id": r[0],
            "session_id": r[1],
            "canonical_id": r[2],
            "parent_event_id": r[3],
            "event_kind": kind,
            "event_name": name,
            "started_at": r[6],
            "ended_at": r[7],
            "duration_ms": r[8],
            "output_size_bytes": r[9],
            "status": r[10],
            "details": safe_details,
        }
        events_list.append(ev)
        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1

        # Classify event categories
        if kind in {"tool", "tool_use", "tool_call", "toolcall"}:
            if name not in tools_map:
                tools_map[name] = {
                    "tool_name": name,
                    "event_kind": kind,
                    "invocations_count": 0,
                    "timed_invocations_count": 0,
                    "total_duration_ms": 0,
                    "total_output_size_bytes": 0,
                    "output_size_observed_count": 0,
                    "success_count": 0,
                    "error_count": 0,
                    "status_unobserved_count": 0,
                }
            t_rec = tools_map[name]
            t_rec["invocations_count"] += 1
            tool_event_names[r[0]] = name
            if r[8] is not None:
                t_rec["timed_invocations_count"] += 1
                t_rec["total_duration_ms"] += r[8]

        elif kind in {"compaction", "context_compaction", "truncate"}:
            before = details.get("before_tokens")
            after = details.get("after_tokens")
            reduction = details.get("reduction_tokens")
            ratio = None
            if before is not None and after is not None and before > 0:
                reduction = before - after
                ratio = round(reduction / before, 4)

            compaction_events.append({
                "event_id": r[0],
                "session_id": r[1],
                "timestamp": r[6],
                "before_tokens": before,
                "after_tokens": after,
                "reduction_tokens": reduction,
                "reduction_ratio": ratio,
                "turns_compacted": details.get("turns_compacted"),
            })

        elif kind in {"subagent", "sub_agent", "agent_spawn"}:
            subagent_events.append(ev)
        elif kind in {"mcp", "mcp_tool", "mcp_call"}:
            mcp_events.append(ev)
        elif kind in {"skill", "skill_use"}:
            skills_events.append(ev)
        elif kind in {"hook", "hook_execution"}:
            hooks_events.append(ev)

    linked_output_parents: Set[str] = set()
    result_status_by_parent: Dict[str, str] = {}
    for event in events_list:
        parent_id = event.get("parent_event_id")
        if event.get("event_kind") != "tool_result" or parent_id not in tool_event_names:
            continue
        if event.get("output_size_bytes") is not None:
            tool = tools_map[tool_event_names[parent_id]]
            tool["total_output_size_bytes"] += event["output_size_bytes"]
            tool["output_size_observed_count"] += 1
            linked_output_parents.add(parent_id)
        result_status = event.get("status")
        if result_status and (result_status == "error" or parent_id not in result_status_by_parent):
            result_status_by_parent[parent_id] = result_status

    for event in events_list:
        event_id = event.get("event_id")
        if event_id not in tool_event_names:
            continue
        tool = tools_map[tool_event_names[event_id]]
        if event_id not in linked_output_parents and event.get("output_size_bytes") is not None:
            tool["total_output_size_bytes"] += event["output_size_bytes"]
            tool["output_size_observed_count"] += 1
        status = result_status_by_parent.get(event_id, event.get("status"))
        if status in {"ok", "success", "complete", "completed"}:
            tool["success_count"] += 1
        elif status:
            tool["error_count"] += 1
        else:
            tool["status_unobserved_count"] += 1

    compaction_data = {
        "status": "available" if compaction_events else "unavailable",
        "compaction_events_count": len(compaction_events),
        "total_reduction_tokens": sum(e["reduction_tokens"] for e in compaction_events if e.get("reduction_tokens") is not None),
        "events": compaction_events,
    }

    return {
        "all_events": events_list,
        "tools_breakdown": list(tools_map.values()),
        "compaction": compaction_data,
        "subagent_events": subagent_events,
        "mcp_events": mcp_events,
        "skills_events": skills_events,
        "hooks_events": hooks_events,
        "counts_by_kind": counts_by_kind,
    }


def detect_retry_loops(
    filtered_requests: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Identifies retry attempts and loop patterns.

    Classified strictly as suspected signals with source evidence links, never proven waste.
    """
    signals: List[Dict[str, Any]] = []
    retry_requests_count = 0
    retry_tokens = 0

    # 1. Analyze request-level retry attempts
    for r in filtered_requests:
        retry_num = r.get("retry_attempt") or 0
        status = r.get("request_status")
        tokens = r.get("total_tokens")

        if retry_num > 0 or status in {"retry", "error", "rate_limit", "rate_limited", "timeout", "interrupted", "failed"}:
            retry_requests_count += 1
            if tokens is not None:
                retry_tokens += tokens
            signals.append({
                "type": "retry_attempt",
                "canonical_id": r.get("canonical_id"),
                "session_id": r.get("session_id"),
                "retry_attempt": retry_num,
                "request_status": status,
                "tokens": tokens,
                "classification": "suspected_signal",
                "evidence": f"Request status '{status}' with retry_attempt={retry_num}",
            })

    # 2. Analyze consecutive tool error loops
    tool_events = [e for e in events if e.get("event_kind") in {"tool", "tool_use", "tool_call"}]
    for i in range(1, len(tool_events)):
        prev = tool_events[i - 1]
        curr = tool_events[i]
        if (
            prev.get("event_name") == curr.get("event_name")
            and prev.get("session_id") == curr.get("session_id")
            and curr.get("status") in {"error", "timeout", "failed"}
        ):
            signals.append({
                "type": "repeated_tool_failure",
                "event_id": curr.get("event_id"),
                "session_id": curr.get("session_id"),
                "tool_name": curr.get("event_name"),
                "status": curr.get("status"),
                "classification": "suspected_signal",
                "evidence": f"Sequential failure of tool '{curr.get('event_name')}'",
            })

    return {
        "retry_sequences_count": len(signals),
        "retry_requests_count": retry_requests_count,
        "retry_tokens": retry_tokens,
        "suspected_waste_signals": signals,
    }


def analyze_repetition(
    conn: sqlite3.Connection,
    events: Sequence[Dict[str, Any]],
    opt_in_fingerprints: bool = False,
    locator_cache: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Reports repeated path aliases, classifying content only from explicit evidence."""
    del conn  # Kept in the public signature for compatibility with the trace analyzer.
    if locator_cache is None:
        locator_cache = {}
    reads_by_alias: Dict[str, List[Dict[str, Any]]] = {}
    reads_by_event_id: Dict[Any, Dict[str, Any]] = {}

    def digest_from(details: Dict[str, Any]) -> Optional[str]:
        if not opt_in_fingerprints:
            return None
        digest = next(
            (details.get(key) for key in ("content_fingerprint", "source_content_digest", "content_digest")
             if details.get(key) is not None),
            None,
        )
        if isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower()):
            return digest.lower()
        return None

    def change_state_from(details: Dict[str, Any]) -> Optional[str]:
        state = details.get("source_change_state")
        if isinstance(state, str) and state.lower() in {"changed", "unchanged"}:
            return state.lower()
        for key in ("content_changed", "changed_since_previous_read"):
            changed = details.get(key)
            if isinstance(changed, bool):
                return "changed" if changed else "unchanged"
        return None

    for event in events:
        name = event.get("event_name") or ""
        details = event.get("details") or {}
        raw_path = details.get("path") or details.get("file")
        if (
            event.get("event_kind") != "tool_result"
            and isinstance(raw_path, str)
            and raw_path
            and any(term in name.lower() for term in ("read", "view", "cat", "open"))
        ):
            alias = _sanitized_locator(raw_path, locator_cache)
            record = {
                "event_id": event.get("event_id"),
                "digest": digest_from(details),
                "result_digest": None,
                "change_state": change_state_from(details),
            }
            reads_by_alias.setdefault(alias, []).append(record)
            if record["event_id"] is not None:
                reads_by_event_id[record["event_id"]] = record

    for event in events:
        if event.get("event_kind") != "tool_result":
            continue
        parent_id = event.get("parent_event_id")
        record = reads_by_event_id.get(parent_id)
        if record is None:
            continue
        details = event.get("details") or {}
        record["result_digest"] = digest_from(details)
        if record["change_state"] is None:
            record["change_state"] = change_state_from(details)

    signals: List[Dict[str, Any]] = []
    for alias, reads in reads_by_alias.items():
        if len(reads) < 2:
            continue
        digests = [
            read.get("result_digest") or read.get("digest")
            for read in reads
            if read.get("result_digest") or read.get("digest")
        ]
        change_states = [read["change_state"] for read in reads if read.get("change_state")]
        if opt_in_fingerprints and len(digests) == len(reads):
            evidence_state = "changed" if len(set(digests)) > 1 else "unchanged"
            evidence_basis = "source_content_digest"
        elif "changed" in change_states:
            evidence_state = "changed"
            evidence_basis = "source_change_evidence"
        elif len(change_states) == len(reads) and all(state == "unchanged" for state in change_states):
            evidence_state = "unchanged"
            evidence_basis = "source_change_evidence"
        else:
            evidence_state = "unknown"
            evidence_basis = "unknown"

        fingerprints = sorted(set(digests)) if opt_in_fingerprints else []
        signals.append({
            "type": "repeated_file_read",
            "path_alias": alias,
            "read_count": len(reads),
            "evidence_state": evidence_state,
            "evidence_basis": evidence_basis,
            "classification": "suspected_signal",
            "content_fingerprint": fingerprints[0] if len(fingerprints) == 1 else None,
            "content_fingerprints": fingerprints,
            "disclaimer": "Repeated reads are suspected repetition signals; may represent necessary re-verification or iterative inspection, not proven waste.",
        })

    return {
        "opt_in_content_fingerprints": opt_in_fingerprints,
        "repeated_reads_count": len(signals),
        "signals": signals,
    }


def analyze(
    conn: sqlite3.Connection,
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Computes comprehensive trace attribution and context metrics for the query envelope.

    Operates strictly on requests returned by token_ledger.query.filtered_request_rows.
    Returns standardized response compatible with query engine contract.
    """
    norm_query = envelope if "filters" in envelope and "contract_version" in envelope else validate_query(envelope)
    warnings: List[str] = []

    # 1. Fetch strictly filtered requests
    requests = filtered_request_rows(conn, norm_query)

    # 2. Build session hierarchy (exclusive vs inclusive)
    session_tree_data = build_session_tree(conn, requests)

    # 5. Associated trace events; content digests are projected only under explicit consent.
    # Grouping never broadens request identity; standalone events remain separate.
    opt_in_fp = is_opt_in_enabled(conn, OPTIN_CONTENT_FINGERPRINTS)
    locator_cache: Dict[str, str] = {}
    events_data = analyze_events(conn, requests, opt_in_fingerprints=opt_in_fp, locator_cache=locator_cache)
    session_events = analyze_events(conn, requests, opt_in_fingerprints=opt_in_fp, session_level_only=True, locator_cache=locator_cache)
    prompt_data = group_prompts_attribution(conn, requests, events_data["all_events"])
    context_data = calculate_context_occupancy(requests, events_data["all_events"])
    carry_forward = analyze_carry_forward(events_data["all_events"])
    if session_events["all_events"]:
        warnings.append("Session-level events are separate, unfiltered by request attributes or period, and excluded from request-scoped metrics.")

    # 6. Retry loops and error signals
    retry_data = detect_retry_loops(requests, events_data["all_events"])

    # 7. Repetition and file reads (with privacy consent gate)
    repetition_data = analyze_repetition(conn, events_data["all_events"], opt_in_fingerprints=opt_in_fp, locator_cache=locator_cache)
    if not opt_in_fp:
        warnings.append("Repetition content fingerprinting disabled (opt-in 'content_fingerprints' is inactive).")

    # Aggregate top-level values
    total_tokens = sum(r.get("total_tokens") or 0 for r in requests)
    fresh_input_tokens = sum(r.get("fresh_input_tokens") or 0 for r in requests)
    output_tokens = sum(r.get("output_tokens") or 0 for r in requests)
    reasoning_tokens = sum(r.get("reasoning_tokens") or 0 for r in requests)
    total_requests = len(requests)
    distinct_sessions = len({r["session_id"] for r in requests if r.get("session_id")})

    # Duration aggregation
    total_duration_ms = sum(r.get("duration_ms") or 0 for r in requests if r.get("duration_ms") is not None)

    # Missingness tracking
    unlinked_prompts = prompt_data["unlinked_requests_count"]
    if unlinked_prompts > 0:
        warnings.append(f"{unlinked_prompts} canonical requests are unlinked from prompts (lack prompt_id linkage).")

    # Source coverage
    tools_supported = "supported" if events_data["tools_breakdown"] else "no_tool_events_observed"
    compaction_supported = events_data["compaction"]["status"]
    mcp_supported = "supported" if events_data["mcp_events"] else "unavailable_in_current_adapters"
    skills_hooks_supported = (
        "supported" if (events_data["skills_events"] or events_data["hooks_events"]) else "unavailable_in_current_adapters"
    )

    source_coverage = {
        "tools": tools_supported,
        "session_hierarchy": "supported",
        "subagents": "supported" if events_data["subagent_events"] or session_tree_data["subagent_sessions_count"] else "no_subagent_evidence_observed",
        "prompt_grouping": "supported",
        "context_compaction": compaction_supported,
        "mcp_tools": mcp_supported,
        "skills_and_hooks": skills_hooks_supported,
        "missing_ingestion_path": (
            "Native ingestion extracts source-present tool calls/results, compaction records, and parent_event_id links. "
            "Event classes absent from native source logs are not synthesized."
        ),
    }

    # Format output rows based on group_by
    group_by = norm_query.get("group_by", ["session"])
    rows: List[Dict[str, Any]] = []

    if "session" in group_by:
        for node in session_tree_data["all_nodes"].values():
            rows.append({
                "dimension": "session",
                "session_id": node["session_id"],
                "parent_session_id": node["parent_session_id"],
                "is_subagent": node["is_subagent"],
                "exclusive_requests_count": node["exclusive_requests_count"],
                "exclusive_tokens": node["exclusive_tokens"],
                "inclusive_requests_count": node["inclusive_requests_count"],
                "inclusive_tokens": node["inclusive_tokens"],
                "duration_ms": node["duration_ms"],
            })
    elif "prompt" in group_by:
        for p in prompt_data["prompts"]:
            rows.append({
                "dimension": "prompt",
                "prompt_id": p["prompt_id"],
                "prompt_group": p["prompt_group"],
                "requests_count": p["requests_count"],
                "total_tokens": p["total_tokens"],
                "cost_usd": p["cost_usd"],
            })
    elif "tool" in group_by:
        for t in events_data["tools_breakdown"]:
            rows.append({
                "dimension": "tool",
                "tool_name": t["tool_name"],
                "invocations_count": t["invocations_count"],
                "total_duration_ms": t["total_duration_ms"],
                "total_output_size_bytes": t["total_output_size_bytes"],
                "success_count": t["success_count"],
                "error_count": t["error_count"],
            })
    else:
        # Default chronological trace execution timeline
        for ev in events_data["all_events"]:
            rows.append({
                "dimension": "event",
                "event_id": ev["event_id"],
                "session_id": ev["session_id"],
                "canonical_id": ev["canonical_id"],
                "event_kind": ev["event_kind"],
                "event_name": ev["event_name"],
                "started_at": ev["started_at"],
                "duration_ms": ev["duration_ms"],
                "status": ev["status"],
            })

    values = {
        "total_requests": total_requests,
        "total_sessions": distinct_sessions,
        "root_sessions_count": session_tree_data["root_sessions_count"],
        "subagent_sessions_count": session_tree_data["subagent_sessions_count"],
        "total_prompts_count": prompt_data["total_prompts_count"],
        "total_tokens": total_tokens,
        "fresh_input_tokens": fresh_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_events": len(events_data["all_events"]),
        "tool_calls_count": sum(t["invocations_count"] for t in events_data["tools_breakdown"]),
        "subagent_calls_count": len(events_data["subagent_events"]),
        "mcp_calls_count": len(events_data["mcp_events"]),
        "skills_count": len(events_data["skills_events"]),
        "hooks_count": len(events_data["hooks_events"]),
        "compaction_events_count": events_data["compaction"]["compaction_events_count"],
        "retry_requests_count": retry_data["retry_requests_count"],
        "retry_tokens": retry_data["retry_tokens"],
        "suspected_repetition_count": repetition_data["repeated_reads_count"],
        "cumulative_input_tokens": context_data["cumulative_input_tokens"],
        "max_context_occupancy_ratio": context_data["peak_occupancy_ratio"],
        "avg_context_occupancy_ratio": context_data["avg_context_occupancy_ratio"],
        "total_duration_ms": total_duration_ms,
    }
    event_ids = {event["event_id"] for event in events_data["all_events"]}
    unresolved_parent_event_count = sum(
        1 for event in events_data["all_events"]
        if event.get("parent_event_id") and event["parent_event_id"] not in event_ids
    )

    return {
        "contract_version": "1",
        "query": norm_query,
        "metric_id": norm_query.get("metric_id") or "traces",
        "family": "traces",
        "values": values,
        "rows": rows,
        "session_tree": session_tree_data["root_nodes"],
        "session_nodes": list(session_tree_data["all_nodes"].values()),
        "prompt_groups": prompt_data["prompts"],
        "events": events_data["all_events"],
        "event_scope": "filtered_requests",
        "session_level_event_scope": "linked_sessions_unfiltered_by_request_or_time",
        "session_level_events": session_events["all_events"],
        "session_level_analysis": {
            key: value for key, value in session_events.items() if key != "all_events"
        },
        "event_counts_by_kind": events_data["counts_by_kind"],
        "tools_breakdown": events_data["tools_breakdown"],
        "subagent_events": events_data["subagent_events"],
        "mcp_events": events_data["mcp_events"],
        "skills_events": events_data["skills_events"],
        "hooks_events": events_data["hooks_events"],
        "compaction": events_data["compaction"],
        "retry_loops": retry_data,
        "repetition_signals": repetition_data,
        "source_coverage": source_coverage,
        "context_occupancy": context_data,
        "carry_forward": carry_forward,
        "prompt_attribution": {key: value for key, value in prompt_data.items() if key != "prompts"},
        "eligible": {
            "total_filtered_requests": total_requests,
            "requests_with_prompts": total_requests - unlinked_prompts,
            "timed_requests": sum(1 for r in requests if r.get("timing_basis") != "none"),
            "sessions_in_scope": distinct_sessions,
        },
        "observed": {
            "events_observed": len(events_data["all_events"]),
            "tool_calls_observed": sum(t["invocations_count"] for t in events_data["tools_breakdown"]),
            "subagent_sessions_observed": session_tree_data["subagent_sessions_count"],
            "compaction_events_observed": events_data["compaction"]["compaction_events_count"],
        },
        "missingness": {
            "unlinked_prompt_requests_count": unlinked_prompts,
            "unlinked_prompt_tokens": prompt_data["unlinked_tokens"],
            "unknown_prompt_category_requests_count": prompt_data["unknown_category_requests_count"],
            "missing_parent_sessions_count": session_tree_data["missing_parent_sessions_count"],
            "missing_event_timing_count": sum(1 for e in events_data["all_events"] if e.get("duration_ms") is None),
            "unresolved_parent_events_count": unresolved_parent_event_count,
            "unobserved_context_window_requests_count": context_data["unobserved_window_requests_count"],
        },
        "warnings": warnings,
    }
