"""Canonical identity scoping, model normalization, and cross-source deduplication rules.

Implements WP03 identity requirements:
1. Canonical identity scoped by (provider, account_alias, provider_request_id).
2. Fallback identity based on source_adapter, file_locator, line_index, and event_id when
   provider_request_id is absent.
3. Distinct retry attempts with new request IDs remain distinct canonical requests.
4. Origin harness (e.g. claude-code, oh-my-pi) is tracked separately from observation source adapter.
5. Unknown account or provider cannot establish cross-source identity. Unresolved candidate
   overlaps are marked with is_unresolved_overlap = 1 and never silently collapsed.
6. Content or timestamp deduplication is strictly forbidden.
7. Model identity normalization strips date suffixes and effort tiers while preserving raw_model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


# Regex patterns for model normalization
_DATE_SUFFIX_PATTERN = re.compile(r"-\d{8}$|-\d{4}-\d{2}-\d{2}$")
_EFFORT_TIER_PATTERN = re.compile(r"-(?:low|medium|high)$")
_SYNTHETIC_MODELS = frozenset({"<synthetic>", "unknown", "synthetic", ""})


@dataclass(frozen=True)
class CanonicalIdentity:
    """Canonical request identity scope."""
    canonical_id: str
    provider: str
    account_alias: str
    origin_harness: str
    provider_request_id: Optional[str] = None
    fallback_identity: Optional[str] = None
    retry_attempt: int = 0
    parent_request_id: Optional[str] = None
    is_unresolved_overlap: bool = False


@dataclass(frozen=True)
class CrossSourceCandidate:
    """Candidate representation for cross-source overlap evaluation."""
    origin_harness: str
    source_adapter: str
    provider: str
    account_alias: str
    provider_request_id: Optional[str]
    fallback_identity: Optional[str] = None
    observed_at: Optional[str] = None
    raw_model: Optional[str] = None


def normalize_model_name(raw_model: Optional[str]) -> str:
    """Normalize model identifier by stripping date suffixes and effort tiers.

    Preserves base family and version. Synthetic and empty models are normalized to 'unknown'.

    Examples:
        'claude-sonnet-5-5-20251001' -> 'claude-sonnet-5-5'
        'claude-haiku-4-5-20251001'  -> 'claude-haiku-4-5'
        'gpt-6.1-sol-low'            -> 'gpt-6.1-sol'
        'gemini-3.8-flash'           -> 'gemini-3.8-flash'
        '<synthetic>'                -> 'unknown'
    """
    if not raw_model or raw_model.strip() in _SYNTHETIC_MODELS:
        return "unknown"

    model = raw_model.strip()
    # Strip date suffix: e.g. -20251001 or -2025-10-01
    model = _DATE_SUFFIX_PATTERN.sub("", model)
    # Strip effort tier: e.g. -low, -medium, -high
    model = _EFFORT_TIER_PATTERN.sub("", model)
    return model


def build_canonical_id(
    provider: str,
    account_alias: str,
    provider_request_id: Optional[str],
    fallback_identity: Optional[str] = None,
    retry_attempt: int = 0,
) -> str:
    """Derive canonical_id string.

    When provider_request_id is present:
        f"{provider}:{account_alias}:{provider_request_id}"
        If retry_attempt > 0 and the provider_request_id does not already encode retry,
        retry is qualified to guarantee uniqueness.

    When provider_request_id is absent:
        f"fallback:{fallback_identity}"
    """
    prov = (provider or "unknown").strip().lower()
    acct = (account_alias or "default").strip()

    if provider_request_id:
        req_id = provider_request_id.strip()
        # If retry_attempt > 0 and req_id doesn't already contain retry qualifier, qualify it
        if retry_attempt > 0 and f"retry_{retry_attempt}" not in req_id.lower() and f"retry{retry_attempt}" not in req_id.lower():
            return f"{prov}:{acct}:{req_id}:retry_{retry_attempt}"
        return f"{prov}:{acct}:{req_id}"

    if fallback_identity:
        return f"fallback:{fallback_identity.strip()}"

    raise ValueError("Either provider_request_id or fallback_identity must be provided to build canonical_id")


def build_fallback_identity(
    source_adapter: str,
    file_locator: str,
    line_index: Optional[int] = None,
    event_id: Optional[str] = None,
    file_generation: Optional[str] = None,
) -> str:
    """Build durable fallback identity when provider-issued request ID is absent.

    Scoped by source adapter, file locator, generation, line index, and event ID.
    Never relies on prompt content or timestamps.
    """
    adapter = (source_adapter or "unknown").strip()
    loc = (file_locator or "unknown").strip()
    gen = (file_generation or "gen0").strip()
    line = str(line_index) if line_index is not None else "noline"
    evt = (event_id or "noevent").strip()

    return f"{adapter}:{loc}:{gen}:{line}:{evt}"


def create_canonical_identity(
    provider: str,
    account_alias: str,
    origin_harness: str,
    provider_request_id: Optional[str] = None,
    fallback_identity: Optional[str] = None,
    retry_attempt: int = 0,
    parent_request_id: Optional[str] = None,
    is_unresolved_overlap: bool = False,
) -> CanonicalIdentity:
    """Factory creating CanonicalIdentity with validated canonical_id."""
    cid = build_canonical_id(
        provider=provider,
        account_alias=account_alias,
        provider_request_id=provider_request_id,
        fallback_identity=fallback_identity,
        retry_attempt=retry_attempt,
    )
    return CanonicalIdentity(
        canonical_id=cid,
        provider=(provider or "unknown").strip().lower(),
        account_alias=(account_alias or "default").strip(),
        origin_harness=origin_harness,
        provider_request_id=provider_request_id,
        fallback_identity=fallback_identity,
        retry_attempt=retry_attempt,
        parent_request_id=parent_request_id,
        is_unresolved_overlap=is_unresolved_overlap,
    )


def evaluate_cross_source_identity(
    cand_a: CrossSourceCandidate,
    cand_b: CrossSourceCandidate,
) -> Tuple[bool, bool, str]:
    """Evaluate whether two candidates from different sources/harnesses share canonical identity.

    Returns:
        (is_same_canonical, is_unresolved_overlap, reason)

    Principles:
    - Never deduplicate by content or timestamp.
    - If request IDs differ or are missing: no identity.
    - If providers differ: no identity.
    - Unknown account or unknown provider CANNOT establish cross-source identity:
      candidate overlap must be marked unresolved (is_unresolved_overlap=True) and
      the records must NOT be merged into a single canonical request.
    - Distinct accounts sharing an ID are distinct requests.
    - Verified match requires matching verified provider, matching verified non-default
      account alias, and matching provider_request_id.
    """
    # Missing request IDs on either side cannot establish cross-source identity
    if not cand_a.provider_request_id or not cand_b.provider_request_id:
        return False, False, "missing_request_id"

    # If request IDs do not match
    if cand_a.provider_request_id != cand_b.provider_request_id:
        return False, False, "different_request_ids"

    prov_a = (cand_a.provider or "unknown").strip().lower()
    prov_b = (cand_b.provider or "unknown").strip().lower()

    # Different providers
    if prov_a != prov_b:
        return False, False, "different_providers"

    # Unknown provider cannot establish cross-source identity
    if prov_a in ("unknown", "") or prov_b in ("unknown", ""):
        return False, True, "unknown_provider_cannot_establish_cross_source_identity"

    acct_a = (cand_a.account_alias or "default").strip()
    acct_b = (cand_b.account_alias or "default").strip()

    # Check if either account is unverified ('default', 'unknown', or scoped anonymous 'anon_')
    is_anon_a = acct_a in ("default", "unknown", "") or acct_a.startswith("anon_")
    is_anon_b = acct_b in ("default", "unknown", "") or acct_b.startswith("anon_")

    if is_anon_a or is_anon_b:
        # Candidate overlap exists (same provider and request ID), but account is unknown/unverified
        return False, True, "unknown_account_cannot_establish_cross_source_identity"

    # If explicit accounts are different
    if acct_a != acct_b:
        return False, False, "distinct_accounts"

    # Both provider and account alias match and are verified non-default
    return True, False, "verified_cross_source_identity"
