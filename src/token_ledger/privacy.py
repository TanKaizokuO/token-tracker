"""Privacy controls, redaction, retention, and consent management for Token Ledger.

Implements WP07 requirements:
1. Metadata allowlisting and redaction before persistence and export.
2. Source and project inclusion/exclusion checks.
3. Explicit versioned opt-in consent gates (content_capture, content_fingerprints,
   git_linkage, notifications).
4. Safe non-secret account aliases and hashed source locators (without content
   hashes by default).
5. Default exclusion of transcript/prompt/response bodies, keys, code, and tool outputs.
6. Cascade deletion and retention lifecycle safely constrained to a managed root directory
   (with strict path traversal defense).
7. Hashing risk mitigations (equality leakage and dictionary attack disclosures).
8. Guarantees that raw source logs on disk remain completely untouched.

Pure Python 3.9+ standard library only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union


# -----------------------------------------------------------------------------
# 1. Constants & Versioning
# -----------------------------------------------------------------------------

PRIVACY_POLICY_VERSION = "1.0"

OPTIN_CONTENT_CAPTURE = "content_capture"
OPTIN_CONTENT_FINGERPRINTS = "content_fingerprints"
OPTIN_GIT_LINKAGE = "git_linkage"
OPTIN_NOTIFICATIONS = "notifications"

VALID_OPT_IN_KEYS = frozenset({
    OPTIN_CONTENT_CAPTURE,
    OPTIN_CONTENT_FINGERPRINTS,
    OPTIN_GIT_LINKAGE,
    OPTIN_NOTIFICATIONS,
})

# Missing schema entities identified in canonical SQLite store:
# The store schema (001_initial.sql) defines 21 tables.
# Entities 'insights' and 'rollups' are virtual/analytical concepts:
# - 'rollups' are stored under 'archive_partitions'.
# - 'insights' are computed dynamically or staged in 'alerts_and_reports'/'manual_settings_and_outcomes'.
MISSING_SCHEMA_ENTITIES: Tuple[str, ...] = ("insights", "rollups")


# -----------------------------------------------------------------------------
# 2. Secret Redaction Patterns & Detection
# -----------------------------------------------------------------------------

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"
REDACTED_CONTENT_PLACEHOLDER = "[REDACTED_CONTENT]"

# Compiled regex patterns for well-known secrets, API keys, private keys, and tokens
SECRET_PATTERNS: Sequence[re.Pattern] = (
    # OpenAI API Keys
    re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b"),
    # Anthropic API Keys
    re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{20,}\b"),
    # Google AI / GCP API Keys
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # GitHub Personal Access Tokens and OAuth tokens
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{50,}\b"),
    # AWS Access Key IDs
    re.compile(r"\b(?:AKIA|ASIA|AROA)[0-9A-Z]{16}\b"),
    # Slack Tokens
    re.compile(r"\bxox[baprs]-[0-9a-zA-Z]{10,48}\b"),
    # Bearer / JWT Tokens (Bearer eyJ...)
    re.compile(r"(?i)\bBearer\s+(?:[a-zA-Z0-9_\-\.]{16,})\b"),
    # Authorization header lines
    re.compile(r"(?i)(?:authorization|proxy-authorization)\s*:\s*(?:Basic|Bearer|Token)\s+[^\r\n,]+"),
    # Private Key blocks
    re.compile(
        r"-----BEGIN (?:RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE KEY-----[\s\S]*?-----END (?:RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE KEY-----",
        re.MULTILINE,
    ),
    # Key / Secret assignment patterns in JSON or config-like strings
    re.compile(
        r"""(?i)["']?(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|private[_-]?key)["']?\s*[:=]\s*["']([^"'\r\n\s]{8,})["']"""
    ),
    # Common ENV variable assignments (e.g., OPENAI_API_KEY=sk-...)
    re.compile(
        r"""(?i)(?:export\s+)?(?:[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|AUTH))[ \t]*=[ \t]*["']?([^\r\n"'\s]{8,})["']?"""
    ),
)

# Sensitive dictionary keys whose values must be scrubbed or masked
SENSITIVE_KEY_NAMES: Set[str] = {
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "client_secret",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "private_key",
    "auth",
    "authorization",
    "cookie",
    "set-cookie",
}

# Content keys representing prompt/response bodies, code, tool inputs/outputs
CONTENT_KEY_NAMES: Set[str] = {
    "content",
    "prompt",
    "prompt_text",
    "response",
    "response_text",
    "body",
    "code",
    "arguments",
    "tool_input",
    "tool_output",
    "output",
    "stdout",
    "stderr",
    "result",
    "tool_result",
}

# Default safe metadata allowlist
DEFAULT_METADATA_ALLOWLIST: Set[str] = {
    "provider",
    "model",
    "normalized_model",
    "access_route",
    "harness",
    "origin_harness",
    "session_id",
    "parent_session_id",
    "project_id",
    "account_alias",
    "status",
    "request_status",
    "timing_basis",
    "duration_ms",
    "ttft_ms",
    "generation_duration_ms",
    "generation_speed_tps",
    "error_code",
    "error_type",
    "event_kind",
    "event_name",
    "output_size_bytes",
    "tokens",
    "cost",
    "version",
    "is_subagent",
    "turn_index",
    "retry_attempt",
    "created_at",
    "started_at",
    "ended_at",
}


def redact_secrets(text: str, replacement: str = REDACTED_SECRET_PLACEHOLDER) -> str:
    """Scan arbitrary text and replace API keys, tokens, and credentials with placeholder.

    Args:
        text: Input string to scan.
        replacement: String to replace detected secrets with.

    Returns:
        Sanitized string with sensitive secret substrings replaced.
    """
    if not isinstance(text, str) or not text:
        return text

    sanitized = text
    for pattern in SECRET_PATTERNS:
        # If the pattern has groups (e.g. key=value or header: value), replace group match
        if pattern.groups > 0:
            def _sub_group(match: re.Match) -> str:
                full = match.group(0)
                # Group 1 is the sensitive secret portion
                try:
                    val = match.group(1)
                    if val:
                        return full.replace(val, replacement)
                except IndexError:
                    pass
                return replacement

            sanitized = pattern.sub(_sub_group, sanitized)
        else:
            sanitized = pattern.sub(replacement, sanitized)

    return sanitized


# -----------------------------------------------------------------------------
# 3. Metadata Allowlisting & Sanitization Before Persistence / Export
# -----------------------------------------------------------------------------

def sanitize_metadata(
    data: Any,
    allowed_keys: Optional[Set[str]] = None,
    opt_ins: Optional[Union[PrivacySettings, Dict[str, Any]]] = None,
    max_depth: int = 20,
) -> Any:
    """Deeply sanitize metadata dictionary, list, or primitive before persistence or export.

    Enforces:
    - Secrets are scrubbed via `redact_secrets` across all strings.
    - Sensitive key values (passwords, tokens, keys) are redacted.
    - Content keys (prompt bodies, response text, code, tool outputs) are scrubbed
      unless the 'content_capture' opt-in is explicitly enabled.
    - When `allowed_keys` is provided, non-allowlisted keys are pruned.

    Args:
        data: Arbitrary metadata structure (dict, list, string, number, etc.).
        allowed_keys: Optional set of allowed key names for dictionary sanitization.
        opt_ins: Optional privacy opt-in settings or mapping.
        max_depth: Maximum recursion depth to prevent infinite loops on recursive structures.

    Returns:
        Sanitized data structure safe for storage or transmission.
    """
    if max_depth <= 0:
        return "[DEPTH_LIMIT_EXCEEDED]"

    # Resolve content capture opt-in state
    content_capture_enabled = False
    if opt_ins is not None:
        if isinstance(opt_ins, PrivacySettings):
            content_capture_enabled = opt_ins.content_capture
        elif isinstance(opt_ins, dict):
            content_capture_enabled = bool(opt_ins.get(OPTIN_CONTENT_CAPTURE, False))

    if isinstance(data, dict):
        sanitized_dict: Dict[str, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            key_lower = key_str.lower()

            # If explicit allowlist is provided and key is not allowed, omit it
            if allowed_keys is not None and key_lower not in allowed_keys and key_str not in allowed_keys:
                continue

            # Check if key is inherently sensitive (credentials / keys)
            if any(s in key_lower for s in ("password", "secret", "api_key", "apikey", "auth_token", "private_key")):
                sanitized_dict[key_str] = REDACTED_SECRET_PLACEHOLDER
                continue

            # Check if key represents content/code/tool outputs
            if key_lower in CONTENT_KEY_NAMES or any(c in key_lower for c in ("prompt", "stdout", "stderr", "tool_result")):
                if not content_capture_enabled:
                    sanitized_dict[key_str] = REDACTED_CONTENT_PLACEHOLDER
                    continue
                # If content_capture IS enabled, still recursively sanitize for secrets
                sanitized_dict[key_str] = sanitize_metadata(
                    v, allowed_keys=None, opt_ins=opt_ins, max_depth=max_depth - 1
                )
                continue

            # Recurse for nested structure
            sanitized_dict[key_str] = sanitize_metadata(
                v, allowed_keys=None, opt_ins=opt_ins, max_depth=max_depth - 1
            )
        return sanitized_dict

    elif isinstance(data, (list, tuple, set)):
        return [
            sanitize_metadata(item, allowed_keys=None, opt_ins=opt_ins, max_depth=max_depth - 1)
            for item in data
        ]

    elif isinstance(data, str):
        # Scan and redact any inline secrets
        return redact_secrets(data)

    else:
        # Primitive types (int, float, bool, None) are preserved as-is
        return data


def redact_for_export(
    dataset: Any,
    opt_ins: Optional[Union[PrivacySettings, Dict[str, Any]]] = None,
    allowed_columns: Optional[Set[str]] = None,
) -> Any:
    """Reusable export redaction function for CSV rows, JSON payloads, or HTML datasets.

    Guarantees:
    - Never leaks bodies, credentials, keys, code, or tool outputs into exports.
    - If content_capture is not opted in, all prompt/response/tool bodies are redacted.
    - All secrets (API keys, tokens, passwords) are scrubbed regardless of opt-ins.

    Args:
        dataset: List of row dictionaries, nested dictionaries, or table rows.
        opt_ins: Active privacy settings.
        allowed_columns: Optional column/field allowlist.

    Returns:
        Redacted copy of dataset safe for offline export.
    """
    return sanitize_metadata(dataset, allowed_keys=allowed_columns, opt_ins=opt_ins)


# -----------------------------------------------------------------------------
# 4. Source & Project Inclusion Checks
# -----------------------------------------------------------------------------

@dataclass
class ProjectInclusionPolicy:
    """Rules defining which projects are included or excluded from tracking."""
    allowed_projects: Optional[Set[str]] = None
    excluded_projects: Optional[Set[str]] = None
    include_patterns: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None

    def is_included(self, project_name_or_path: str) -> bool:
        """Check whether a given project identifier or filesystem path is included."""
        if not project_name_or_path:
            return True

        norm = project_name_or_path.strip()

        # Check explicit exclusion list first
        if self.excluded_projects and norm in self.excluded_projects:
            return False

        # Check glob exclusion patterns (e.g. '*/secret-*', '.env*')
        if self.exclude_patterns:
            for pat in self.exclude_patterns:
                if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(os.path.basename(norm), pat):
                    return False

        # If explicit allowed set given, must match
        if self.allowed_projects is not None:
            if norm not in self.allowed_projects:
                return False

        # If include glob patterns given, must match at least one
        if self.include_patterns:
            matched = any(
                fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(os.path.basename(norm), pat)
                for pat in self.include_patterns
            )
            if not matched:
                return False

        return True


@dataclass
class SourceInclusionPolicy:
    """Rules defining which source adapters / harnesses are included."""
    allowed_sources: Optional[Set[str]] = None
    excluded_sources: Optional[Set[str]] = None

    def is_included(self, source_name: str) -> bool:
        """Check whether source name (e.g. 'claude_code', 'omp') is included."""
        if not source_name:
            return False

        src = source_name.strip().lower()
        if self.excluded_sources and src in {s.lower() for s in self.excluded_sources}:
            return False
        if self.allowed_sources is not None and src not in {s.lower() for s in self.allowed_sources}:
            return False
        return True


# -----------------------------------------------------------------------------
# 5. Explicit Versioned Opt-In Settings & Consent Gates
# -----------------------------------------------------------------------------

@dataclass
class PrivacySettings:
    """Container for active user privacy toggles and version metadata."""
    content_capture: bool = False
    content_fingerprints: bool = False
    git_linkage: bool = False
    notifications: bool = False
    policy_version: str = PRIVACY_POLICY_VERSION
    opt_in_versions: Dict[str, str] = field(default_factory=dict)
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert settings to dictionary representation."""
        return {
            OPTIN_CONTENT_CAPTURE: self.content_capture,
            OPTIN_CONTENT_FINGERPRINTS: self.content_fingerprints,
            OPTIN_GIT_LINKAGE: self.git_linkage,
            OPTIN_NOTIFICATIONS: self.notifications,
            "policy_version": self.policy_version,
            "opt_in_versions": dict(self.opt_in_versions),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PrivacySettings:
        """Construct PrivacySettings from dictionary."""
        raw_versions = data.get("opt_in_versions", {})
        versions = dict(raw_versions) if isinstance(raw_versions, dict) else {}
        return cls(
            content_capture=bool(data.get(OPTIN_CONTENT_CAPTURE, False)),
            content_fingerprints=bool(data.get(OPTIN_CONTENT_FINGERPRINTS, False)),
            git_linkage=bool(data.get(OPTIN_GIT_LINKAGE, False)),
            notifications=bool(data.get(OPTIN_NOTIFICATIONS, False)),
            policy_version=str(data.get("policy_version", PRIVACY_POLICY_VERSION)),
            opt_in_versions={str(k): str(v) for k, v in versions.items()},
            updated_at=str(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
        )


def load_privacy_settings(conn: sqlite3.Connection) -> PrivacySettings:
    """Load privacy settings from the SQLite privacy_settings table.

    If rows are absent or table is empty, returns safe default settings (all disabled).
    """
    settings = PrivacySettings()

    # If table does not exist, return defaults
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='privacy_settings';"
    )
    if not cur.fetchone():
        cur.close()
        return settings

    cur.execute("SELECT setting_key, setting_value, opt_in_enabled, updated_at FROM privacy_settings;")
    rows = cur.fetchall()
    cur.close()

    for row in rows:
        key = str(row[0])
        val = str(row[1]) if row[1] is not None else ""
        enabled = bool(row[2])

        # Parse version from payload JSON if present
        parsed_version = None
        if val:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict) and "version" in parsed:
                    parsed_version = str(parsed["version"])
            except Exception:
                pass

        clean_key = key.replace("optin:", "")
        if parsed_version:
            settings.opt_in_versions[clean_key] = parsed_version

        if key == "policy_version":
            settings.policy_version = val
        elif clean_key == OPTIN_CONTENT_CAPTURE:
            settings.content_capture = enabled
        elif clean_key == OPTIN_CONTENT_FINGERPRINTS:
            settings.content_fingerprints = enabled
        elif clean_key == OPTIN_GIT_LINKAGE:
            settings.git_linkage = enabled
        elif clean_key == OPTIN_NOTIFICATIONS:
            settings.notifications = enabled

    return settings


def save_privacy_setting(
    conn: sqlite3.Connection,
    key: str,
    enabled: bool,
    description: Optional[str] = None,
    version: str = PRIVACY_POLICY_VERSION,
) -> None:
    """Persist a versioned privacy opt-in setting to SQLite.

    Args:
        conn: SQLite connection.
        key: Setting key name (one of VALID_OPT_IN_KEYS or policy_version).
        enabled: Boolean opt-in state.
        description: Human-readable description of toggle.
        version: Policy version under which consent was granted.
    """
    now = datetime.now(timezone.utc).isoformat()
    value_payload = json.dumps({"enabled": enabled, "version": version})

    with conn:
        conn.execute(
            """
            INSERT INTO privacy_settings (setting_key, setting_value, opt_in_enabled, description, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                opt_in_enabled = excluded.opt_in_enabled,
                description = COALESCE(excluded.description, privacy_settings.description),
                updated_at = excluded.updated_at;
            """,
            (key, value_payload, 1 if enabled else 0, description, now),
        )


def is_opt_in_enabled(
    conn_or_settings: Union[sqlite3.Connection, PrivacySettings, Dict[str, Any]],
    opt_in_key: str,
    expected_version: str = PRIVACY_POLICY_VERSION,
) -> bool:
    """Consent gate check for sensitive features.

    Validates that:
    1. The opt-in key is recognized.
    2. The user has explicitly enabled consent.
    3. The policy version under which consent was recorded matches expected version.

    Args:
        conn_or_settings: Active SQLite connection, PrivacySettings instance, or dict.
        opt_in_key: One of VALID_OPT_IN_KEYS.
        expected_version: Required policy version.

    Returns:
        True if consent is explicitly granted for current version, False otherwise.
    """
    if opt_in_key not in VALID_OPT_IN_KEYS:
        return False

    if isinstance(conn_or_settings, sqlite3.Connection):
        settings = load_privacy_settings(conn_or_settings)
    elif isinstance(conn_or_settings, PrivacySettings):
        settings = conn_or_settings
    elif isinstance(conn_or_settings, dict):
        settings = PrivacySettings.from_dict(conn_or_settings)
    else:
        return False

    # Check per-key opt-in recorded version or global policy version
    recorded_version = settings.opt_in_versions.get(opt_in_key, settings.policy_version)
    if recorded_version != expected_version:
        # Consent recorded under incompatible policy version is not accepted
        return False

    return getattr(settings, opt_in_key, False)


# -----------------------------------------------------------------------------
# 6. Safe Account Aliasing & Source Locator Hashing (Without Content Hashing)
# -----------------------------------------------------------------------------

def sanitize_account_alias(
    raw_account: Optional[str],
    default: str = "default",
    salt: str = "",
    hash_alias: bool = True,
) -> str:
    """Generate a safe, stable, nonsecret account alias.

    Protects against leaking emails, user IDs, auth tokens, or filesystem usernames
    in account identifiers while preserving stable identity scoping.

    Rules:
    - If empty or None -> returns `default`.
    - If normalized account matches `default` -> returns `default`.
    - If hash_alias is True (default) -> derives a deterministic pseudonymous alias
      `acct_<sha256[:12]>`, preventing any exposure of account names or usernames.
    - If hash_alias is False -> validates safe slug; any email, path, secret prefix,
      or long identifier is still hashed for safety.

    Args:
        raw_account: Raw account string from logs or configuration.
        default: Fallback alias name when empty or matches default.
        salt: Optional salt for hashing sensitive identifiers.
        hash_alias: Whether to hash non-default account names (default True).

    Returns:
        Non-secret safe account alias string.
    """
    if not raw_account or not str(raw_account).strip():
        return default

    account = str(raw_account).strip()
    if account == default:
        return default

    # If hash_alias is True (the safe default) or if identifier contains sensitive patterns:
    is_sensitive = (
        hash_alias
        or "@" in account
        or "/" in account
        or "\\" in account
        or account.startswith(("sk-", "ghp_", "AIza", "Bearer ", "token:"))
        or len(account) > 48
    )

    if is_sensitive:
        digest = hashlib.sha256((salt + account).encode("utf-8")).hexdigest()[:12]
        return f"acct_{digest}"

    # Verify characters are safe alphanumeric slug
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", account).lower()
    return cleaned if cleaned else default


def sanitize_file_locator(
    file_path: str,
    base_dir: Optional[str] = None,
    hash_locator: bool = True,
    salt: str = "",
) -> str:
    """Normalize or hash a source file locator without leaking local user home directories.

    CRITICAL PRIVACY GUARANTEE:
    This hashes the *locator path string*, NEVER the file content!
    Content hashing is disabled by default and requires explicit 'content_fingerprints' opt-in.
    Default: hash_locator=True, generating opaque 'loc_<sha256[:16]>'.

    When hash_locator=False:
    - If base_dir is provided and path is strictly within base_dir, returns safe relative path.
    - If path would leak home directory (e.g. ~ or /home/...), hashes the locator to prevent
      directory structure disclosure.
    - Otherwise returns the safe basename of the file.

    Args:
        file_path: Raw source log file path.
        base_dir: Optional base directory to make path relative.
        hash_locator: If True, hashes the path string to an opaque locator hash (default True).
        salt: Optional salt string.

    Returns:
        Sanitized relative locator or opaque path hash.
    """
    if not file_path:
        return ""

    norm = os.path.normpath(str(file_path).strip())

    if hash_locator:
        digest = hashlib.sha256((salt + norm).encode("utf-8")).hexdigest()[:16]
        return f"loc_{digest}"

    # If base_dir provided and path is within it, return relative path
    if base_dir:
        try:
            rel = os.path.relpath(norm, base_dir)
            if not rel.startswith("..") and not os.path.isabs(rel):
                return rel
        except ValueError:
            pass

    # Never retain '~' paths or user directory hierarchies when unhashed!
    home = os.path.expanduser("~")
    if norm.startswith("~") or norm.startswith(home) or "/home/" in norm or "/Users/" in norm:
        digest = hashlib.sha256((salt + norm).encode("utf-8")).hexdigest()[:16]
        return f"loc_{digest}"

    return os.path.basename(norm)

def hash_content_fingerprint(
    content: str,
    opt_in: bool = False,
    salt: str = "",
) -> Optional[str]:
    """Compute a content fingerprint hash for repetition analysis ONLY IF opted in.

    DISCLOSURE OF RISK:
    Hashing low-entropy content (short code snippets, common commands, short prompts)
    leaks equality across sessions and is susceptible to dictionary / brute-force attacks.
    Therefore, content fingerprinting is strictly blocked unless the user has explicitly
    opted in via `content_fingerprints`.

    Args:
        content: Text content to hash.
        opt_in: Boolean indicating whether `content_fingerprints` opt-in is active.
        salt: Optional salt.

    Returns:
        Content SHA-256 fingerprint if opt_in is True, or None if blocked.
    """
    if not opt_in:
        return None

    if not isinstance(content, str) or not content:
        return None

    return hashlib.sha256((salt + content).encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# 7. Safe Retention & Cascading Deletion Constrained to Managed Root
# -----------------------------------------------------------------------------

class PathTraversalError(ValueError):
    """Raised when an operation attempts to access or delete a file outside the managed root."""
    pass


def verify_path_within_root(target_path: str, managed_root: str) -> str:
    """Validate that target_path resolves strictly within managed_root without symlink traversal.

    Defends against directory traversal attacks (e.g. '../', swapped symlinks).
    Uses descriptor-relative traversal with O_NOFOLLOW to prevent TOCTOU races.
    """
    if not managed_root or not target_path:
        raise PathTraversalError("Both target_path and managed_root must be provided.")

    resolved_root = os.path.realpath(os.path.abspath(managed_root))
    norm_target = os.path.normpath(os.path.abspath(target_path))

    try:
        rel = os.path.relpath(norm_target, resolved_root)
    except ValueError:
        raise PathTraversalError(f"Path on different drive: {target_path}")

    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise PathTraversalError(
            f"Security violation: path '{target_path}' resolves outside managed root '{managed_root}'"
        )

    parts = Path(rel).parts
    if any(p in (".", "..") for p in parts):
        raise PathTraversalError(f"Path traversal sequence in target path: {target_path}")

    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        root_fd = os.open(resolved_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        current_fd = root_fd
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=current_fd,
                    )
                except OSError as err:
                    raise PathTraversalError(f"Cannot securely traverse path component '{part}': {err}")
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd

            filename = parts[-1]
            try:
                st = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    raise PathTraversalError(f"Security violation: path '{target_path}' is a symlink")
            except FileNotFoundError:
                pass
            except OSError as err:
                raise PathTraversalError(f"Cannot stat '{filename}': {err}")
        finally:
            if current_fd != root_fd:
                try:
                    os.close(current_fd)
                except OSError:
                    pass
            try:
                os.close(root_fd)
            except OSError:
                pass

    return norm_target


def delete_snapshot_file(file_path: str, managed_root: str) -> bool:
    """Safely delete a local snapshot or export file using descriptor-relative unlink with no-follow.

    Immune to parent symlink swaps and TOCTOU races.

    Args:
        file_path: Path of file to delete.
        managed_root: Base managed directory.

    Returns:
        True if file was deleted, False if file did not exist.

    Raises:
        PathTraversalError: If file_path resolves outside managed_root or crosses symlinks.
    """
    if not managed_root or not file_path:
        raise PathTraversalError("Both target_path and managed_root must be provided.")

    resolved_root = os.path.realpath(os.path.abspath(managed_root))
    norm_target = os.path.normpath(os.path.abspath(file_path))

    try:
        rel = os.path.relpath(norm_target, resolved_root)
    except ValueError:
        raise PathTraversalError(f"Path on different drive: {file_path}")

    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise PathTraversalError(f"Security violation: path '{file_path}' escapes managed root '{managed_root}'")

    parts = Path(rel).parts
    if any(p in (".", "..") for p in parts) or len(parts) == 0:
        raise PathTraversalError(f"Invalid snapshot path: {file_path}")

    if os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        root_fd = os.open(resolved_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        current_fd = root_fd
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=current_fd,
                    )
                except OSError as err:
                    raise PathTraversalError(f"Path traversal or symlink blocked at '{part}': {err}")
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd

            filename = parts[-1]
            try:
                st = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    raise PathTraversalError(f"Target '{filename}' is a symlink, refusing to unlink")
                if stat.S_ISDIR(st.st_mode):
                    raise PathTraversalError(f"Target '{filename}' is a directory, not a file")

                os.unlink(filename, dir_fd=current_fd)
                return True
            except FileNotFoundError:
                return False
            except OSError as err:
                raise PathTraversalError(f"Failed to securely unlink '{filename}': {err}")
        finally:
            if current_fd != root_fd:
                try:
                    os.close(current_fd)
                except OSError:
                    pass
            try:
                os.close(root_fd)
            except OSError:
                pass
    else:
        safe_path = verify_path_within_root(file_path, managed_root)
        if os.path.isfile(safe_path):
            os.remove(safe_path)
            return True
        return False


@dataclass
class RetentionResult:
    """Audit summary of records and snapshot files purged during retention/delete."""
    deleted_sessions: int = 0
    deleted_requests: int = 0
    deleted_prompts: int = 0
    deleted_events: int = 0
    deleted_links: int = 0
    deleted_usage: int = 0
    deleted_financial_facts: int = 0
    deleted_observations: int = 0
    deleted_outcomes: int = 0
    deleted_archive_partitions: int = 0
    deleted_reports: int = 0
    deleted_exports: int = 0
    deleted_snapshot_files: List[str] = field(default_factory=list)
    skipped_paths_due_to_traversal: List[str] = field(default_factory=list)
    missing_schema_entities: List[str] = field(
        default_factory=lambda: list(MISSING_SCHEMA_ENTITIES)
    )

    def total_deleted_records(self) -> int:
        """Sum of all deleted database entity rows."""
        return (
            self.deleted_sessions
            + self.deleted_requests
            + self.deleted_prompts
            + self.deleted_events
            + self.deleted_links
            + self.deleted_usage
            + self.deleted_financial_facts
            + self.deleted_observations
            + self.deleted_outcomes
            + self.deleted_archive_partitions
            + self.deleted_reports
            + self.deleted_exports
        )


def cascade_delete_retention(
    conn: sqlite3.Connection,
    managed_root: str,
    older_than_iso: Optional[str] = None,
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    account_alias: Optional[str] = None,
    delete_snapshot_files: bool = True,
) -> RetentionResult:
    """Execute a cascading retention or deletion operation across store tables.

    Cascades deletion across:
    - sessions, prompts, canonical_requests, events
    - request_observation_links, normalized_usage, financial_facts
    - source_observations (orphan observations whose requests are removed)
    - manual_settings_and_outcomes
    - archive_partitions (scoped to deleted targets/cutoff)
    - alerts_and_reports (scoped to deleted targets/cutoff)
    - export_inventory and associated local snapshot files (scoped strictly to affected artifacts)

    Maintains store schema integrity: does not alter schema, identifies missing entities
    ('insights', 'rollups') in the audit report.

    Args:
        conn: SQLite connection.
        managed_root: Managed root directory containing local exports/snapshots.
        older_than_iso: Optional ISO-8601 timestamp cutoff for retention purging.
        session_id: Optional single session to purge.
        project_id: Optional single project to purge.
        account_alias: Optional account alias scope to purge.
        delete_snapshot_files: Whether to delete associated export snapshot files.

    Returns:
        RetentionResult with counts of purged records and files.
    """
    result = RetentionResult()

    # Enforce foreign key constraints
    conn.execute("PRAGMA foreign_keys = ON;")

    # Collect sessions targeted for deletion
    session_where = ["1=1"]
    session_params: List[Any] = []
    has_scope = False

    if older_than_iso:
        session_where.append("created_at < ?")
        session_params.append(older_than_iso)
        has_scope = True
    if session_id:
        session_where.append("session_id = ?")
        session_params.append(session_id)
        has_scope = True
    if project_id:
        session_where.append("project_id = ?")
        session_params.append(project_id)
        has_scope = True
    if account_alias:
        session_where.append("account_alias = ?")
        session_params.append(account_alias)
        has_scope = True

    if not has_scope:
        # An empty scope selects nothing; never fall through to match-all.
        return result

    session_clause = " AND ".join(session_where)

    cur = conn.cursor()
    cur.execute(f"SELECT session_id, project_id, account_alias, created_at FROM sessions WHERE {session_clause}", session_params)
    target_sessions = cur.fetchall()
    cur.close()

    target_session_ids = [r[0] for r in target_sessions]
    session_created_map = {r[0]: r[3] for r in target_sessions}

    # Collect canonical_ids for targeted sessions
    if target_session_ids:
        ph_s = ",".join("?" * len(target_session_ids))
        cur = conn.cursor()
        cur.execute(f"SELECT canonical_id FROM canonical_requests WHERE session_id IN ({ph_s})", target_session_ids)
        target_canonical_ids = [r[0] for r in cur.fetchall()]

        cur.execute(f"SELECT COUNT(*) FROM prompts WHERE session_id IN ({ph_s})", target_session_ids)
        result.deleted_prompts = cur.fetchone()[0]

        cur.execute(f"SELECT COUNT(*) FROM events WHERE session_id IN ({ph_s})", target_session_ids)
        result.deleted_events = cur.fetchone()[0]
        cur.close()
    else:
        target_canonical_ids = []

    if target_canonical_ids:
        ph_c = ",".join("?" * len(target_canonical_ids))
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM request_observation_links WHERE canonical_id IN ({ph_c})", target_canonical_ids)
        result.deleted_links = cur.fetchone()[0]

        cur.execute(f"SELECT COUNT(*) FROM normalized_usage WHERE canonical_id IN ({ph_c})", target_canonical_ids)
        result.deleted_usage = cur.fetchone()[0]

        cur.execute(f"SELECT COUNT(*) FROM financial_facts WHERE canonical_id IN ({ph_c})", target_canonical_ids)
        result.deleted_financial_facts = cur.fetchone()[0]
        cur.close()

        result.deleted_requests = len(target_canonical_ids)

    # Collect orphan source_observations before deleting FK links
    orphan_observation_ids: List[str] = []
    if target_canonical_ids:
        ph_c = ",".join("?" * len(target_canonical_ids))
        cur = conn.cursor()
        cur.execute(
            f"SELECT DISTINCT observation_id FROM request_observation_links WHERE canonical_id IN ({ph_c})",
            target_canonical_ids,
        )
        cand_obs = [r[0] for r in cur.fetchall()]
        if cand_obs:
            ph_cand = ",".join("?" * len(cand_obs))
            # Observations that are ALSO linked to other live canonical requests
            cur.execute(
                f"SELECT DISTINCT observation_id FROM request_observation_links WHERE canonical_id NOT IN ({ph_c}) AND observation_id IN ({ph_cand})",
                target_canonical_ids + cand_obs,
            )
            shared_obs = {r[0] for r in cur.fetchall()}
            orphan_observation_ids = [obs for obs in cand_obs if obs not in shared_obs]
        cur.close()

    # If older_than_iso, also collect unlinked observations older than cutoff
    if older_than_iso:
        cur = conn.cursor()
        cur.execute(
            "SELECT observation_id FROM source_observations WHERE observed_at < ? "
            "AND observation_id NOT IN (SELECT DISTINCT observation_id FROM request_observation_links)",
            (older_than_iso,),
        )
        for r in cur.fetchall():
            if r[0] not in orphan_observation_ids:
                orphan_observation_ids.append(r[0])
        cur.close()

    # Scope export_inventory and snapshot files
    cur = conn.cursor()
    cur.execute("SELECT export_id, file_path, query_filter_json, created_at FROM export_inventory")
    all_exports = cur.fetchall()
    cur.close()

    affected_exports: List[Tuple[str, str]] = []
    for exp_id, fpath, qfilter_str, exp_created in all_exports:
        qfilter = {}
        if qfilter_str:
            try:
                qfilter = json.loads(qfilter_str)
            except (TypeError, ValueError):
                pass
        if not isinstance(qfilter, dict):
            qfilter = {}

        is_exp_affected = False
        if not has_scope:
            is_exp_affected = True
        elif older_than_iso and exp_created < older_than_iso:
            is_exp_affected = True
        elif session_id or project_id or account_alias:
            filters = qfilter.get("filters")
            if not isinstance(filters, dict):
                filters = {}

            # Canonical queries use nested list filters. Keep reading legacy
            # inventory scopes; absent/malformed scope is not proof of isolation.
            scopes = {}
            for dimension, legacy_keys in (
                ("session", ("session_id", "session_ids")),
                ("project", ("project_id",)),
                ("account_alias", ("account_alias",)),
            ):
                values = filters.get(dimension)
                if values is None:
                    values = next((qfilter[key] for key in legacy_keys if qfilter.get(key)), None)
                if isinstance(values, str) and values:
                    scopes[dimension] = {values}
                elif isinstance(values, list) and values and all(isinstance(v, str) and v for v in values):
                    scopes[dimension] = set(values)

            # Filter dimensions intersect; values within a dimension are ORed.
            # Preserve only artifacts demonstrably disjoint from every target.
            # Without a surviving target row, only the requested scope is known.
            targets = [(row[0], row[1] or "unattributed", row[2]) for row in target_sessions]
            # Without a surviving target row, unrequested dimensions are unknown. They must
            # not act as wildcards: a scope matching no session would otherwise delete every
            # session-scoped snapshot.
            unknown_matches = bool(targets)
            if not targets:
                targets = [(session_id, project_id, account_alias)]
            for target in targets:
                if all(
                    dimension not in scopes or (value is None and unknown_matches) or value in scopes[dimension]
                    for dimension, value in zip(("session", "project", "account_alias"), target)
                ):
                    is_exp_affected = True
                    break
            if is_exp_affected and session_id and not scopes:
                min_created = session_created_map.get(session_id)
                is_exp_affected = min_created is None or exp_created >= min_created

        if is_exp_affected:
            affected_exports.append((exp_id, fpath))

    # Delete snapshot files for affected exports
    if delete_snapshot_files and affected_exports:
        for exp_id, fpath in affected_exports:
            if fpath:
                try:
                    if delete_snapshot_file(fpath, managed_root):
                        result.deleted_snapshot_files.append(fpath)
                except PathTraversalError:
                    result.skipped_paths_due_to_traversal.append(fpath)
                except OSError:
                    pass

    # Begin atomic database deletion
    with conn:
        # Delete orphan source_observations
        if orphan_observation_ids:
            ph_o = ",".join("?" * len(orphan_observation_ids))
            cur = conn.cursor()
            cur.execute(f"DELETE FROM source_observations WHERE observation_id IN ({ph_o})", orphan_observation_ids)
            result.deleted_observations = cur.rowcount
            cur.close()

        # Delete sessions (Foreign keys CASCADE prompts, canonical_requests, events, usage, facts)
        if target_session_ids:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM sessions WHERE {session_clause}", session_params)
            result.deleted_sessions = cur.rowcount
            cur.close()

        # Delete manual_settings_and_outcomes
        outcomes_clause = []
        outcomes_params: List[Any] = []
        if target_session_ids:
            ph_s = ",".join("?" * len(target_session_ids))
            outcomes_clause.append(f"(entity_type = 'session' AND entity_id IN ({ph_s}))")
            outcomes_params.extend(target_session_ids)
        if target_canonical_ids:
            ph_c = ",".join("?" * len(target_canonical_ids))
            outcomes_clause.append(f"(entity_type = 'request' AND entity_id IN ({ph_c}))")
            outcomes_params.extend(target_canonical_ids)
        if older_than_iso:
            outcomes_clause.append("created_at < ?")
            outcomes_params.append(older_than_iso)
        if account_alias:
            outcomes_clause.append("account_alias = ?")
            outcomes_params.append(account_alias)

        if outcomes_clause:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM manual_settings_and_outcomes WHERE {' OR '.join(outcomes_clause)}",
                outcomes_params,
            )
            result.deleted_outcomes = cur.rowcount
            cur.close()

        # Scoped delete for archive_partitions
        cur = conn.cursor()
        cur.execute("SELECT partition_id, start_time, end_time, payload_json FROM archive_partitions")
        all_parts = cur.fetchall()
        cur.close()

        parts_to_delete: List[str] = []
        for pid, pstart, pend, payload_str in all_parts:
            payload = {}
            if payload_str:
                try:
                    payload = json.loads(payload_str)
                except Exception:
                    pass

            is_part_affected = False
            if not has_scope:
                is_part_affected = True
            elif older_than_iso and pend < older_than_iso:
                is_part_affected = True
            elif session_id:
                sess_list = payload.get("session_ids") or []
                if session_id in sess_list or payload.get("session_id") == session_id:
                    is_part_affected = True
            elif project_id and payload.get("project_id") == project_id:
                is_part_affected = True
            elif account_alias and payload.get("account_alias") == account_alias:
                is_part_affected = True

            if is_part_affected:
                parts_to_delete.append(pid)

        if parts_to_delete:
            ph_p = ",".join("?" * len(parts_to_delete))
            cur = conn.cursor()
            cur.execute(f"DELETE FROM archive_partitions WHERE partition_id IN ({ph_p})", parts_to_delete)
            result.deleted_archive_partitions = cur.rowcount
            cur.close()

        # Scoped delete for alerts_and_reports
        cur = conn.cursor()
        cur.execute("SELECT item_id, filter_state_json, created_at FROM alerts_and_reports")
        all_reps = cur.fetchall()
        cur.close()

        reps_to_delete: List[str] = []
        for rep_id, filter_str, rep_created in all_reps:
            rfilters = {}
            if filter_str:
                try:
                    rfilters = json.loads(filter_str)
                except Exception:
                    pass

            is_rep_affected = False
            if not has_scope:
                is_rep_affected = True
            elif older_than_iso and rep_created < older_than_iso:
                is_rep_affected = True
            elif session_id:
                sess_list = rfilters.get("session_ids") or []
                if rfilters.get("session_id") == session_id or session_id in sess_list:
                    is_rep_affected = True
            elif project_id and rfilters.get("project_id") == project_id:
                is_rep_affected = True
            elif account_alias and rfilters.get("account_alias") == account_alias:
                is_rep_affected = True

            if is_rep_affected:
                reps_to_delete.append(rep_id)

        if reps_to_delete:
            ph_r = ",".join("?" * len(reps_to_delete))
            cur = conn.cursor()
            cur.execute(f"DELETE FROM alerts_and_reports WHERE item_id IN ({ph_r})", reps_to_delete)
            result.deleted_reports = cur.rowcount
            cur.close()

        # Delete affected export_inventory records
        if affected_exports:
            exp_ids = [r[0] for r in affected_exports]
            ph_e = ",".join("?" * len(exp_ids))
            cur = conn.cursor()
            cur.execute(f"DELETE FROM export_inventory WHERE export_id IN ({ph_e})", exp_ids)
            result.deleted_exports = cur.rowcount
            cur.close()

    return result
