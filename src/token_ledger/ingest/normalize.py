"""Source observation normalization for Claude Code and Oh-My-Pi logs.

Normalizes directly from native JSON (not legacy lossy parser), preserving:
1. Reported counters with strict null vs. 0 distinction (missing is None, not 0).
2. Unknown TTL cache-write tokens separated from 5m and 1h tiers (unknownTTL not 5m).
3. Reasoning tokens as a reported non-additive subset of output tokens.
4. Reported total tokens preserved as reported by provider/source.
5. Raw timing basis, duration_ms, ttft_ms, generation duration, and generation speed TPS.
6. Native tool calls, results, compaction, and observed parent-event links.
7. Scoped canonical request identity and request-observation linking.
8. Safe non-secret accounts and metadata allowlisting.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
from token_ledger.ingest.events import extract_native_events

try:
    from token_ledger.privacy import (
        sanitize_account_alias as _privacy_sanitize_account,
        sanitize_file_locator as _privacy_sanitize_locator,
        sanitize_metadata as _privacy_sanitize_metadata,
    )
except ImportError:
    # Graceful fallback if privacy module not loaded
    def _privacy_sanitize_account(acc: Optional[str], default: str = "default") -> str:
        return (acc or default).strip()

    def _privacy_sanitize_locator(loc: str, **kwargs: Any) -> str:
        digest = hashlib.sha256((loc or "").encode("utf-8")).hexdigest()[:16]
        return f"loc_{digest}"

    def _privacy_sanitize_metadata(meta: Any, **kwargs: Any) -> Any:
        return meta

def _safe_sanitize_locator(loc: str) -> str:
    if loc and str(loc).startswith("loc_"):
        return str(loc)
    return _privacy_sanitize_locator(loc, hash_locator=True)


PARSER_VERSION = "1.0"
NORMALIZATION_RULE_VERSION = "1.0"
MAX_SPEED_DURATION_MS = 600000  # 600 seconds (10 minutes)
SPEED_MAX_SECS = 600.0


@dataclass
class NormalizedObservationRecord:
    """Complete bundle resulting from single log observation normalization.

    Maps directly to SQLite schema entities:
    - observation -> source_observations
    - canonical_request -> canonical_requests (Optional)
    - normalized_usage -> normalized_usage (Optional)
    - events -> events (List)
    - link -> request_observation_links (Optional)
    """
    observation: Dict[str, Any]
    canonical_request: Optional[Dict[str, Any]] = None
    normalized_usage: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    link: Optional[Dict[str, Any]] = None
    parse_status: str = "ok"
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert record bundle to clean dictionary representation."""
        return {
            "observation": self.observation,
            "canonical_request": self.canonical_request,
            "normalized_usage": self.normalized_usage,
            "events": self.events,
            "link": self.link,
            "parse_status": self.parse_status,
            "error_message": self.error_message,
        }


def parse_timestamp(raw_ts: Any) -> Tuple[str, Optional[str]]:
    """Parse raw timestamp into standard ISO-8601 UTC string and raw string.

    Returns:
        (iso_utc_string, raw_timestamp_string)
    """
    if raw_ts is None:
        now = datetime.now(timezone.utc).isoformat()
        return now, None

    raw_str = str(raw_ts).strip()

    # Check for integer/float epoch timestamp (e.g. 1792927200000 or 1792927200)
    if isinstance(raw_ts, (int, float)) or raw_str.isdigit():
        try:
            val = float(raw_ts)
            if val > 1e11:  # Epoch in milliseconds
                dt = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
            else:  # Epoch in seconds
                dt = datetime.fromtimestamp(val, tz=timezone.utc)
            return dt.isoformat(), raw_str
        except (ValueError, OverflowError, OSError):
            pass

    # Try ISO-8601 string parsing
    try:
        # Standard library handles e.g. '2026-10-25T10:00:00Z' or '+00:00'
        iso_candidate = raw_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(), raw_str
    except (ValueError, TypeError):
        pass

    # Unparseable / corrupt timestamp: return fallback current time and retain raw
    fallback = datetime.now(timezone.utc).isoformat()
    return fallback, raw_str


def _derive_observation_id(
    source_adapter: str,
    file_locator: str,
    file_generation: Optional[str],
    line_index: Optional[int],
    event_id: Optional[str],
    raw_timestamp: Optional[str],
) -> str:
    """Generate deterministic, unique observation ID for deduplication and provenance."""
    digest_input = (
        f"{source_adapter}|{file_locator}|{file_generation or ''}|"
        f"{line_index if line_index is not None else ''}|{event_id or ''}|"
        f"{raw_timestamp or ''}"
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:24]
    return f"obs_{digest}"


def _derive_project_id(cwd: Optional[str]) -> Optional[str]:
    """Derive clean project name or ID from working directory path."""
    if not cwd or not isinstance(cwd, str):
        return None
    cleaned = cwd.strip().rstrip("/\\")
    if not cleaned:
        return None
    return os.path.basename(cleaned) or "default"


def _basename_parent_ref(value: Any) -> Any:
    """Keep only the final path segment of a parent session reference.

    Session ids pass through unchanged. Path-like values lose their directories,
    for both POSIX and Windows separators. None and non-strings are returned as-is.
    """
    if not isinstance(value, str) or not value:
        return value
    stripped = value.rstrip("/\\")
    if not stripped:
        return None
    return re.split(r"[\\/]", stripped)[-1] or None


def normalize_claude_record(
    raw_record: Union[dict, str],
    file_locator: str = "claude.jsonl",
    line_index: Optional[int] = None,
    file_generation: Optional[str] = None,
    session_context: Optional[dict] = None,
) -> NormalizedObservationRecord:
    """Normalize a native Claude Code JSON log line into canonical entities."""
    now_utc = datetime.now(timezone.utc).isoformat()
    sanitized_locator = _safe_sanitize_locator(file_locator)
    loc_digest = hashlib.sha256(file_locator.encode("utf-8")).hexdigest()[:8]

    # 1. Parse JSON if string was passed
    if isinstance(raw_record, str):
        try:
            record_dict = json.loads(raw_record)
        except Exception as exc:
            obs_id = _derive_observation_id("claude_code", sanitized_locator, file_generation, line_index, None, None)
            obs = {
                "observation_id": obs_id,
                "source_adapter": "claude_code",
                "origin_harness": "claude-code",
                "account_alias": f"anon_claude_code_{loc_digest}",
                "file_locator": sanitized_locator,
                "file_generation": file_generation,
                "line_index": line_index,
                "event_id": None,
                "observed_at": now_utc,
                "raw_timestamp": None,
                "parser_version": PARSER_VERSION,
                "raw_counters_json": "{}",
                "source_reported_total": None,
                "provenance_json": json.dumps({
                    "error_kind": type(exc).__name__,
                    "line_index": line_index,
                    "file_locator": sanitized_locator,
                }),
                "parse_status": "corrupt",
                "error_message": f"Invalid JSON syntax: {type(exc).__name__}",
                "created_at": now_utc,
            }
            return NormalizedObservationRecord(
                observation=obs,
                parse_status="corrupt",
                error_message=f"Invalid JSON: {type(exc).__name__}",
            )
    elif isinstance(raw_record, dict):
        record_dict = raw_record
    else:
        obs_id = _derive_observation_id("claude_code", sanitized_locator, file_generation, line_index, None, None)
        obs = {
            "observation_id": obs_id,
            "source_adapter": "claude_code",
            "origin_harness": "claude-code",
            "account_alias": "default",
            "file_locator": sanitized_locator,
            "file_generation": file_generation,
            "line_index": line_index,
            "event_id": None,
            "observed_at": now_utc,
            "raw_timestamp": None,
            "parser_version": PARSER_VERSION,
            "raw_counters_json": "{}",
            "source_reported_total": None,
            "provenance_json": "{}",
            "parse_status": "corrupt",
            "error_message": "Expected dict or json str",
            "created_at": now_utc,
        }
        return NormalizedObservationRecord(
            observation=obs,
            parse_status="corrupt",
            error_message="Expected dict or json str",
        )

    # 2. Extract base metadata
    entry_type = record_dict.get("type")
    raw_ts = record_dict.get("timestamp")
    observed_at, raw_ts_str = parse_timestamp(raw_ts)

    raw_account = record_dict.get("account_id") or record_dict.get("account")
    has_explicit_account = bool(
        raw_account and str(raw_account).strip().lower() not in ("", "default", "unknown", "none")
    )
    if has_explicit_account:
        account_alias = _privacy_sanitize_account(raw_account, default="default")
    else:
        account_alias = f"anon_claude_code_{loc_digest}"
    message_obj = record_dict.get("message") or {}
    message_id = message_obj.get("id") if isinstance(message_obj, dict) else None
    request_id = record_dict.get("requestId")

    obs_id = _derive_observation_id(
        "claude_code",
        sanitized_locator,
        file_generation,
        line_index,
        request_id or message_id,
        raw_ts_str,
    )

    usage_dict = message_obj.get("usage") if isinstance(message_obj, dict) else None
    raw_counters_json = json.dumps(usage_dict) if usage_dict is not None else "{}"
    ctx = session_context or {}
    session_id = record_dict.get("sessionId") or record_dict.get("session_id") or ctx.get("session_id")
    native_events = extract_native_events(
        record_dict, adapter="claude_code", session_id=session_id
    )

    provenance = {
        "adapter": "claude_code",
        "harness": "claude-code",
        "entry_type": entry_type,
        "is_sidechain": record_dict.get("isSidechain", False),
    }

    base_obs = {
        "observation_id": obs_id,
        "source_adapter": "claude_code",
        "origin_harness": "claude-code",
        "account_alias": account_alias,
        "file_locator": sanitized_locator,
        "file_generation": file_generation,
        "line_index": line_index,
        "event_id": request_id or message_id or record_dict.get("id"),
        "observed_at": observed_at,
        "raw_timestamp": raw_ts_str,
        "parser_version": PARSER_VERSION,
        "raw_counters_json": raw_counters_json,
        "source_reported_total": None,
        "provenance_json": json.dumps(provenance),
        "parse_status": "ok",
        "error_message": None,
        "created_at": now_utc,
    }

    # 3. Filter non-assistant records
    if entry_type != "assistant":
        base_obs["parse_status"] = "skipped"
        return NormalizedObservationRecord(
            observation=base_obs,
            events=native_events,
            parse_status="skipped",
        )

    # Filter synthetic models
    raw_model = message_obj.get("model") if isinstance(message_obj, dict) else None
    norm_model = normalize_model_name(raw_model)
    if norm_model == "unknown" and (not raw_model or raw_model.strip() in ("<synthetic>", "unknown")):
        base_obs["parse_status"] = "skipped"
        return NormalizedObservationRecord(
            observation=base_obs,
            events=native_events,
            parse_status="skipped",
        )

    # 4. Identity Construction
    provider = "anthropic"
    fallback_id = None
    if not request_id:
        fallback_id = build_fallback_identity(
            "claude_code",
            sanitized_locator,
            line_index,
            message_id,
            file_generation,
        )

    retry_attempt = int(record_dict.get("retry_attempt", 0))
    parent_req_id = record_dict.get("parent_request_id")

    # A missing account label alone is not an overlap; cross-row candidate overlap
    # (same provider + request id) is decided by recompute_overlap_flags.
    is_unresolved = provider == "unknown"
    identity = create_canonical_identity(
        provider=provider,
        account_alias=account_alias,
        origin_harness="claude-code",
        provider_request_id=request_id,
        fallback_identity=fallback_id,
        retry_attempt=retry_attempt,
        parent_request_id=parent_req_id,
        is_unresolved_overlap=is_unresolved,
    )
    for event in native_events:
        event["canonical_id"] = identity.canonical_id

    # Session & Project context
    project_id = _derive_project_id(record_dict.get("cwd") or ctx.get("cwd"))

    # Timing: Claude Code does not report duration/ttft in message records
    timing_basis = "none"
    duration_ms = None
    ttft_ms = None
    gen_duration_ms = None
    gen_speed_tps = None

    canonical_req = {
        "canonical_id": identity.canonical_id,
        "provider": identity.provider,
        "account_alias": identity.account_alias,
        "provider_request_id": identity.provider_request_id,
        "fallback_identity": identity.fallback_identity,
        "origin_harness": identity.origin_harness,
        "access_route": "default",
        "raw_model": raw_model or "unknown",
        "normalized_model": norm_model,
        "session_id": session_id,
        "project_id": project_id,
        "prompt_id": None,
        "parent_request_id": identity.parent_request_id,
        "retry_attempt": identity.retry_attempt,
        "request_status": "ok",
        "request_timestamp": observed_at,
        "timing_basis": timing_basis,
        "duration_ms": duration_ms,
        "ttft_ms": ttft_ms,
        "generation_duration_ms": gen_duration_ms,
        "generation_speed_tps": gen_speed_tps,
        "is_unresolved_overlap": 1 if identity.is_unresolved_overlap else 0,
        "pricing_version_id": None,
        "created_at": now_utc,
        "raw_payload_json": "{}",
    }

    # 5. Extract Usage & Preserve Null vs 0
    norm_usage = None
    if isinstance(usage_dict, dict):
        fresh_in = usage_dict.get("input_tokens")
        out = usage_dict.get("output_tokens")
        cr = usage_dict.get("cache_read_input_tokens")

        cw_obj = usage_dict.get("cache_creation")
        cw5m = None
        cw1h = None
        cw_unknown = None

        if isinstance(cw_obj, dict):
            cw5m = cw_obj.get("ephemeral_5m_input_tokens")
            cw1h = cw_obj.get("ephemeral_1h_input_tokens")
            cw_unknown = usage_dict.get("unknown_ttl_tokens")
        else:
            # Cache creation breakdown absent
            # Flat cache_creation_input_tokens MUST be preserved as unknown TTL write, NOT 5m!
            if "cache_creation_input_tokens" in usage_dict:
                cw_unknown = usage_dict.get("cache_creation_input_tokens")
            elif "unknown_ttl_tokens" in usage_dict:
                cw_unknown = usage_dict.get("unknown_ttl_tokens")

        reasoning = usage_dict.get("reasoning_tokens")

        # Track missing counters
        missing_counters = []
        standard_fields = [
            ("fresh_input_tokens", fresh_in),
            ("cache_read_tokens", cr),
            ("cache_write_5m_tokens", cw5m),
            ("cache_write_1h_tokens", cw1h),
            ("cache_write_unknown_ttl_tokens", cw_unknown),
            ("output_tokens", out),
        ]
        for name, val in standard_fields:
            if val is None:
                missing_counters.append(name)

        is_partial = 1 if len(missing_counters) > 0 else 0

        norm_usage = {
            "canonical_id": identity.canonical_id,
            "fresh_input_tokens": fresh_in,
            "cache_read_tokens": cr,
            "cache_write_5m_tokens": cw5m,
            "cache_write_1h_tokens": cw1h,
            "cache_write_unknown_ttl_tokens": cw_unknown,
            "output_tokens": out,
            "reasoning_tokens": reasoning,
            "reported_total_tokens": None,
            "counter_inclusion_semantics": "disjoint",
            "normalization_rule_version": NORMALIZATION_RULE_VERSION,
            "is_partial": is_partial,
            "missing_counters_json": json.dumps(missing_counters),
        }

    # 7. Request-Observation Link
    link = {
        "canonical_id": identity.canonical_id,
        "observation_id": obs_id,
        "linked_at": now_utc,
    }

    return NormalizedObservationRecord(
        observation=base_obs,
        canonical_request=canonical_req,
        normalized_usage=norm_usage,
        events=native_events,
        link=link,
        parse_status="ok",
    )


def normalize_omp_record(
    raw_record: Union[dict, str],
    file_locator: str = "omp.jsonl",
    line_index: Optional[int] = None,
    file_generation: Optional[str] = None,
    session_context: Optional[dict] = None,
) -> NormalizedObservationRecord:
    """Normalize a native Oh-My-Pi JSON log line into canonical entities."""
    now_utc = datetime.now(timezone.utc).isoformat()
    sanitized_locator = _safe_sanitize_locator(file_locator)
    loc_digest = hashlib.sha256(file_locator.encode("utf-8")).hexdigest()[:8]

    # 1. Parse JSON if string was passed
    if isinstance(raw_record, str):
        try:
            record_dict = json.loads(raw_record)
        except Exception as exc:
            obs_id = _derive_observation_id("omp", sanitized_locator, file_generation, line_index, None, None)
            obs = {
                "observation_id": obs_id,
                "source_adapter": "omp",
                "origin_harness": "oh-my-pi",
                "account_alias": f"anon_omp_{loc_digest}",
                "file_locator": sanitized_locator,
                "file_generation": file_generation,
                "line_index": line_index,
                "event_id": None,
                "observed_at": now_utc,
                "raw_timestamp": None,
                "parser_version": PARSER_VERSION,
                "raw_counters_json": "{}",
                "source_reported_total": None,
                "provenance_json": json.dumps({
                    "error_kind": type(exc).__name__,
                    "line_index": line_index,
                    "file_locator": sanitized_locator,
                }),
                "parse_status": "corrupt",
                "error_message": f"Invalid JSON syntax: {type(exc).__name__}",
                "created_at": now_utc,
            }
            return NormalizedObservationRecord(
                observation=obs,
                parse_status="corrupt",
                error_message=f"Invalid JSON: {type(exc).__name__}",
            )
    elif isinstance(raw_record, dict):
        record_dict = raw_record
    else:
        obs_id = _derive_observation_id("omp", sanitized_locator, file_generation, line_index, None, None)
        obs = {
            "observation_id": obs_id,
            "source_adapter": "omp",
            "origin_harness": "oh-my-pi",
            "account_alias": "default",
            "file_locator": sanitized_locator,
            "file_generation": file_generation,
            "line_index": line_index,
            "event_id": None,
            "observed_at": now_utc,
            "raw_timestamp": None,
            "parser_version": PARSER_VERSION,
            "raw_counters_json": "{}",
            "source_reported_total": None,
            "provenance_json": "{}",
            "parse_status": "corrupt",
            "error_message": "Expected dict or json str",
            "created_at": now_utc,
        }
        return NormalizedObservationRecord(
            observation=obs,
            parse_status="corrupt",
            error_message="Expected dict or json str",
        )

    ctx = session_context or {}
    entry_type = record_dict.get("type")
    raw_ts = record_dict.get("timestamp")
    observed_at, raw_ts_str = parse_timestamp(raw_ts)

    raw_account = record_dict.get("account") or ctx.get("account")
    has_explicit_account = bool(
        raw_account and str(raw_account).strip().lower() not in ("", "default", "unknown", "none")
    )
    if has_explicit_account:
        account_alias = _privacy_sanitize_account(raw_account, default="default")
    else:
        account_alias = f"anon_omp_{loc_digest}"
    record_id = record_dict.get("id")
    session_id = record_dict.get("sessionId") or record_dict.get("session_id") or ctx.get("session_id")
    native_events = extract_native_events(
        record_dict, adapter="omp", session_id=session_id
    )

    # Header record: type == 'session'
    if entry_type == "session":
        obs_id = _derive_observation_id("omp", sanitized_locator, file_generation, line_index, record_id, raw_ts_str)
        provenance = {
            "adapter": "omp",
            "harness": "oh-my-pi",
            "entry_type": "session",
            "session_id": record_dict.get("id"),
            "parent_session": _basename_parent_ref(record_dict.get("parentSession")),
            "cwd_alias": _derive_project_id(record_dict.get("cwd")),
        }
        obs = {
            "observation_id": obs_id,
            "source_adapter": "omp",
            "origin_harness": "oh-my-pi",
            "account_alias": account_alias,
            "file_locator": sanitized_locator,
            "file_generation": file_generation,
            "line_index": line_index,
            "event_id": record_id,
            "observed_at": observed_at,
            "raw_timestamp": raw_ts_str,
            "parser_version": PARSER_VERSION,
            "raw_counters_json": "{}",
            "source_reported_total": None,
            "provenance_json": json.dumps(provenance),
            "parse_status": "ok",
            "error_message": None,
            "created_at": now_utc,
        }
        return NormalizedObservationRecord(
            observation=obs,
            events=native_events,
            parse_status="ok",
        )

    # Message record: type == 'message'
    msg_obj = record_dict.get("message")
    if not isinstance(msg_obj, dict):
        obs_id = _derive_observation_id("omp", sanitized_locator, file_generation, line_index, record_id, raw_ts_str)
        obs = {
            "observation_id": obs_id,
            "source_adapter": "omp",
            "origin_harness": "oh-my-pi",
            "account_alias": account_alias,
            "file_locator": sanitized_locator,
            "file_generation": file_generation,
            "line_index": line_index,
            "event_id": record_id,
            "observed_at": observed_at,
            "raw_timestamp": raw_ts_str,
            "parser_version": PARSER_VERSION,
            "raw_counters_json": "{}",
            "source_reported_total": None,
            "provenance_json": json.dumps({"adapter": "omp", "entry_type": entry_type}),
            "parse_status": "skipped",
            "error_message": None,
            "created_at": now_utc,
        }
        return NormalizedObservationRecord(observation=obs, parse_status="skipped", events=native_events)

    role = msg_obj.get("role")
    usage_dict = msg_obj.get("usage") if isinstance(msg_obj.get("usage"), dict) else None
    response_id = msg_obj.get("responseId")

    obs_id = _derive_observation_id(
        "omp",
        sanitized_locator,
        file_generation,
        line_index,
        response_id or record_id or msg_obj.get("id"),
        raw_ts_str,
    )

    reported_total = usage_dict.get("totalTokens") if usage_dict else None
    raw_counters_json = json.dumps(usage_dict) if usage_dict else "{}"

    provenance = {
        "adapter": "omp",
        "harness": "oh-my-pi",
        "entry_type": "message",
        "role": role,
    }

    base_obs = {
        "observation_id": obs_id,
        "source_adapter": "omp",
        "origin_harness": "oh-my-pi",
        "account_alias": account_alias,
        "file_locator": sanitized_locator,
        "file_generation": file_generation,
        "line_index": line_index,
        "event_id": response_id or record_id or msg_obj.get("id"),
        "observed_at": observed_at,
        "raw_timestamp": raw_ts_str,
        "parser_version": PARSER_VERSION,
        "raw_counters_json": raw_counters_json,
        "source_reported_total": reported_total,
        "provenance_json": json.dumps(provenance),
        "parse_status": "ok",
        "error_message": None,
        "created_at": now_utc,
    }

    # Filter non-assistant messages
    if role != "assistant":
        base_obs["parse_status"] = "skipped"
        return NormalizedObservationRecord(
            observation=base_obs,
            events=native_events,
            parse_status="skipped",
        )

    raw_model = msg_obj.get("model")
    norm_model = normalize_model_name(raw_model)
    provider = msg_obj.get("provider") or "unknown"

    fallback_id = None
    if not response_id:
        fallback_id = build_fallback_identity(
            "omp",
            sanitized_locator,
            line_index,
            record_id or msg_obj.get("id"),
            file_generation,
        )

    retry_attempt = int(record_dict.get("retryAttempt", record_dict.get("retry_attempt", 0)))
    parent_req_id = record_dict.get("retryOf", record_dict.get("parent_request_id"))

    # A missing account label alone is not an overlap; cross-row candidate overlap
    # (same provider + request id) is decided by recompute_overlap_flags.
    is_unresolved = provider == "unknown"
    identity = create_canonical_identity(
        provider=provider,
        account_alias=account_alias,
        origin_harness="oh-my-pi",
        provider_request_id=response_id,
        fallback_identity=fallback_id,
        retry_attempt=retry_attempt,
        parent_request_id=parent_req_id,
        is_unresolved_overlap=is_unresolved,
    )
    for event in native_events:
        event["canonical_id"] = identity.canonical_id

    project_id = _derive_project_id(record_dict.get("cwd") or ctx.get("cwd"))

    # Timing and Speed
    duration = msg_obj.get("duration")
    ttft = msg_obj.get("ttft")
    output_tokens = usage_dict.get("output") if usage_dict else None

    timing_basis = "none"
    duration_ms = None
    ttft_ms = None
    gen_duration_ms = None
    gen_speed_tps = None

    if duration is not None:
        timing_basis = "measured"
        duration_ms = int(duration)
        if ttft is not None:
            ttft_ms = int(ttft)
            gen_duration_ms = max(0, int(duration - ttft))
            secs = gen_duration_ms / 1000.0
            if secs > 0:
                if output_tokens is not None:
                    gen_speed_tps = round(output_tokens / secs, 4)
                else:
                    gen_speed_tps = None
            elif secs == 0:
                if output_tokens is not None and output_tokens == 0:
                    gen_speed_tps = 0.0
                else:
                    gen_speed_tps = None
        else:
            # Plan 46: Source reports total duration. Generation time excludes TTFT.
            # When TTFT is absent, generation duration and generation TPS are unavailable.
            # Raw total duration is preserved in duration_ms, ttft_ms is None.
            ttft_ms = None
            gen_duration_ms = None
            gen_speed_tps = None
    canonical_req = {
        "canonical_id": identity.canonical_id,
        "provider": identity.provider,
        "account_alias": identity.account_alias,
        "provider_request_id": identity.provider_request_id,
        "fallback_identity": identity.fallback_identity,
        "origin_harness": identity.origin_harness,
        "access_route": "default",
        "raw_model": raw_model or "unknown",
        "normalized_model": norm_model,
        "session_id": session_id,
        "project_id": project_id,
        "prompt_id": None,
        "parent_request_id": identity.parent_request_id,
        "retry_attempt": identity.retry_attempt,
        "request_status": "ok",
        "request_timestamp": observed_at,
        "timing_basis": timing_basis,
        "duration_ms": duration_ms,
        "ttft_ms": ttft_ms,
        "generation_duration_ms": gen_duration_ms,
        "generation_speed_tps": gen_speed_tps,
        "is_unresolved_overlap": 1 if identity.is_unresolved_overlap else 0,
        "pricing_version_id": None,
        "created_at": now_utc,
        "raw_payload_json": "{}",
    }

    # Usage counters & Null vs. 0 preservation
    norm_usage = None
    if isinstance(usage_dict, dict):
        fresh_in = usage_dict.get("input")
        out = usage_dict.get("output")
        cr = usage_dict.get("cacheRead")

        cttl = usage_dict.get("cttl")
        cw5m = None
        cw1h = None
        cw_unknown = None

        if isinstance(cttl, dict):
            cw5m = cttl.get("ephemeral5m")
            cw1h = cttl.get("ephemeral1h")
            cw_unknown = usage_dict.get("unknownTTL")
        else:
            # cttl absent: flat cacheWrite is unknown TTL write, NOT 5m!
            if "cacheWrite" in usage_dict:
                cw_unknown = usage_dict.get("unknownTTL", usage_dict.get("cacheWrite"))
            elif "unknownTTL" in usage_dict:
                cw_unknown = usage_dict.get("unknownTTL")

        reasoning = usage_dict.get("reasoning")

        missing_counters = []
        standard_fields = [
            ("fresh_input_tokens", fresh_in),
            ("cache_read_tokens", cr),
            ("cache_write_5m_tokens", cw5m),
            ("cache_write_1h_tokens", cw1h),
            ("cache_write_unknown_ttl_tokens", cw_unknown),
            ("output_tokens", out),
        ]
        for name, val in standard_fields:
            if val is None:
                missing_counters.append(name)

        is_partial = 1 if len(missing_counters) > 0 else 0

        norm_usage = {
            "canonical_id": identity.canonical_id,
            "fresh_input_tokens": fresh_in,
            "cache_read_tokens": cr,
            "cache_write_5m_tokens": cw5m,
            "cache_write_1h_tokens": cw1h,
            "cache_write_unknown_ttl_tokens": cw_unknown,
            "output_tokens": out,
            "reasoning_tokens": reasoning,
            "reported_total_tokens": reported_total,
            "counter_inclusion_semantics": "disjoint",
            "normalization_rule_version": NORMALIZATION_RULE_VERSION,
            "is_partial": is_partial,
            "missing_counters_json": json.dumps(missing_counters),
        }


    link = {
        "canonical_id": identity.canonical_id,
        "observation_id": obs_id,
        "linked_at": now_utc,
    }

    return NormalizedObservationRecord(
        observation=base_obs,
        canonical_request=canonical_req,
        normalized_usage=norm_usage,
        events=native_events,
        link=link,
        parse_status="ok",
    )


def normalize_observation(
    raw_record: Union[dict, str],
    source_adapter: str,
    file_locator: str,
    line_index: Optional[int] = None,
    file_generation: Optional[str] = None,
    session_context: Optional[dict] = None,
) -> NormalizedObservationRecord:
    """Normalize a raw record based on source_adapter.

    Supported adapters:
    - 'claude_code' / 'claude'
    - 'omp' / 'oh_my_pi'
    """
    adapter = (source_adapter or "").strip().lower()
    if adapter in ("claude_code", "claude"):
        return normalize_claude_record(
            raw_record=raw_record,
            file_locator=file_locator,
            line_index=line_index,
            file_generation=file_generation,
            session_context=session_context,
        )
    elif adapter in ("omp", "oh_my_pi", "oh-my-pi"):
        return normalize_omp_record(
            raw_record=raw_record,
            file_locator=file_locator,
            line_index=line_index,
            file_generation=file_generation,
            session_context=session_context,
        )
    else:
        raise ValueError(f"Unsupported source adapter: {source_adapter}")
