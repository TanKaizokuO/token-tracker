"""Native trace and event extraction module for Token Ledger (WP15).

Extracts native execution trace events, session hierarchies, prompt groupings,
and compaction records from source harness log streams (claude-code, omp).

Guarantees:
1. Tool Invocation & Result Metadata: Event IDs, parent links, timestamps, status,
   duration_ms, and output_size_bytes ONLY. Zero payload or argument bodies preserved.
2. Source-Observed Hierarchy: session.parentSessionId is a session link; Claude
   parentUuid is an event/message parent and never a session link.
3. Explicit Compaction: Captures native before/after context token counts with nullable
   semantics; unmeasured fields remain None, never defaulted to 0.
4. Categorized Event Classes: Identifies MCP, skill, and hook events strictly from verified
   source namespaces (e.g. mcp__, skill_, hook_), without inferring missing invocations.
5. Prompt Linkage without Bodies: Captures promptgroup references optionally with no text.
6. Privacy & Opt-in Fingerprints: No content hashing by default; hashes computed only
   when explicit opt-in consent is present.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from token_ledger.privacy import (
    hash_content_fingerprint,
    sanitize_file_locator,
    sanitize_metadata,
)


def _safe_iso_timestamp(ts: Any) -> str:
    """Converts unix millis, float seconds, or iso strings to canonical ISO 8601 UTC."""
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, (int, float)):
        # If timestamp is millisecond epoch (> 10^11)
        if ts > 1e11:
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        else:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, str):
        cleaned = ts.strip()
        if cleaned:
            # Validate basic ISO formatting or fallback to now
            try:
                # Handle trailing Z
                if cleaned.endswith("Z"):
                    dt = datetime.fromisoformat(cleaned[:-1] + "+00:00")
                else:
                    dt = datetime.fromisoformat(cleaned)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_event_kind(name: str) -> str:
    """Classifies event kind based strictly on verified source name prefixes/namespaces.

    Does not infer missing invocations.
    """
    clean_name = (name or "").lower().strip()
    if clean_name.startswith("mcp__") or clean_name.startswith("mcp:") or clean_name.startswith("mcp/"):
        return "mcp"
    if clean_name.startswith("skill_") or clean_name.startswith("skill:") or clean_name.startswith("skill/"):
        return "skill"
    if clean_name.startswith("hook_") or clean_name.startswith("hook:") or clean_name.startswith("hook/"):
        return "hook"
    return "tool_call"


def extract_native_events(
    raw_record: Dict[str, Any],
    adapter: str,
    canonical_id: Optional[str] = None,
    session_id: Optional[str] = None,
    opt_in_fingerprints: bool = False,
) -> List[Dict[str, Any]]:
    """Extracts verified native events (tool calls, tool results, compaction, MCP, skills, hooks).

    Strips all argument payloads and execution bodies, preserving only safe metadata.
    """
    if not isinstance(raw_record, dict):
        return []
    raw_session_id = session_id or raw_record.get("sessionId") or raw_record.get("session_id")
    if raw_session_id is None:
        candidate_session = raw_record.get("session")
        raw_session_id = candidate_session if isinstance(candidate_session, str) else None
    sess_id = str(raw_session_id) if raw_session_id is not None and str(raw_session_id) else "default_session"

    events: List[Dict[str, Any]] = []
    rec_type = raw_record.get("type") or raw_record.get("event") or ""
    rec_ts = _safe_iso_timestamp(raw_record.get("timestamp") or raw_record.get("created_at"))
    parent_event_id = raw_record.get("parent_event_id")
    if not parent_event_id and str(adapter or "").strip().lower() in {"claude", "claude_code", "claude-code"}:
        parent_event_id = raw_record.get("parentUuid")

    # -------------------------------------------------------------------------
    # 1. Compaction Events (explicit compaction records or /compact markers)
    # -------------------------------------------------------------------------
    if rec_type in {"compaction", "context_compaction", "compact"} or "compaction" in raw_record:
        comp_obj = raw_record.get("compaction") if isinstance(raw_record.get("compaction"), dict) else raw_record
        before_tok = next(
            (comp_obj.get(key) for key in ("before_tokens", "tokens_before", "context_before")
             if comp_obj.get(key) is not None),
            None,
        )
        after_tok = next(
            (comp_obj.get(key) for key in ("after_tokens", "tokens_after", "context_after")
             if comp_obj.get(key) is not None),
            None,
        )

        # Explicit nullability: unmeasured values remain None, never default to 0
        before_val = int(before_tok) if before_tok is not None else None
        after_val = int(after_tok) if after_tok is not None else None
        reduc_tok = None
        reduc_ratio = None
        if before_val is not None and after_val is not None:
            reduc_tok = before_val - after_val
            reduc_ratio = round(reduc_tok / before_val, 4) if before_val > 0 else 0.0

        details = {
            "before_tokens": before_val,
            "after_tokens": after_val,
            "reduction_tokens": reduc_tok,
            "reduction_ratio": reduc_ratio,
        }
        event_id = comp_obj.get("id") or raw_record.get("id") or f"compact_{sess_id}_{rec_ts}"
        events.append({
            "event_id": event_id,
            "session_id": sess_id,
            "canonical_id": canonical_id,
            "parent_event_id": parent_event_id,
            "event_kind": "compaction",
            "event_name": "context_compaction",
            "started_at": rec_ts,
            "ended_at": rec_ts,
            "duration_ms": comp_obj.get("duration_ms"),
            "output_size_bytes": None,
            "status": "ok",
            "details_json": json.dumps(details),
        })

    # -------------------------------------------------------------------------
    # 2. Tool Invocations and Results (Claude style or OMP style)
    # -------------------------------------------------------------------------
    # Look inside message or top-level content
    msg_obj = raw_record.get("message") if isinstance(raw_record.get("message"), dict) else raw_record
    content_blocks = msg_obj.get("content", []) if isinstance(msg_obj, dict) else []

    if isinstance(content_blocks, list):
        for idx, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                continue
            b_type = block.get("type") or ""

            # Tool Invocation: tool_use (Claude) or toolCall (OMP)
            if b_type in {"tool_use", "toolCall"}:
                t_id = block.get("id") or f"call_{len(events) + 1}_{idx}"
                t_name = block.get("name") or "unknown_tool"
                kind = classify_event_kind(t_name)

                # Extract safe metadata ONLY (e.g. file path locator if present), never payloads/args
                inputs = block.get("input") or block.get("args") or {}
                safe_details: Dict[str, Any] = {"tool_id": t_id, "tool_name": t_name}
                if isinstance(inputs, dict):
                    raw_path = inputs.get("path") or inputs.get("file") or inputs.get("filepath")
                    if raw_path and isinstance(raw_path, str):
                        safe_details["path"] = sanitize_file_locator(raw_path)

                # Content fingerprinting strictly opt-in
                if opt_in_fingerprints and "content" in inputs:
                    safe_details["content_fingerprint"] = hash_content_fingerprint(
                        str(inputs["content"]), opt_in=opt_in_fingerprints
                    )

                # Check duration if message level duration exists
                dur = msg_obj.get("duration") or raw_record.get("duration_ms")
                events.append({
                    "event_id": t_id,
                    "session_id": sess_id,
                    "canonical_id": canonical_id,
                    "parent_event_id": parent_event_id,
                    "event_kind": kind,
                    "event_name": t_name,
                    "started_at": rec_ts,
                    "ended_at": None,
                    "duration_ms": dur if isinstance(dur, (int, float)) else None,
                    "output_size_bytes": None,
                    "status": "ok",
                    "details_json": json.dumps(safe_details),
                })

            # Tool Result: tool_result (Claude) or toolResult (OMP)
            elif b_type in {"tool_result", "toolResult"}:
                t_use_id = block.get("tool_use_id") or block.get("toolCallId") or block.get("call_id")
                r_id = block.get("id") or (f"res_{t_use_id}" if t_use_id else f"res_{len(events) + 1}_{idx}")
                is_err = bool(block.get("is_error") or block.get("isError") or block.get("status") == "error")

                # Compute output size in bytes without saving the payload body
                raw_out = block.get("content") or block.get("output") or ""
                out_bytes = len(str(raw_out).encode("utf-8")) if raw_out else 0

                safe_details = {
                    "parent_tool_use_id": t_use_id,
                    "is_error": is_err,
                }
                if opt_in_fingerprints and raw_out:
                    safe_details["content_fingerprint"] = hash_content_fingerprint(
                        str(raw_out), opt_in=opt_in_fingerprints
                    )

                events.append({
                    "event_id": r_id,
                    "session_id": sess_id,
                    "canonical_id": canonical_id,
                    "parent_event_id": t_use_id or parent_event_id,  # Prefer the observed tool invocation
                    "event_kind": "tool_result",
                    "event_name": f"result_{t_use_id}" if t_use_id else "tool_result",
                    "started_at": rec_ts,
                    "ended_at": rec_ts,
                    "duration_ms": block.get("duration_ms"),
                    "output_size_bytes": out_bytes,
                    "status": "error" if is_err else "ok",
                    "details_json": json.dumps(safe_details),
                })

    # Direct top-level tool_result record (common in streaming logs)
    if rec_type in {"tool_result", "toolResult"}:
        t_use_id = raw_record.get("tool_use_id") or raw_record.get("toolCallId")
        r_id = raw_record.get("id") or (f"res_{t_use_id}" if t_use_id else f"res_{rec_ts}")
        is_err = bool(raw_record.get("is_error") or raw_record.get("isError") or raw_record.get("status") == "error")
        raw_out = raw_record.get("content") or raw_record.get("output") or ""
        out_bytes = len(str(raw_out).encode("utf-8")) if raw_out else 0

        safe_details = {
            "parent_tool_use_id": t_use_id,
            "is_error": is_err,
        }
        events.append({
            "event_id": r_id,
            "session_id": sess_id,
            "canonical_id": canonical_id,
            "parent_event_id": t_use_id or parent_event_id,
            "event_kind": "tool_result",
            "event_name": f"result_{t_use_id}" if t_use_id else "tool_result",
            "started_at": rec_ts,
            "ended_at": rec_ts,
            "duration_ms": raw_record.get("duration_ms"),
            "output_size_bytes": out_bytes,
            "status": "error" if is_err else "ok",
            "details_json": json.dumps(safe_details),
        })

    unique_events: Dict[Any, Dict[str, Any]] = {}
    for event in events:
        unique_events.setdefault(event["event_id"], event)
    return list(unique_events.values())


def extract_session_hierarchy(raw_record: Dict[str, Any], adapter: str) -> Dict[str, Any]:
    """Extracts source-observed session hierarchy.

    Guarantees: only observed session-parent fields are used for session hierarchy;
    Claude parentUuid remains an event/message parent.
    """
    if not isinstance(raw_record, dict):
        return {"session_id": None, "parent_session_id": None, "is_subagent": 0}

    sess_id = (
        raw_record.get("sessionId")
        or raw_record.get("session_id")
        or raw_record.get("session")
        or (raw_record.get("id") if raw_record.get("type") == "session" else None)
    )

    # Source-observed parent links
    parent_sess_id = (
        raw_record.get("parentSessionId")
        or raw_record.get("parentSession")
        or raw_record.get("parent_session_id")
    )

    is_subagent = 1 if (parent_sess_id and str(parent_sess_id).strip()) else 0

    return {
        "session_id": str(sess_id) if sess_id else None,
        "parent_session_id": str(parent_sess_id) if parent_sess_id else None,
        "is_subagent": is_subagent,
    }


def extract_prompt_group(raw_record: Dict[str, Any], adapter: str) -> Optional[Dict[str, Any]]:
    """Preserves promptgroup links optionally without body text, preserving privacy and chronology."""
    if not isinstance(raw_record, dict):
        return None

    # Check for prompt markers
    prompt_id = raw_record.get("promptId") or raw_record.get("prompt_id")
    prompt_group = raw_record.get("promptGroup") or raw_record.get("prompt_group")
    sess_id = raw_record.get("sessionId") or raw_record.get("session_id") or raw_record.get("session")

    # If it is a user turn message
    is_user_turn = False
    if raw_record.get("type") == "user":
        is_user_turn = True
    elif isinstance(raw_record.get("message"), dict) and raw_record["message"].get("role") == "user":
        is_user_turn = True

    if prompt_id or prompt_group or is_user_turn:
        pid = prompt_id or f"p_{raw_record.get('id') or _safe_iso_timestamp(raw_record.get('timestamp'))}"
        return {
            "prompt_id": pid,
            "session_id": sess_id or "default_session",
            "prompt_group": prompt_group,
            "created_at": _safe_iso_timestamp(raw_record.get("timestamp")),
            # No body text preserved
            "metadata_json": json.dumps({"source_adapter": adapter}),
        }

    return None
