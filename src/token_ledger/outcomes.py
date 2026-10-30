"""Outcome tracking, task labeling, Git linkage, and manual feedback persistence (WP18).

Implements:
1. Persistence of manual task labels and completion ratings ('complete', 'partial', 'unsuccessful', 'abandoned').
2. Correction and restart tracking, manual ratings, and explicit user-ownership vs. inference metadata.
3. Strict separation of manual vs. inferred outcomes (method, confidence, source, version).
4. Local Git metadata linkage protected by explicit git_linkage opt-in consent gate:
   - When consent is disabled: performs NO git command.
   - When consent is enabled: inspects ONLY local git metadata on explicit repository path.
   - Extracts changed file counts, line insertions/deletions, commit hash, test outcome.
   - Produces safe file aliases; strictly excludes source code, transcripts, and network operations.
5. Parameterized CRUD operations with strict input validation and SQL injection defense.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from token_ledger.privacy import (
    OPTIN_GIT_LINKAGE,
    PRIVACY_POLICY_VERSION,
    is_opt_in_enabled,
    redact_secrets,
    sanitize_metadata,
)

OUTCOMES_POLICY_VERSION = "1.0"
DEFAULT_MIN_SAMPLE_ADEQUACY = 3
_UNSET = object()

VALID_ENTITY_TYPES = frozenset({"session", "request", "task", "project", "prompt"})
VALID_COMPLETION_STATUSES = frozenset({"complete", "partial", "unsuccessful", "abandoned"})
VALID_INFERENCE_METHODS = frozenset({"manual", "rule", "ai", "heuristic"})
STANDARD_TASK_LABELS = frozenset({
    "debugging",
    "feature",
    "refactoring",
    "research",
    "review",
    "docs",
    "quick_question",
})


@dataclass
class OutcomeRecord:
    """Canonical outcome, task label, and completion tracking record."""

    outcome_id: str
    entity_type: str
    entity_id: str
    account_alias: str = "default"
    task_label: Optional[str] = None
    completion_rating: Optional[str] = None  # 'complete', 'partial', 'unsuccessful', 'abandoned'
    manual_rating: Optional[float] = None
    is_inferred: bool = False
    inference_method: Optional[str] = None
    inference_confidence: Optional[float] = None
    git_commit_link: Optional[str] = None
    notes: Optional[str] = None
    was_corrected: Optional[bool] = None
    was_restarted: Optional[bool] = None
    user_ownership: str = "user"  # 'user' vs 'inferred'
    source: str = "user"  # 'user', 'cli', 'api', 'inference'
    version: str = OUTCOMES_POLICY_VERSION
    git_metadata: Optional[Dict[str, Any]] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializes record to clean dictionary."""
        return {
            "outcome_id": self.outcome_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "account_alias": self.account_alias,
            "task_label": self.task_label,
            "completion_rating": self.completion_rating,
            "manual_rating": self.manual_rating,
            "is_inferred": self.is_inferred,
            "inference_method": self.inference_method,
            "inference_confidence": self.inference_confidence,
            "git_commit_link": self.git_commit_link,
            "notes": self.notes,
            "was_corrected": self.was_corrected,
            "was_restarted": self.was_restarted,
            "user_ownership": self.user_ownership,
            "source": self.source,
            "version": self.version,
            "git_metadata": self.git_metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Union[sqlite3.Row, Dict[str, Any], Tuple[Any, ...]]) -> OutcomeRecord:
        """Constructs OutcomeRecord from SQLite row or dictionary, unpacking structured notes."""
        if isinstance(row, dict):
            d = row
        elif isinstance(row, sqlite3.Row):
            d = dict(row)
        else:
            # Tuple order matching 001_initial.sql manual_settings_and_outcomes
            keys = [
                "outcome_id", "entity_type", "entity_id", "account_alias",
                "task_label", "completion_rating", "manual_rating", "is_inferred",
                "inference_method", "inference_confidence", "git_commit_link",
                "notes", "created_at", "updated_at"
            ]
            d = dict(zip(keys, row))

        raw_notes = d.get("notes")
        was_corrected: Optional[bool] = None
        was_restarted: Optional[bool] = None
        git_metadata: Optional[Dict[str, Any]] = None
        user_notes: Optional[str] = raw_notes
        source = "inference" if d.get("is_inferred") else "user"
        user_ownership = "inferred" if d.get("is_inferred") else "user"
        version = OUTCOMES_POLICY_VERSION

        if isinstance(raw_notes, str) and raw_notes.strip().startswith("{") and raw_notes.strip().endswith("}"):
            try:
                parsed = json.loads(raw_notes)
                if isinstance(parsed, dict) and ("was_corrected" in parsed or "user_ownership" in parsed or "version" in parsed or "git_metadata" in parsed):
                    was_corrected = parsed.get("was_corrected")
                    was_restarted = parsed.get("was_restarted")
                    git_metadata = parsed.get("git_metadata")
                    user_notes = parsed.get("notes")
                    source = parsed.get("source", source)
                    user_ownership = parsed.get("user_ownership", user_ownership)
                    version = parsed.get("version", version)
            except Exception:
                pass

        return cls(
            outcome_id=str(d.get("outcome_id", "")),
            entity_type=str(d.get("entity_type", "")),
            entity_id=str(d.get("entity_id", "")),
            account_alias=str(d.get("account_alias", "default")),
            task_label=d.get("task_label"),
            completion_rating=d.get("completion_rating"),
            manual_rating=float(d["manual_rating"]) if d.get("manual_rating") is not None else None,
            is_inferred=bool(d.get("is_inferred", 0)),
            inference_method=d.get("inference_method"),
            inference_confidence=float(d["inference_confidence"]) if d.get("inference_confidence") is not None else None,
            git_commit_link=d.get("git_commit_link"),
            notes=user_notes,
            was_corrected=was_corrected,
            was_restarted=was_restarted,
            user_ownership=user_ownership,
            source=source,
            version=version,
            git_metadata=git_metadata,
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
        )


# -----------------------------------------------------------------------------
# Input Validation & Sanitization
# -----------------------------------------------------------------------------

def validate_entity(entity_type: str, entity_id: str) -> Tuple[str, str]:
    """Validates entity_type and entity_id."""
    if not isinstance(entity_type, str) or entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"Invalid entity_type: '{entity_type}'. Must be one of {sorted(VALID_ENTITY_TYPES)}")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("entity_id must be a non-empty string.")
    return entity_type.strip(), entity_id.strip()


def validate_completion_rating(status: Optional[str]) -> Optional[str]:
    """Validates completion status rating ('complete', 'partial', 'unsuccessful', 'abandoned')."""
    if status is None:
        return None
    cleaned = str(status).strip().lower()
    if cleaned not in VALID_COMPLETION_STATUSES:
        raise ValueError(f"Invalid completion_rating: '{status}'. Must be one of {sorted(VALID_COMPLETION_STATUSES)} or None.")
    return cleaned


def validate_task_label(task_label: Optional[str]) -> Optional[str]:
    """Validates and sanitizes task label."""
    if task_label is None:
        return None
    cleaned = redact_secrets(str(task_label).strip())
    if not cleaned:
        return None
    if len(cleaned) > 120:
        cleaned = cleaned[:120]
    return cleaned


def validate_manual_rating(rating: Optional[Union[int, float]]) -> Optional[float]:
    """Validates manual rating numeric range [0.0, 10.0]."""
    if rating is None:
        return None
    try:
        val = float(rating)
    except (ValueError, TypeError):
        raise ValueError(f"manual_rating must be a numeric float, got: {type(rating).__name__}")
    if val < 0.0 or val > 10.0:
        raise ValueError(f"manual_rating must be between 0.0 and 10.0, got: {val}")
    return round(val, 2)


def validate_confidence(confidence: Optional[Union[int, float]]) -> Optional[float]:
    """Validates inference confidence in [0.0, 1.0]."""
    if confidence is None:
        return None
    try:
        val = float(confidence)
    except (ValueError, TypeError):
        raise ValueError(f"inference_confidence must be numeric, got: {type(confidence).__name__}")
    if val < 0.0 or val > 1.0:
        raise ValueError(f"inference_confidence must be in range [0.0, 1.0], got: {val}")
    return round(val, 4)


def validate_inference_parameters(
    is_inferred: bool,
    inference_method: Optional[str],
    inference_confidence: Optional[float],
) -> None:
    """Validates inference fields consistency."""
    if not is_inferred:
        if inference_method and inference_method not in ("manual", None):
            raise ValueError(f"Manual outcome (is_inferred=False) cannot have automated inference_method '{inference_method}'")
    else:
        if inference_method and inference_method not in VALID_INFERENCE_METHODS:
            raise ValueError(f"Invalid inference_method: '{inference_method}'. Must be one of {sorted(VALID_INFERENCE_METHODS)}")


def _encode_notes_payload(
    notes: Optional[str],
    was_corrected: Optional[bool],
    was_restarted: Optional[bool],
    user_ownership: str,
    source: str,
    version: str,
    git_metadata: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Encodes structured metadata and user notes into a single string for storage."""
    payload: Dict[str, Any] = {
        "version": version,
        "user_ownership": user_ownership,
        "source": source,
    }
    if was_corrected is not None:
        payload["was_corrected"] = was_corrected
    if was_restarted is not None:
        payload["was_restarted"] = was_restarted
    if notes is not None:
        payload["notes"] = redact_secrets(str(notes).strip())
    if git_metadata is not None:
        payload["git_metadata"] = sanitize_metadata(git_metadata)

    return json.dumps(payload, ensure_ascii=False)


# -----------------------------------------------------------------------------
# Parameterized CRUD Operations
# -----------------------------------------------------------------------------

def create_outcome(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    outcome_id: Optional[str] = None,
    account_alias: str = "default",
    task_label: Optional[str] = None,
    completion_rating: Optional[str] = None,
    manual_rating: Optional[Union[int, float]] = None,
    is_inferred: bool = False,
    inference_method: Optional[str] = None,
    inference_confidence: Optional[Union[int, float]] = None,
    git_commit_link: Optional[str] = None,
    notes: Optional[str] = None,
    was_corrected: Optional[bool] = None,
    was_restarted: Optional[bool] = None,
    user_ownership: Optional[str] = None,
    source: Optional[str] = None,
    git_metadata: Optional[Dict[str, Any]] = None,
    version: str = OUTCOMES_POLICY_VERSION,
) -> OutcomeRecord:
    """Creates or replaces an outcome record using strictly parameterized SQL."""
    valid_entity_type, valid_entity_id = validate_entity(entity_type, entity_id)
    valid_status = validate_completion_rating(completion_rating)
    valid_label = validate_task_label(task_label)
    valid_rating = validate_manual_rating(manual_rating)
    valid_confidence = validate_confidence(inference_confidence)
    validate_inference_parameters(is_inferred, inference_method, valid_confidence)

    resolved_outcome_id = (outcome_id or f"outc_{secrets.token_hex(8)}").strip()
    if not resolved_outcome_id:
        raise ValueError("outcome_id cannot be empty.")

    resolved_ownership = user_ownership or ("inferred" if is_inferred else "user")
    resolved_source = source or ("inference" if is_inferred else "user")

    now_iso = datetime.now(timezone.utc).isoformat()
    encoded_notes = _encode_notes_payload(
        notes=notes,
        was_corrected=was_corrected,
        was_restarted=was_restarted,
        user_ownership=resolved_ownership,
        source=resolved_source,
        version=version,
        git_metadata=git_metadata,
    )

    with conn:
        conn.execute(
            """
            INSERT INTO manual_settings_and_outcomes (
                outcome_id, entity_type, entity_id, account_alias,
                task_label, completion_rating, manual_rating, is_inferred,
                inference_method, inference_confidence, git_commit_link,
                notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(outcome_id) DO UPDATE SET
                entity_type = excluded.entity_type,
                entity_id = excluded.entity_id,
                account_alias = excluded.account_alias,
                task_label = excluded.task_label,
                completion_rating = excluded.completion_rating,
                manual_rating = excluded.manual_rating,
                is_inferred = excluded.is_inferred,
                inference_method = excluded.inference_method,
                inference_confidence = excluded.inference_confidence,
                git_commit_link = excluded.git_commit_link,
                notes = excluded.notes,
                updated_at = excluded.updated_at;
            """,
            (
                resolved_outcome_id,
                valid_entity_type,
                valid_entity_id,
                account_alias.strip() or "default",
                valid_label,
                valid_status,
                valid_rating,
                1 if is_inferred else 0,
                inference_method,
                valid_confidence,
                git_commit_link,
                encoded_notes,
                now_iso,
                now_iso,
            ),
        )

    res = get_outcome(conn, resolved_outcome_id)
    if res is None:
        raise RuntimeError(f"Failed to retrieve created outcome {resolved_outcome_id}")
    return res


def get_outcome(conn: sqlite3.Connection, outcome_id: str) -> Optional[OutcomeRecord]:
    """Retrieves an outcome by outcome_id."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT outcome_id, entity_type, entity_id, account_alias,
               task_label, completion_rating, manual_rating, is_inferred,
               inference_method, inference_confidence, git_commit_link,
               notes, created_at, updated_at
        FROM manual_settings_and_outcomes
        WHERE outcome_id = ?;
        """,
        (outcome_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return OutcomeRecord.from_row(row)


def get_outcome_for_entity(
    conn: sqlite3.Connection, entity_type: str, entity_id: str
) -> Optional[OutcomeRecord]:
    """Retrieves latest outcome associated with an entity."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT outcome_id, entity_type, entity_id, account_alias,
               task_label, completion_rating, manual_rating, is_inferred,
               inference_method, inference_confidence, git_commit_link,
               notes, created_at, updated_at
        FROM manual_settings_and_outcomes
        WHERE entity_type = ? AND entity_id = ?
        ORDER BY updated_at DESC
        LIMIT 1;
        """,
        (entity_type, entity_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return OutcomeRecord.from_row(row)


def list_outcomes(
    conn: sqlite3.Connection,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    task_label: Optional[str] = None,
    completion_rating: Optional[str] = None,
    is_inferred: Optional[bool] = None,
    account_alias: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[OutcomeRecord]:
    """Queries outcomes using parameterized conditions."""
    clauses: List[str] = []
    params: List[Any] = []

    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if task_label:
        clauses.append("task_label = ?")
        params.append(task_label)
    if completion_rating:
        clauses.append("completion_rating = ?")
        params.append(completion_rating)
    if is_inferred is not None:
        clauses.append("is_inferred = ?")
        params.append(1 if is_inferred else 0)
    if account_alias:
        clauses.append("account_alias = ?")
        params.append(account_alias)

    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT outcome_id, entity_type, entity_id, account_alias,
               task_label, completion_rating, manual_rating, is_inferred,
               inference_method, inference_confidence, git_commit_link,
               notes, created_at, updated_at
        FROM manual_settings_and_outcomes
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?;
    """
    params.extend([max(1, limit), max(0, offset)])

    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    return [OutcomeRecord.from_row(r) for r in rows]


def update_outcome(
    conn: sqlite3.Connection,
    outcome_id: str,
    task_label: Optional[str] = None,
    completion_rating: Optional[str] = None,
    manual_rating: Union[int, float, None, object] = _UNSET,
    notes: Optional[str] = None,
    was_corrected: Optional[bool] = None,
    was_restarted: Optional[bool] = None,
    git_commit_link: Optional[str] = None,
    git_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[OutcomeRecord]:
    """Updates an existing outcome record preserving unchanged fields."""
    existing = get_outcome(conn, outcome_id)
    if existing is None:
        return None

    resolved_label = validate_task_label(task_label) if task_label is not None else existing.task_label
    resolved_status = validate_completion_rating(completion_rating) if completion_rating is not None else existing.completion_rating
    resolved_rating = existing.manual_rating if manual_rating is _UNSET else validate_manual_rating(manual_rating)
    resolved_notes = notes if notes is not None else existing.notes
    resolved_corrected = was_corrected if was_corrected is not None else existing.was_corrected
    resolved_restarted = was_restarted if was_restarted is not None else existing.was_restarted
    resolved_git_link = git_commit_link if git_commit_link is not None else existing.git_commit_link
    resolved_git_meta = git_metadata if git_metadata is not None else existing.git_metadata

    now_iso = datetime.now(timezone.utc).isoformat()
    encoded_notes = _encode_notes_payload(
        notes=resolved_notes,
        was_corrected=resolved_corrected,
        was_restarted=resolved_restarted,
        user_ownership=existing.user_ownership,
        source=existing.source,
        version=existing.version,
        git_metadata=resolved_git_meta,
    )

    with conn:
        conn.execute(
            """
            UPDATE manual_settings_and_outcomes
            SET task_label = ?,
                completion_rating = ?,
                manual_rating = ?,
                git_commit_link = ?,
                notes = ?,
                updated_at = ?
            WHERE outcome_id = ?;
            """,
            (
                resolved_label,
                resolved_status,
                resolved_rating,
                resolved_git_link,
                encoded_notes,
                now_iso,
                outcome_id,
            ),
        )

    return get_outcome(conn, outcome_id)


def delete_outcome(conn: sqlite3.Connection, outcome_id: str) -> bool:
    """Deletes an outcome record by outcome_id."""
    with conn:
        cur = conn.execute(
            "DELETE FROM manual_settings_and_outcomes WHERE outcome_id = ?;",
            (outcome_id,),
        )
        return cur.rowcount > 0


# -----------------------------------------------------------------------------
# Local Git Linkage with Strict Consent Gate
# -----------------------------------------------------------------------------

def _alias_path(file_path: str) -> str:
    """Hashes file path to a safe, non-identifying alias preserving extension."""
    clean_p = file_path.strip().replace("\\", "/")
    suffix = Path(clean_p).suffix
    digest = hashlib.sha256(clean_p.encode("utf-8")).hexdigest()[:8]
    return f"file_{digest}{suffix}"


def inspect_git_metadata(
    repo_path: Union[str, Path],
    conn: Optional[sqlite3.Connection] = None,
    consent_granted: Optional[bool] = None,
    commit_ref: str = "HEAD",
) -> Dict[str, Any]:
    """Inspects ONLY local Git metadata for an explicit repository path.

    Strict Consent Gate:
    1. If consent is disabled: PERFORMS NO GIT COMMAND WHATSOEVER.
    2. If consent is enabled:
       - Validates explicit repository path.
       - Runs local commands only: rev-parse, diff-tree, diff --shortstat.
       - Strictly no source code, no transcripts, and no network operations.
       - Aliases changed file paths to safe anonymous tokens.

    Returns:
        Structured response with consent state, execution status, and safe metadata.
    """
    # 1. Strict Consent Check
    has_consent = False
    if consent_granted is True:
        has_consent = True
    elif consent_granted is False:
        has_consent = False
    elif conn is not None:
        has_consent = is_opt_in_enabled(conn, OPTIN_GIT_LINKAGE)

    if not has_consent:
        return {
            "consent_enabled": False,
            "status": "consent_disabled",
            "reason": "git_linkage consent opt-in required; no git command executed",
            "metadata": None,
        }

    # 2. Path Validation (Explicit Path Required, No Guessed Path)
    if not repo_path:
        return {
            "consent_enabled": True,
            "status": "invalid_path",
            "reason": "Explicit repository path must be provided; path cannot be empty",
            "metadata": None,
        }

    resolved_path = Path(repo_path).resolve()
    if not resolved_path.is_dir():
        return {
            "consent_enabled": True,
            "status": "not_a_directory",
            "reason": f"Path is not an accessible directory: {resolved_path}",
            "metadata": None,
        }

    # 3. Verify git repository locally
    try:
        check_proc = subprocess.run(
            ["git", "-C", str(resolved_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if check_proc.returncode != 0 or check_proc.stdout.strip() != "true":
            return {
                "consent_enabled": True,
                "status": "not_a_git_repo",
                "reason": f"Directory is not a Git working tree: {resolved_path}",
                "metadata": None,
            }

        # 4. Extract commit hash
        hash_proc = subprocess.run(
            ["git", "-C", str(resolved_path), "rev-parse", commit_ref],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if hash_proc.returncode != 0:
            return {
                "consent_enabled": True,
                "status": "commit_not_found",
                "reason": f"Ref '{commit_ref}' not found in repository",
                "metadata": None,
            }
        commit_hash = hash_proc.stdout.strip()

        # 5. Extract shortstat diff metrics (files changed, insertions, deletions)
        # Check if parent commit exists
        parent_check = subprocess.run(
            ["git", "-C", str(resolved_path), "rev-parse", f"{commit_hash}^@"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        has_parent = bool(parent_check.stdout.strip())

        changed_files_count = 0
        lines_added = 0
        lines_deleted = 0
        raw_filenames: List[str] = []

        if has_parent:
            stat_proc = subprocess.run(
                ["git", "-C", str(resolved_path), "diff", "--shortstat", f"{commit_hash}~1", commit_hash],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            stat_out = stat_proc.stdout.strip()
            name_proc = subprocess.run(
                ["git", "-C", str(resolved_path), "diff", "--name-only", f"{commit_hash}~1", commit_hash],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            raw_filenames = [line.strip() for line in name_proc.stdout.splitlines() if line.strip()]
        else:
            # Initial commit with no parent
            # Empty tree SHA 4b825dc642cb6eb9a060e54bf8d69288fbee4904
            stat_proc = subprocess.run(
                ["git", "-C", str(resolved_path), "diff-tree", "--shortstat", "--no-commit-id", "-r", commit_hash],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            stat_out = stat_proc.stdout.strip()
            name_proc = subprocess.run(
                ["git", "-C", str(resolved_path), "diff-tree", "--name-only", "--no-commit-id", "-r", commit_hash],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            raw_filenames = [line.strip() for line in name_proc.stdout.splitlines() if line.strip()]

        if stat_out:
            m_files = re.search(r"(\d+)\s+files?\s+changed", stat_out)
            m_ins = re.search(r"(\d+)\s+insertions?", stat_out)
            m_del = re.search(r"(\d+)\s+deletions?", stat_out)
            if m_files:
                changed_files_count = int(m_files.group(1))
            if m_ins:
                lines_added = int(m_ins.group(1))
            if m_del:
                lines_deleted = int(m_del.group(1))
        elif raw_filenames:
            changed_files_count = len(raw_filenames)

        # Alias file paths safely
        file_aliases = [_alias_path(f) for f in raw_filenames]

        return {
            "consent_enabled": True,
            "status": "success",
            "metadata": {
                "commit_hash": commit_hash,
                "changed_files_count": changed_files_count,
                "lines_added": lines_added,
                "lines_deleted": lines_deleted,
                "file_aliases": file_aliases,
                "test_outcome": None,  # unobserved without specific test instrumentation
            },
        }

    except subprocess.TimeoutExpired:
        return {
            "consent_enabled": True,
            "status": "timeout",
            "reason": "Git inspection timed out",
            "metadata": None,
        }
    except Exception as exc:
        return {
            "consent_enabled": True,
            "status": "error",
            "reason": f"Git command execution error: {type(exc).__name__}",
            "metadata": None,
        }


def link_git_outcome(
    conn: sqlite3.Connection,
    outcome_id: str,
    repo_path: Union[str, Path],
    commit_ref: str = "HEAD",
    test_outcome: Optional[str] = None,
    consent_granted: Optional[bool] = None,
) -> OutcomeRecord:
    """Links local Git metadata to an outcome record under explicit opt-in consent."""
    git_res = inspect_git_metadata(
        repo_path=repo_path,
        conn=conn,
        consent_granted=consent_granted,
        commit_ref=commit_ref,
    )

    if not git_res.get("consent_enabled"):
        raise PermissionError("Cannot link Git metadata: git_linkage opt-in consent is disabled.")
    if git_res.get("status") != "success" or not git_res.get("metadata"):
        raise RuntimeError(f"Git inspection failed: {git_res.get('reason', 'unknown error')}")

    meta = dict(git_res["metadata"])
    if test_outcome:
        meta["test_outcome"] = redact_secrets(test_outcome.strip())

    commit_hash = meta["commit_hash"]
    updated = update_outcome(
        conn=conn,
        outcome_id=outcome_id,
        git_commit_link=commit_hash,
        git_metadata=meta,
    )
    if updated is None:
        raise KeyError(f"Outcome record {outcome_id} not found.")
    return updated
