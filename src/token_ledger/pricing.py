"""Pricing engine, dated rate cards, historical FX, and financial facts valuation.

Implements WP06 requirements:
1. Exact seven display identities: Opus 5.5, Sonnet 5.5, Haiku 5.5, Sol 6.1, Terra 6.1, Luna 6, Gemini 3.8 Flash.
2. Explicit configurable provider/model-ID/route mapping with original IDs preserved;
   unknown models remain strictly unknown.
3. Effective dated rate import and query with thresholds + per-TTL cache write pricing.
   Source, status, SHA-256 digest, and currency basis tracked.
   STRICT RULE: No latest retroactive fallback by default.
4. Schema integration for pricing_versions, model_pricing_rates, historical_fx, financial_facts.
   Separates actual billed verified facts, API-equivalent value, subscription fees,
   cache savings, and unpriced usage.
5. Historical FX with date/source/rate; unknown FX returns None, never 0.0.
6. Legacy pricing.json may be imported only as undated unverified data, never fabricated history.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from token_ledger.ingest.identity import normalize_model_name

# Exact seven display identities per rebuild plan (all76, line 12, plus Terra 6.1)
OPUS_5_5 = "Opus 5.5"
SONNET_5_5 = "Sonnet 5.5"
HAIKU_5_5 = "Haiku 5.5"
SOL_6_1 = "Sol 6.1"
TERRA_6_1 = "Terra 6.1"
LUNA_6 = "Luna 6"
LUNA_MAX = LUNA_6  # Backward-compat alias for the former display name.
GEMINI_3_8_FLASH = "Gemini 3.8 Flash"

CANONICAL_DISPLAY_IDENTITIES: Tuple[str, ...] = (
    OPUS_5_5,
    SONNET_5_5,
    HAIKU_5_5,
    SOL_6_1,
    TERRA_6_1,
    LUNA_6,
    GEMINI_3_8_FLASH,
)

# Pricing status definitions
STATUS_VERIFIED = "verified"
STATUS_ESTIMATED = "estimated"
STATUS_UNVERIFIED = "unverified"
STATUS_UNPRICED = "unpriced"
STATUS_PARTIAL = "partial"

DEFAULT_ACCESS_ROUTE = "default"
DEFAULT_CURRENCY = "USD"
DEFAULT_TARGET_CURRENCY = "INR"


@dataclass(frozen=True)
class ModelMappingRule:
    """Configurable rule mapping raw/normalized model identity and route to canonical display identity."""
    pattern: str  # Regex pattern to match raw or normalized model ID
    display_name: str  # Must be one of CANONICAL_DISPLAY_IDENTITIES or explicit display name
    provider: Optional[str] = None  # If set, provider must match (case-insensitive)
    access_route: Optional[str] = None  # If set, route must match (case-insensitive)
    canonical_model_id: Optional[str] = None  # Clean standard model ID
    # Other provider strings that carry the same model family (for example the Codex and
    # Antigravity routes that report OpenAI and Google models). Matched case-insensitively.
    provider_aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelIdentityResult:
    """Outcome of resolving a model identifier against configurable mapping rules."""
    raw_model: str
    normalized_model: str
    display_name: Optional[str]
    provider: str
    access_route: str
    is_known: bool
    canonical_model_id: Optional[str] = None


@dataclass
class PricingVersion:
    """Dated pricing schedule record."""
    version_id: str
    pricing_hash: str
    currency_code: str
    effective_start: str
    effective_end: Optional[str] = None
    source_provenance_json: str = "{}"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())


@dataclass
class ModelRate:
    """Per-model rate configuration."""
    version_id: str
    provider: str
    model_identity: str
    input_rate_per_m: float
    output_rate_per_m: float
    access_route: str = DEFAULT_ACCESS_ROUTE
    cache_read_rate_per_m: Optional[float] = None
    cache_write_5m_rate_per_m: Optional[float] = None
    cache_write_1h_rate_per_m: Optional[float] = None
    is_tiered: bool = False
    prompt_threshold: Optional[int] = None
    tiered_rates_json: str = "{}"
    status: str = STATUS_VERIFIED
    source_url: Optional[str] = None
    verified_on: Optional[str] = None
    note: Optional[str] = None
    rate_id: Optional[int] = None


@dataclass
class HistoricalFx:
    """Historical currency exchange rate record."""
    base_currency: str
    target_currency: str
    rate_per_usd: float
    effective_date: str
    source_provenance: str = "manual"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    fx_id: Optional[int] = None


@dataclass
class ValuationResult:
    """Request valuation outcome separating actual billed facts, API equivalents, and savings."""
    canonical_id: str
    pricing_version_id: Optional[str]
    actual_billed_amount: Optional[float] = None
    actual_billed_currency: Optional[str] = None
    api_equivalent_usd: Optional[float] = None
    api_equivalent_inr: Optional[float] = None
    subscription_fee_usd: Optional[float] = None
    cache_savings_usd: Optional[float] = None
    pricing_status: str = STATUS_UNPRICED
    fx_rate_applied: Optional[float] = None
    calculated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    # Diagnostic counter valuations
    fresh_cost_usd: Optional[float] = None
    output_cost_usd: Optional[float] = None
    cache_read_cost_usd: Optional[float] = None
    cache_write_5m_cost_usd: Optional[float] = None
    cache_write_1h_cost_usd: Optional[float] = None
    cache_write_unknown_ttl_cost_usd: Optional[float] = None
    unpriced_counters: List[str] = field(default_factory=list)
    card_currency: Optional[str] = None
    card_to_usd_fx: Optional[Dict[str, Any]] = None
    usd_to_target_fx: Optional[Dict[str, Any]] = None


def get_default_model_mapping_rules() -> List[ModelMappingRule]:
    """Default explicit mapping rules for the seven canonical display identities.

    Original IDs are preserved. Unmatched models remain explicitly unknown.
    """
    return [
        # 1. Opus 5.5 (direct Anthropic IDs and OpenRouter's anthropic/claude-opus-5.5)
        ModelMappingRule(
            pattern=r"^(?:anthropic/)?claude-opus-5[-.]5(?:-\d{8})?$",
            display_name=OPUS_5_5,
            provider="anthropic",
            provider_aliases=("openrouter",),
            canonical_model_id="claude-opus-5-5",
        ),
        # 2. Sonnet 5.5
        ModelMappingRule(
            pattern=r"^claude-sonnet-5-5(?:-\d{8})?$",
            display_name=SONNET_5_5,
            provider="anthropic",
            canonical_model_id="claude-sonnet-5-5",
        ),
        # 3. Haiku 5.5
        ModelMappingRule(
            pattern=r"^claude-haiku-5-5(?:-\d{8})?$",
            display_name=HAIKU_5_5,
            provider="anthropic",
            canonical_model_id="claude-haiku-5-5",
        ),
        # 4. Sol 6.1
        ModelMappingRule(
            pattern=r"^gpt-6\.1(?:-sol(?:-(?:low|medium|high))?)?$",
            display_name=SOL_6_1,
            provider="openai",
            provider_aliases=("openai-codex",),
            canonical_model_id="gpt-6.1-sol",
        ),
        # 5. Terra 6.1
        ModelMappingRule(
            pattern=r"^gpt-6\.1-terra(?:-(?:low|medium|high))?$",
            display_name=TERRA_6_1,
            provider="openai",
            provider_aliases=("openai-codex",),
            canonical_model_id="gpt-6.1-terra",
        ),
        # 6. Luna 6 (matches gpt-6-luna or luna-max variants)
        ModelMappingRule(
            pattern=r"^(?:gpt-6-luna(?:-(?:low|medium|high|max))?|luna-max)$",
            display_name=LUNA_6,
            provider="openai",
            provider_aliases=("openai-codex",),
            canonical_model_id="gpt-6-luna",
        ),
        # 7. Gemini 3.8 Flash
        ModelMappingRule(
            pattern=r"^gemini-3\.8-flash(?:-\d{8})?$",
            display_name=GEMINI_3_8_FLASH,
            provider="google",
            provider_aliases=("google-antigravity",),
            canonical_model_id="gemini-3.8-flash",
        ),
    ]


def resolve_model_identity(
    raw_model: Optional[str],
    provider: Optional[str] = None,
    access_route: Optional[str] = DEFAULT_ACCESS_ROUTE,
    custom_rules: Optional[Sequence[ModelMappingRule]] = None,
) -> ModelIdentityResult:
    """Resolve raw model and route to canonical display identity.

    Rules:
    - Original raw_model and normalized_model are strictly preserved.
    - If no rule matches, display_name is None and is_known is False.
    - Unknown models remain explicitly unknown; never guessed.
    """
    raw_str = (raw_model or "").strip()
    norm_str = normalize_model_name(raw_str)
    prov_str = (provider or "unknown").strip().lower()
    route_str = (access_route or DEFAULT_ACCESS_ROUTE).strip()

    rules = custom_rules if custom_rules is not None else get_default_model_mapping_rules()

    for rule in rules:
        if rule.provider and prov_str not in {rule.provider.lower(), *(a.lower() for a in rule.provider_aliases)}:
            continue
        if rule.access_route and rule.access_route.lower() != route_str.lower():
            continue
        # Match against raw or normalized identifier
        if re.match(rule.pattern, raw_str, re.IGNORECASE) or re.match(rule.pattern, norm_str, re.IGNORECASE):
            return ModelIdentityResult(
                raw_model=raw_str,
                normalized_model=norm_str,
                display_name=rule.display_name,
                provider=prov_str,
                access_route=route_str,
                is_known=True,
                canonical_model_id=rule.canonical_model_id or norm_str,
            )

    return ModelIdentityResult(
        raw_model=raw_str,
        normalized_model=norm_str,
        display_name=None,
        provider=prov_str,
        access_route=route_str,
        is_known=False,
        canonical_model_id=None,
    )


def resolve_versioned_model_identity(
    conn: sqlite3.Connection, raw_model: Optional[str], provider: Optional[str],
    access_route: Optional[str], timestamp: Optional[str], version_id: Optional[str] = None,
) -> ModelIdentityResult:
    """Resolve an alias in its assigned or historically effective version and scope."""
    raw = (raw_model or "").strip()
    version = get_pricing_version(conn, version_id) if version_id else (
        find_effective_pricing_version(conn, timestamp) if timestamp else None
    )
    mappings = json.loads(version.source_provenance_json).get("model_mappings", []) if version else ()
    return _resolve_model_identity_mappings(raw, provider, access_route, mappings)


def _resolve_model_identity_mappings(raw, provider, access_route, mappings):
    for mapping in mappings:
        if (mapping["provider"].casefold() == (provider or "").strip().casefold()
                and mapping["access_route"].casefold() == (access_route or DEFAULT_ACCESS_ROUTE).strip().casefold()
                and mapping["alias"].casefold() in {raw.casefold(), normalize_model_name(raw).casefold()}):
            canonical = mapping["model_identity"]
            default = resolve_model_identity(canonical, provider, access_route)
            display = mapping.get("display_name") or default.display_name
            return ModelIdentityResult(raw, normalize_model_name(raw), display,
                                       (provider or "unknown").strip().lower(),
                                       (access_route or DEFAULT_ACCESS_ROUTE).strip(),
                                       display is not None, canonical)
    return resolve_model_identity(raw, provider, access_route)


class _QueryModelIdentityResolver:
    """A selector-local snapshot; never retained across database writes/queries.

    SQLite computes Julian days for both schedules and request timestamps, keeping
    its timestamp parsing, fractional precision and tie-breaking exactly intact.
    Only identity/provenance is loaded, not rate cards or financial valuations.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._versions = tuple(conn.execute(
            "SELECT version_id, julianday(effective_start), julianday(effective_end), "
            "effective_end IS NULL, source_provenance_json FROM pricing_versions "
            "ORDER BY julianday(effective_start) DESC, created_at DESC, version_id DESC"
        ))
        self._provenance = {row[0]: row[4] for row in self._versions}
        self._mappings = {}
        self._identities = {}

    def resolve(self, raw_model, provider, access_route, timestamp_jd, version_id):
        if not version_id and timestamp_jd is not None:
            for candidate, start, end, open_end, _ in self._versions:
                if (start is not None and start <= timestamp_jd
                        and (open_end or (end is not None and timestamp_jd < end))):
                    version_id = candidate
                    break
        # An explicit missing version must not fall back to an effective one.
        key = (raw_model, provider, access_route, version_id)
        identity = self._identities.get(key)
        if identity is None:
            if version_id not in self._mappings:
                provenance = self._provenance.get(version_id)
                self._mappings[version_id] = tuple(
                    json.loads(provenance).get("model_mappings", ())
                ) if provenance is not None else ()
            identity = _resolve_model_identity_mappings(
                (raw_model or "").strip(), provider, access_route, self._mappings[version_id]
            )
            self._identities[key] = identity
        return identity


def compute_pricing_hash(rates_payload: Any) -> str:
    """Calculate deterministic SHA-256 digest of rate definitions."""
    serialized = json.dumps(rates_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_pricing_version(
    conn: sqlite3.Connection,
    version_id: str,
    rates: Sequence[Union[ModelRate, Dict[str, Any]]],
    effective_start: str,
    effective_end: Optional[str] = None,
    currency_code: str = DEFAULT_CURRENCY,
    source_provenance: Optional[Dict[str, Any]] = None,
) -> str:
    """Register an effective dated pricing schedule with model rate entries.

    Atomically writes to pricing_versions and model_pricing_rates.
    """
    # Normalize rates list for hashing and persistence
    normalized_rates: List[Dict[str, Any]] = []
    model_rate_objs: List[ModelRate] = []

    for r in rates:
        if isinstance(r, ModelRate):
            obj = r
        elif isinstance(r, dict):
            obj = ModelRate(
                version_id=version_id,
                provider=r["provider"],
                model_identity=r["model_identity"],
                access_route=r.get("access_route", DEFAULT_ACCESS_ROUTE),
                input_rate_per_m=float(r["input_rate_per_m"]),
                output_rate_per_m=float(r["output_rate_per_m"]),
                cache_read_rate_per_m=float(r["cache_read_rate_per_m"]) if r.get("cache_read_rate_per_m") is not None else None,
                cache_write_5m_rate_per_m=float(r["cache_write_5m_rate_per_m"]) if r.get("cache_write_5m_rate_per_m") is not None else None,
                cache_write_1h_rate_per_m=float(r["cache_write_1h_rate_per_m"]) if r.get("cache_write_1h_rate_per_m") is not None else None,
                is_tiered=bool(r.get("is_tiered", False)),
                prompt_threshold=int(r["prompt_threshold"]) if r.get("prompt_threshold") is not None else None,
                tiered_rates_json=r.get("tiered_rates_json") if isinstance(r.get("tiered_rates_json"), str) else json.dumps(r.get("tiered_rates_json", {})),
                status=r.get("status", STATUS_VERIFIED),
                source_url=r.get("source_url"),
                verified_on=r.get("verified_on"),
                note=r.get("note"),
            )
        else:
            raise TypeError(f"Unsupported rate type: {type(r)}")

        model_rate_objs.append(obj)
        normalized_rates.append({
            "provider": obj.provider,
            "model_identity": obj.model_identity,
            "access_route": obj.access_route,
            "input_rate_per_m": obj.input_rate_per_m,
            "output_rate_per_m": obj.output_rate_per_m,
            "cache_read_rate_per_m": obj.cache_read_rate_per_m,
            "cache_write_5m_rate_per_m": obj.cache_write_5m_rate_per_m,
            "cache_write_1h_rate_per_m": obj.cache_write_1h_rate_per_m,
            "is_tiered": 1 if obj.is_tiered else 0,
            "prompt_threshold": obj.prompt_threshold,
            "tiered_rates": obj.tiered_rates_json,
            "status": obj.status,
        })

    mappings = (source_provenance or {}).get("model_mappings", [])
    digest = compute_pricing_hash({"currency_code": currency_code.upper(), "rates": normalized_rates, "model_mappings": mappings})
    prov_json = json.dumps(source_provenance or {})
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO pricing_versions (
            version_id, pricing_hash, currency_code, effective_start, effective_end,
            source_provenance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (version_id, digest, currency_code.upper(), effective_start, effective_end, prov_json, now_iso),
    )


    for obj in model_rate_objs:
        cursor.execute(
            """
            INSERT INTO model_pricing_rates (
                version_id, provider, model_identity, access_route,
                input_rate_per_m, output_rate_per_m,
                cache_read_rate_per_m, cache_write_5m_rate_per_m, cache_write_1h_rate_per_m,
                is_tiered, prompt_threshold, tiered_rates_json,
                status, source_url, verified_on, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                obj.provider,
                obj.model_identity,
                obj.access_route,
                obj.input_rate_per_m,
                obj.output_rate_per_m,
                obj.cache_read_rate_per_m,
                obj.cache_write_5m_rate_per_m,
                obj.cache_write_1h_rate_per_m,
                1 if obj.is_tiered else 0,
                obj.prompt_threshold,
                obj.tiered_rates_json,
                obj.status,
                obj.source_url,
                obj.verified_on,
                obj.note,
            ),
        )

    return version_id


def get_pricing_version(conn: sqlite3.Connection, version_id: str) -> Optional[PricingVersion]:
    """Retrieve pricing version metadata by ID."""
    row = conn.execute(
        "SELECT version_id, pricing_hash, currency_code, effective_start, effective_end, source_provenance_json, created_at "
        "FROM pricing_versions WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if not row:
        return None
    return PricingVersion(
        version_id=row[0],
        pricing_hash=row[1],
        currency_code=row[2],
        effective_start=row[3],
        effective_end=row[4],
        source_provenance_json=row[5],
        created_at=row[6],
    )


def find_effective_pricing_version(conn: sqlite3.Connection, target_timestamp: str) -> Optional[PricingVersion]:
    """Find the effective pricing version for a given ISO timestamp.

    CRITICAL RULE:
    - Checks effective_start <= target_timestamp AND (effective_end IS NULL OR target_timestamp < effective_end).
    - If no schedule covers target_timestamp, returns None.
    - NEVER falls back to latest rate retroactively.
    """
    if not target_timestamp:
        return None

    row = conn.execute(
        """
        SELECT version_id, pricing_hash, currency_code, effective_start, effective_end,
               source_provenance_json, created_at
        FROM pricing_versions
        WHERE julianday(effective_start) <= julianday(?)
          AND (effective_end IS NULL OR julianday(?) < julianday(effective_end))
        ORDER BY julianday(effective_start) DESC, created_at DESC, version_id DESC
        LIMIT 1
        """,
        (target_timestamp, target_timestamp),
    ).fetchone()

    if not row:
        return None

    return PricingVersion(
        version_id=row[0],
        pricing_hash=row[1],
        currency_code=row[2],
        effective_start=row[3],
        effective_end=row[4],
        source_provenance_json=row[5],
        created_at=row[6],
    )


def get_model_rate(
    conn: sqlite3.Connection,
    version_id: str,
    provider: str,
    model_identity: str,
    access_route: str = DEFAULT_ACCESS_ROUTE,
) -> Optional[ModelRate]:
    """Look up model pricing rate within a specified version.

    Matches provider, model_identity (or normalized model_identity), and access_route.
    """
    prov = (provider or "").strip().lower()
    route = (access_route or DEFAULT_ACCESS_ROUTE).strip()
    norm_model = normalize_model_name(model_identity)

    # Versioned literal aliases are scoped to provider and access route.
    version = get_pricing_version(conn, version_id)
    if version:
        provenance = json.loads(version.source_provenance_json)
        for mapping in provenance.get("model_mappings", []):
            if (mapping["provider"].lower() == prov
                    and mapping["access_route"].lower() == route.lower()
                    and mapping["alias"].lower() == model_identity.lower()):
                model_identity = mapping["model_identity"]
                norm_model = normalize_model_name(model_identity)
                break

    # 1. Try exact model_identity and access_route
    row = conn.execute(
        """
        SELECT version_id, provider, model_identity, access_route,
               input_rate_per_m, output_rate_per_m,
               cache_read_rate_per_m, cache_write_5m_rate_per_m, cache_write_1h_rate_per_m,
               is_tiered, prompt_threshold, tiered_rates_json,
               status, source_url, verified_on, note, rate_id
        FROM model_pricing_rates
        WHERE version_id = ?
          AND LOWER(provider) = ?
          AND (model_identity = ? OR model_identity = ?)
          AND access_route = ?
        LIMIT 1
        """,
        (version_id, prov, model_identity, norm_model, route),
    ).fetchone()

    # 2. If route specific not found and route != default, try default route
    if not row and route != DEFAULT_ACCESS_ROUTE:
        row = conn.execute(
            """
            SELECT version_id, provider, model_identity, access_route,
                   input_rate_per_m, output_rate_per_m,
                   cache_read_rate_per_m, cache_write_5m_rate_per_m, cache_write_1h_rate_per_m,
                   is_tiered, prompt_threshold, tiered_rates_json,
                   status, source_url, verified_on, note, rate_id
            FROM model_pricing_rates
            WHERE version_id = ?
              AND LOWER(provider) = ?
              AND (model_identity = ? OR model_identity = ?)
              AND access_route = ?
            LIMIT 1
            """,
            (version_id, prov, model_identity, norm_model, DEFAULT_ACCESS_ROUTE),
        ).fetchone()

    if not row:
        return None

    return ModelRate(
        version_id=row[0],
        provider=row[1],
        model_identity=row[2],
        access_route=row[3],
        input_rate_per_m=row[4],
        output_rate_per_m=row[5],
        cache_read_rate_per_m=row[6],
        cache_write_5m_rate_per_m=row[7],
        cache_write_1h_rate_per_m=row[8],
        is_tiered=bool(row[9]),
        prompt_threshold=row[10],
        tiered_rates_json=row[11],
        status=row[12],
        source_url=row[13],
        verified_on=row[14],
        note=row[15],
        rate_id=row[16],
    )


def resolve_rates_for_request(
    rate: ModelRate,
    prompt_tokens: int,
) -> Dict[str, Optional[float]]:
    """Determine effective per-token rates considering context length thresholds.

    When prompt_tokens > prompt_threshold on tiered models, tiered rates apply.
    """
    effective = {
        "input": rate.input_rate_per_m,
        "output": rate.output_rate_per_m,
        "cache_read": rate.cache_read_rate_per_m,
        "cache_write_5m": rate.cache_write_5m_rate_per_m,
        "cache_write_1h": rate.cache_write_1h_rate_per_m,
    }

    if rate.is_tiered and rate.prompt_threshold is not None and prompt_tokens > rate.prompt_threshold:
        try:
            long_tier = json.loads(rate.tiered_rates_json or "{}")
            if "input_rate_per_m" in long_tier or "in" in long_tier:
                effective["input"] = float(long_tier.get("input_rate_per_m", long_tier.get("in")))
            if "output_rate_per_m" in long_tier or "out" in long_tier:
                effective["output"] = float(long_tier.get("output_rate_per_m", long_tier.get("out")))
            if "cache_read_rate_per_m" in long_tier or "cr" in long_tier:
                effective["cache_read"] = float(long_tier.get("cache_read_rate_per_m", long_tier.get("cr")))
            if "cache_write_5m_rate_per_m" in long_tier or "cw5m" in long_tier:
                effective["cache_write_5m"] = float(long_tier.get("cache_write_5m_rate_per_m", long_tier.get("cw5m")))
            if "cache_write_1h_rate_per_m" in long_tier or "cw1h" in long_tier:
                effective["cache_write_1h"] = float(long_tier.get("cache_write_1h_rate_per_m", long_tier.get("cw1h")))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return effective


# -----------------------------------------------------------------------------
# Historical FX API
# -----------------------------------------------------------------------------

def record_historical_fx(
    conn: sqlite3.Connection,
    base_currency: str,
    target_currency: str,
    rate_per_usd: float,
    effective_date: str,
    source_provenance: str = "manual",
) -> int:
    """Store dated foreign exchange rate.

    Upserts into historical_fx.
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO historical_fx (
            base_currency, target_currency, rate_per_usd, effective_date, source_provenance, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(base_currency, target_currency, effective_date) DO UPDATE SET
            rate_per_usd=excluded.rate_per_usd,
            source_provenance=excluded.source_provenance;
        """,
        (base_currency.upper(), target_currency.upper(), float(rate_per_usd), effective_date, source_provenance, now_iso),
    )
    return cursor.lastrowid or 0


def get_historical_fx_record(
    conn: sqlite3.Connection, effective_date: str,
    base_currency: str = DEFAULT_CURRENCY, target_currency: str = DEFAULT_TARGET_CURRENCY,
) -> Optional[HistoricalFx]:
    """Return the recorded direct pair on or before the UTC request date."""
    base = (base_currency or DEFAULT_CURRENCY).strip().upper()
    target = (target_currency or DEFAULT_TARGET_CURRENCY).strip().upper()
    if base == target or not effective_date:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(effective_date.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(datetime.timezone.utc)
        date_str = parsed.date().isoformat()
    except ValueError:
        return None
    row = conn.execute(
        """SELECT base_currency, target_currency, rate_per_usd, effective_date,
                  source_provenance, created_at, fx_id
           FROM historical_fx WHERE base_currency = ? AND target_currency = ?
             AND effective_date <= ? ORDER BY effective_date DESC LIMIT 1""",
        (base, target, date_str),
    ).fetchone()
    return HistoricalFx(*row) if row else None


def get_historical_fx(
    conn: sqlite3.Connection, effective_date: str,
    base_currency: str = DEFAULT_CURRENCY, target_currency: str = DEFAULT_TARGET_CURRENCY,
) -> Optional[float]:
    """Return a direct dated rate, or None when unavailable."""
    if (base_currency or DEFAULT_CURRENCY).strip().upper() == (target_currency or DEFAULT_TARGET_CURRENCY).strip().upper():
        return 1.0
    record = get_historical_fx_record(conn, effective_date, base_currency, target_currency)
    return record.rate_per_usd if record else None


# -----------------------------------------------------------------------------
# Request Valuation & Financial Facts
# -----------------------------------------------------------------------------

def unknown_ttl_write_rate(rates: Dict[str, Optional[float]]) -> Optional[float]:
    """Rate for unknown-TTL cache writes. ASSUMPTION: unknown TTL is priced as the 5m default."""
    return rates.get("cache_write_5m")


def evaluate_usage_cost(
    usage: Dict[str, Any],
    rate: Optional[ModelRate],
    fx_rate: Optional[float] = None,
    actual_billed_amount: Optional[float] = None,
    actual_billed_currency: Optional[str] = None,
    subscription_fee_usd: Optional[float] = None,
    canonical_id: str = "",
    card_to_usd_rate: Optional[float] = 1.0,
) -> ValuationResult:
    """Calculate request valuation across disjoint token counters.

    Rules:
    - Disjoint counters: fresh_input, cache_read, cache_write_5m, cache_write_1h, output.
    - Unknown TTL writes (cache_write_unknown_ttl_tokens) are priced at the 5m cache-write rate
      (see unknown_ttl_write_rate). Only a model with no 5m rate leaves them unpriced -> 'partial'.
    - Missing rate -> status = 'unpriced', api_equivalent_usd = None (never 0.0).
    - Status 'estimated' on rate card propagates to request valuation.
    - Cache savings = (No-cache prompt cost) - (Actual cached prompt cost).
      Missing cache rates (including a missing 5m rate for unknown-TTL writes) -> cache_savings_usd = None.
    - Historical FX applied if fx_rate is provided; otherwise api_equivalent_inr = None (never 0.0).
    """
    fresh_in = usage.get("fresh_input_tokens")
    cr = usage.get("cache_read_tokens")
    cw5m = usage.get("cache_write_5m_tokens")
    cw1h = usage.get("cache_write_1h_tokens")
    cw_unk = usage.get("cache_write_unknown_ttl_tokens")
    out = usage.get("output_tokens")

    # Prompt tokens calculation for threshold check
    prompt_tokens = (
        (fresh_in or 0)
        + (cr or 0)
        + (cw5m or 0)
        + (cw1h or 0)
        + (cw_unk or 0)
    )

    if rate is None:
        # Model is unpriced
        return ValuationResult(
            canonical_id=canonical_id,
            pricing_version_id=None,
            actual_billed_amount=actual_billed_amount,
            actual_billed_currency=actual_billed_currency,
            api_equivalent_usd=None,
            api_equivalent_inr=None,
            subscription_fee_usd=subscription_fee_usd,
            cache_savings_usd=None,
            pricing_status=STATUS_UNPRICED,
            fx_rate_applied=None,
            unpriced_counters=["input", "output", "cache"],
        )

    if card_to_usd_rate is None:
        return ValuationResult(
            canonical_id=canonical_id, pricing_version_id=rate.version_id,
            actual_billed_amount=actual_billed_amount, actual_billed_currency=actual_billed_currency,
            subscription_fee_usd=subscription_fee_usd, pricing_status=STATUS_UNPRICED,
            unpriced_counters=["card_currency_to_usd"],
        )

    rates = resolve_rates_for_request(rate, prompt_tokens)
    rates = {key: value * card_to_usd_rate if value is not None else None
             for key, value in rates.items()}
    unpriced_counters: List[str] = []

    # Calculate individual components in USD
    in_rate = rates.get("input")
    out_rate = rates.get("output")
    cr_rate = rates.get("cache_read")
    cw5m_rate = rates.get("cache_write_5m")
    cw1h_rate = rates.get("cache_write_1h")

    fresh_cost: Optional[float] = None
    if fresh_in is not None and fresh_in > 0:
        if in_rate is not None:
            fresh_cost = (fresh_in * in_rate) / 1_000_000.0
        else:
            unpriced_counters.append("fresh_input")
    elif fresh_in == 0:
        fresh_cost = 0.0

    output_cost: Optional[float] = None
    if out is not None and out > 0:
        if out_rate is not None:
            output_cost = (out * out_rate) / 1_000_000.0
        else:
            unpriced_counters.append("output")
    elif out == 0:
        output_cost = 0.0

    cr_cost: Optional[float] = None
    if cr is not None and cr > 0:
        if cr_rate is not None:
            cr_cost = (cr * cr_rate) / 1_000_000.0
        else:
            unpriced_counters.append("cache_read")
    elif cr == 0:
        cr_cost = 0.0

    cw5m_cost: Optional[float] = None
    if cw5m is not None and cw5m > 0:
        if cw5m_rate is not None:
            cw5m_cost = (cw5m * cw5m_rate) / 1_000_000.0
        else:
            unpriced_counters.append("cache_write_5m")
    elif cw5m == 0:
        cw5m_cost = 0.0

    cw1h_cost: Optional[float] = None
    if cw1h is not None and cw1h > 0:
        if cw1h_rate is not None:
            cw1h_cost = (cw1h * cw1h_rate) / 1_000_000.0
        else:
            unpriced_counters.append("cache_write_1h")
    elif cw1h == 0:
        cw1h_cost = 0.0

    unk_rate = unknown_ttl_write_rate(rates)
    cw_unk_cost: Optional[float] = None
    if cw_unk is not None and cw_unk > 0:
        if unk_rate is not None:
            cw_unk_cost = (cw_unk * unk_rate) / 1_000_000.0
        else:
            unpriced_counters.append("cache_write_unknown_ttl")
    elif cw_unk == 0:
        cw_unk_cost = 0.0

    # Sum available costs
    costs = [c for c in (fresh_cost, output_cost, cr_cost, cw5m_cost, cw1h_cost, cw_unk_cost) if c is not None]
    api_equivalent_usd = sum(costs) if costs else 0.0

    # Pricing status resolution
    if unpriced_counters:
        if costs:
            pricing_status = STATUS_PARTIAL
        else:
            pricing_status = STATUS_UNPRICED
            api_equivalent_usd = None
    elif rate.status == STATUS_ESTIMATED:
        pricing_status = STATUS_ESTIMATED
    elif rate.status == STATUS_UNVERIFIED:
        pricing_status = STATUS_UNVERIFIED
    else:
        pricing_status = STATUS_VERIFIED

    # Cache savings: (Hypothetical fresh cost for cached tokens) - (Actual cache cost)
    # Unknown-TTL writes count as cache writes at the 5m rate; a missing rate -> savings cannot be proven -> None
    cache_savings_usd: Optional[float] = None
    if in_rate is not None:
        can_calculate_savings = True
        hypothetical_fresh = 0.0
        actual_cache = 0.0

        if cr is not None and cr > 0:
            if cr_rate is not None:
                hypothetical_fresh += (cr * in_rate) / 1_000_000.0
                actual_cache += (cr * cr_rate) / 1_000_000.0
            else:
                can_calculate_savings = False

        if cw5m is not None and cw5m > 0:
            if cw5m_rate is not None:
                hypothetical_fresh += (cw5m * in_rate) / 1_000_000.0
                actual_cache += (cw5m * cw5m_rate) / 1_000_000.0
            else:
                can_calculate_savings = False

        if cw1h is not None and cw1h > 0:
            if cw1h_rate is not None:
                hypothetical_fresh += (cw1h * in_rate) / 1_000_000.0
                actual_cache += (cw1h * cw1h_rate) / 1_000_000.0
            else:
                can_calculate_savings = False

        if cw_unk is not None and cw_unk > 0:
            if unk_rate is not None:
                hypothetical_fresh += (cw_unk * in_rate) / 1_000_000.0
                actual_cache += (cw_unk * unk_rate) / 1_000_000.0
            else:
                can_calculate_savings = False

        # A request with no cache activity saved exactly 0, not an unknown amount.
        if can_calculate_savings:
            cache_savings_usd = round(hypothetical_fresh - actual_cache, 8)

    # Historical FX conversion
    api_equivalent_inr: Optional[float] = None
    fx_applied: Optional[float] = None
    if api_equivalent_usd is not None and fx_rate is not None:
        api_equivalent_inr = round(api_equivalent_usd * fx_rate, 6)
        fx_applied = fx_rate

    return ValuationResult(
        canonical_id=canonical_id,
        pricing_version_id=rate.version_id,
        actual_billed_amount=actual_billed_amount,
        actual_billed_currency=actual_billed_currency,
        api_equivalent_usd=round(api_equivalent_usd, 8) if api_equivalent_usd is not None else None,
        api_equivalent_inr=api_equivalent_inr,
        subscription_fee_usd=subscription_fee_usd,
        cache_savings_usd=cache_savings_usd,
        pricing_status=pricing_status,
        fx_rate_applied=fx_applied,
        fresh_cost_usd=fresh_cost,
        output_cost_usd=output_cost,
        cache_read_cost_usd=cr_cost,
        cache_write_5m_cost_usd=cw5m_cost,
        cache_write_1h_cost_usd=cw1h_cost,
        cache_write_unknown_ttl_cost_usd=cw_unk_cost,
        unpriced_counters=unpriced_counters,
    )


def calculate_request_valuation(
    conn: sqlite3.Connection,
    canonical_id: str,
    target_currency: str = DEFAULT_TARGET_CURRENCY,
    override_pricing_version_id: Optional[str] = None,
    use_assigned_version: bool = True,
) -> ValuationResult:
    """Evaluate financial facts for a stored canonical request.

    Queries canonical_requests, normalized_usage, pricing schedule, and historical FX.
    """
    req_row = conn.execute(
        """
        SELECT provider, normalized_model, access_route, request_timestamp, pricing_version_id, raw_model
        FROM canonical_requests
        WHERE canonical_id = ?
        """,
        (canonical_id,),
    ).fetchone()

    if not req_row:
        raise ValueError(f"Canonical request not found: {canonical_id}")

    provider, norm_model, route, req_timestamp, assigned_pv, raw_model = req_row

    # Usage counters
    usage_row = conn.execute(
        """
        SELECT fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens,
               cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens
        FROM normalized_usage
        WHERE canonical_id = ?
        """,
        (canonical_id,),
    ).fetchone()

    usage: Dict[str, Any] = {}
    if usage_row:
        usage = {
            "fresh_input_tokens": usage_row[0],
            "cache_read_tokens": usage_row[1],
            "cache_write_5m_tokens": usage_row[2],
            "cache_write_1h_tokens": usage_row[3],
            "cache_write_unknown_ttl_tokens": usage_row[4],
            "output_tokens": usage_row[5],
        }

    # Determine effective pricing version
    version_id = override_pricing_version_id or (assigned_pv if use_assigned_version else None)
    if not version_id:
        effective_pv = find_effective_pricing_version(conn, req_timestamp)
        if effective_pv:
            version_id = effective_pv.version_id

    # Lookup model rate
    rate: Optional[ModelRate] = None
    if version_id:
        rate = get_model_rate(conn, version_id, provider, raw_model, route)
        if rate is None and norm_model != raw_model:
            rate = get_model_rate(conn, version_id, provider, norm_model, route)

    version = get_pricing_version(conn, version_id) if version_id else None
    valuation = evaluate_dated_usage_cost(conn, usage, rate, version, req_timestamp,
                                          target_currency, canonical_id)
    if version_id and not valuation.pricing_version_id:
        valuation.pricing_version_id = version_id

    return valuation


def evaluate_dated_usage_cost(
    conn: sqlite3.Connection, usage: Dict[str, Any], rate: Optional[ModelRate],
    version: Optional[PricingVersion], timestamp: Optional[str],
    target_currency: str = DEFAULT_TARGET_CURRENCY, canonical_id: str = "",
) -> ValuationResult:
    """Evaluate all components using the card and direct FX pairs at the request date."""
    from dataclasses import asdict

    currency = version.currency_code.upper() if version else None
    card_fx = get_historical_fx_record(conn, timestamp, currency, DEFAULT_CURRENCY) if currency and currency != DEFAULT_CURRENCY else None
    target_fx = get_historical_fx_record(conn, timestamp, DEFAULT_CURRENCY, target_currency)
    card_rate = 1.0 if currency == DEFAULT_CURRENCY else (card_fx.rate_per_usd if card_fx else None)
    target_rate = 1.0 if target_currency.upper() == DEFAULT_CURRENCY else (target_fx.rate_per_usd if target_fx else None)
    result = evaluate_usage_cost(usage, rate, fx_rate=target_rate, canonical_id=canonical_id,
                                 card_to_usd_rate=card_rate)
    result.card_currency = currency
    result.card_to_usd_fx = asdict(card_fx) if card_fx else None
    result.usd_to_target_fx = asdict(target_fx) if target_fx else None
    return result


def persist_financial_facts(
    conn: sqlite3.Connection,
    valuation: ValuationResult,
    preserve_billing: bool = False,
    update_request_version: bool = True,
) -> None:
    """Store or update calculated financial facts for a request in SQLite."""
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO financial_facts (
            canonical_id, pricing_version_id, actual_billed_amount, actual_billed_currency,
            api_equivalent_usd, api_equivalent_inr, subscription_fee_usd, cache_savings_usd,
            pricing_status, fx_rate_applied, calculated_at, valuation_provenance_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_id) DO UPDATE SET
            pricing_version_id=excluded.pricing_version_id,
            actual_billed_amount=CASE WHEN ? THEN financial_facts.actual_billed_amount ELSE excluded.actual_billed_amount END,
            actual_billed_currency=CASE WHEN ? THEN financial_facts.actual_billed_currency ELSE excluded.actual_billed_currency END,
            api_equivalent_usd=excluded.api_equivalent_usd,
            api_equivalent_inr=excluded.api_equivalent_inr,
            subscription_fee_usd=CASE WHEN ? THEN financial_facts.subscription_fee_usd ELSE excluded.subscription_fee_usd END,
            cache_savings_usd=excluded.cache_savings_usd,
            pricing_status=excluded.pricing_status,
            fx_rate_applied=excluded.fx_rate_applied,
            valuation_provenance_json=excluded.valuation_provenance_json,
            calculated_at=excluded.calculated_at;
        """,
        (
            valuation.canonical_id,
            valuation.pricing_version_id,
            valuation.actual_billed_amount,
            valuation.actual_billed_currency,
            valuation.api_equivalent_usd,
            valuation.api_equivalent_inr,
            valuation.subscription_fee_usd,
            valuation.cache_savings_usd,
            valuation.pricing_status,
            valuation.fx_rate_applied,
            valuation.calculated_at,
            json.dumps({"card_currency": valuation.card_currency,
                        "card_to_usd_fx": valuation.card_to_usd_fx,
                        "usd_to_target_fx": valuation.usd_to_target_fx}, sort_keys=True),
            preserve_billing,
            preserve_billing,
            preserve_billing,
        ),
    )

    if update_request_version and valuation.pricing_version_id:
        cursor.execute(
            "UPDATE canonical_requests SET pricing_version_id = ? WHERE canonical_id = ?",
            (valuation.pricing_version_id, valuation.canonical_id),
        )


# -----------------------------------------------------------------------------
# Legacy pricing.json Import
# -----------------------------------------------------------------------------

def import_legacy_pricing(
    conn: sqlite3.Connection,
    legacy_data_or_path: Union[str, Dict[str, Any]],
    version_id: str = "legacy_pricing_unverified",
    effective_start: str = "1970-01-01T00:00:00Z",
) -> str:
    """Import legacy pricing.json as undated, unverified baseline data.

    CRITICAL RULE:
    - Marked with status = 'unverified' unless model payload has explicit verified_on.
    - Records explicit note that this represents undated legacy import, not historical schedule.
    - Does NOT fabricate historical dates.
    """
    if isinstance(legacy_data_or_path, str):
        with open(legacy_data_or_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    elif isinstance(legacy_data_or_path, dict):
        doc = legacy_data_or_path
    else:
        raise TypeError("legacy_data_or_path must be a file path string or dict")

    models_dict = doc.get("models", {})
    currency_dict = doc.get("currency", {})
    target_currency = currency_dict.get("code", "INR")
    per_usd = currency_dict.get("per_usd")

    # If FX rate is supplied in pricing.json, import it as an undated baseline FX record
    if per_usd is not None:
        record_historical_fx(
            conn,
            base_currency="USD",
            target_currency=target_currency,
            rate_per_usd=float(per_usd),
            effective_date="1970-01-01",
            source_provenance="legacy_pricing_json_import",
        )

    model_rates: List[ModelRate] = []

    for model_name, mdata in models_dict.items():
        in_rate = float(mdata.get("in", 0.0))
        out_rate = float(mdata.get("out", 0.0))
        cr_rate = float(mdata["cr"]) if "cr" in mdata else None
        cw5m_rate = float(mdata["cw5m"]) if "cw5m" in mdata else None
        cw1h_rate = float(mdata["cw1h"]) if "cw1h" in mdata else None

        # Determine provider by prefix/name
        if "claude" in model_name:
            provider = "anthropic"
        elif "gpt" in model_name or "luna" in model_name:
            provider = "openai"
        elif "gemini" in model_name:
            provider = "google"
        else:
            provider = "unknown"

        # Check for tiered structure (e.g. Haiku 5.5 long context)
        is_tiered = "long" in mdata
        long_data = mdata.get("long", {})
        prompt_threshold = long_data.get("threshold") if is_tiered else None
        tiered_json = json.dumps(long_data) if is_tiered else "{}"

        # Status: import as unverified by default; mark verified only if explicitly recorded
        status = STATUS_UNVERIFIED
        if mdata.get("status") == STATUS_VERIFIED:
            status = STATUS_VERIFIED
        elif mdata.get("status") == STATUS_ESTIMATED:
            status = STATUS_ESTIMATED

        note = mdata.get("note") or "Imported from legacy pricing.json (undated unverified)"
        source_url = mdata.get("source_url")
        verified_on = mdata.get("verified_on")

        model_rates.append(
            ModelRate(
                version_id=version_id,
                provider=provider,
                model_identity=model_name,
                access_route=DEFAULT_ACCESS_ROUTE,
                input_rate_per_m=in_rate,
                output_rate_per_m=out_rate,
                cache_read_rate_per_m=cr_rate,
                cache_write_5m_rate_per_m=cw5m_rate,
                cache_write_1h_rate_per_m=cw1h_rate,
                is_tiered=is_tiered,
                prompt_threshold=prompt_threshold,
                tiered_rates_json=tiered_json,
                status=status,
                source_url=source_url,
                verified_on=verified_on,
                note=note,
            )
        )

    prov_meta = {
        "source": "legacy_pricing_json",
        "verified": False,
        "note": "Imported as undated baseline rate card; does not represent verified historical schedule",
    }

    create_pricing_version(
        conn=conn,
        version_id=version_id,
        rates=model_rates,
        effective_start=effective_start,
        effective_end=None,
        currency_code="USD",
        source_provenance=prov_meta,
    )

    return version_id
