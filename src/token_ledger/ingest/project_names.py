"""Name projects by their folder base name, for projects whose project_name still equals project_id.

project_id stays the opaque hash, proj_<sha256(basename)[:12]>. This pass only sets project_name.
Candidate basenames come from logs the database already references:

- oh-my-pi: session header observations keep the header cwd basename as provenance cwd_alias.
- Claude Code: the database stores hashed file locators, loc_<sha256(path)[:16]>. Files under
  claude_root whose locator matches a stored Claude Code locator are read for their "cwd" fields.

Each candidate basename is hashed the way ingest hashes it, and only ids that are still unnamed
are updated. The function is idempotent, and it never overwrites a name a user set.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, Optional, Set

from token_ledger.ingest.runner import _project_display_name, _sanitize_project_identifier
from token_ledger.privacy import sanitize_file_locator


def _note_candidate(raw_cwd: Any, wanted: Set[str], found: Dict[str, str]) -> None:
    """Record the basename of raw_cwd under its ingest-style project id, if that id is unnamed."""
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return
    name = _project_display_name(raw_cwd)
    if not name:
        return
    project_id = _sanitize_project_identifier(name)
    if project_id in wanted and project_id not in found and name != project_id:
        found[project_id] = name


def _omp_candidates(conn: sqlite3.Connection, wanted: Set[str], found: Dict[str, str]) -> None:
    rows = conn.execute(
        """
        SELECT provenance_json FROM source_observations
        WHERE source_adapter = 'omp' AND provenance_json LIKE '%cwd_alias%';
        """
    ).fetchall()
    for (provenance_json,) in rows:
        try:
            provenance = json.loads(provenance_json or "{}")
        except ValueError:
            continue
        if isinstance(provenance, dict) and provenance.get("entry_type") == "session":
            _note_candidate(provenance.get("cwd_alias"), wanted, found)


def _claude_candidates(
    conn: sqlite3.Connection, claude_root: Optional[str], wanted: Set[str], found: Dict[str, str]
) -> int:
    """Read cwd values from Claude Code files the database has ingested. Returns files matched."""
    if not claude_root or not os.path.isdir(claude_root):
        return 0
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT file_locator FROM source_observations WHERE source_adapter = 'claude_code';"
        ).fetchall()
    }
    matched = 0
    for dirpath, _dirnames, filenames in os.walk(claude_root):
        for filename in filenames:
            if not filename.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, filename)
            if sanitize_file_locator(path) not in stored:
                continue
            matched += 1
            try:
                with open(path, "rb") as handle:
                    for line in handle:
                        if b'"cwd"' not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(record, dict):
                            _note_candidate(record.get("cwd"), wanted, found)
            except OSError:
                continue
    return matched


def backfill_project_names(conn: sqlite3.Connection, claude_root: Optional[str] = None) -> Dict[str, int]:
    """Set project_name to the folder base name where project_name still equals project_id.

    Returns counts: considered (unnamed projects), named (updated by this run),
    still_unnamed (no name derivable), and claude_files_matched (diagnostic).
    """
    wanted: Set[str] = {
        row[0]
        for row in conn.execute("SELECT project_id FROM projects WHERE project_name = project_id;").fetchall()
    }
    found: Dict[str, str] = {}
    claude_files_matched = 0
    if wanted:
        _omp_candidates(conn, wanted, found)
        claude_files_matched = _claude_candidates(conn, claude_root, wanted, found)

    named = 0
    with conn:
        for project_id, name in sorted(found.items()):
            cursor = conn.execute(
                "UPDATE projects SET project_name = ? WHERE project_id = ? AND project_name = project_id;",
                (name, project_id),
            )
            named += cursor.rowcount
    considered = len(wanted)
    return {
        "considered": considered,
        "named": named,
        "still_unnamed": considered - named,
        "claude_files_matched": claude_files_matched,
    }
