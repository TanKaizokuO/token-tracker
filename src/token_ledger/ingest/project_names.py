"""Name projects by their folder base name, and fold Claude Code worktrees into their parent repo.

project_id stays the opaque hash, proj_<sha256(basename)[:12]>. This pass sets project_name and
re-homes worktree projects. Candidate cwds come from logs the database already references:

- oh-my-pi: session header observations keep the header cwd basename as provenance cwd_alias.
- Claude Code: the database stores hashed file locators, loc_<sha256(path)[:16]>. Files under
  claude_root whose locator matches a stored Claude Code locator are read for their "cwd" fields.

Worktree fold: a cwd inside .claude/worktrees/<name> belongs to its parent repo. Rows under the
old worktree project id (hash of the worktree folder name) are re-assigned to the parent project
id (hash of the parent folder name) in every table with a project_id column. The old project row
is deleted once nothing references it. The whole pass runs in one transaction, and it is idempotent.

Only names still equal to project_id are updated, and a user-set name is never overwritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from token_ledger.ingest.runner import _sanitize_project_identifier
from token_ledger.ingest.worktrees import is_worktree_cwd, project_folder_name
from token_ledger.privacy import sanitize_file_locator


def _note_candidate(raw_cwd: Any, wanted: Set[str], found: Dict[str, str]) -> None:
    """Record the folder name of raw_cwd under its ingest-style project id, if that id is unnamed."""
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return
    name = project_folder_name(raw_cwd)
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


def _claude_cwds(conn: sqlite3.Connection, claude_root: Optional[str]) -> Tuple[int, Set[str]]:
    """Read distinct cwd values from Claude Code files the database has ingested.

    Returns (files matched, distinct cwd strings). Nothing is written.
    """
    if not claude_root or not os.path.isdir(claude_root):
        return 0, set()
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT file_locator FROM source_observations WHERE source_adapter = 'claude_code';"
        ).fetchall()
    }
    matched = 0
    cwds: Set[str] = set()
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
                        if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                            cwds.add(record["cwd"])
            except OSError:
                continue
    return matched, cwds


def _tables_with_project_id(conn: sqlite3.Connection) -> List[str]:
    """Every table except projects that has a project_id column."""
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'projects';"
        ).fetchall()
    ]
    return [
        name
        for name in names
        if any(column[1] == "project_id" for column in conn.execute(f'PRAGMA table_info("{name}");').fetchall())
    ]


def _reference_count(conn: sqlite3.Connection, tables: List[str], project_id: str) -> int:
    total = 0
    for table in tables:
        total += conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE project_id = ?;', (project_id,)).fetchone()[0]
    return total


def _fold_worktrees(conn: sqlite3.Connection, cwds: Set[str]) -> Tuple[int, int]:
    """Re-home rows of worktree projects under their parent repo project. Runs inside the caller's transaction.

    Returns (worktree projects folded, rows reassigned). An old worktree id that maps to more than one
    parent is ambiguous and is left alone.
    """
    targets: Dict[str, Dict[str, str]] = {}
    for cwd in cwds:
        if not is_worktree_cwd(cwd):
            continue
        parent_name = project_folder_name(cwd)
        old_name = os.path.basename(cwd.strip().rstrip("/\\"))
        if not parent_name or not old_name:
            continue
        old_id = _sanitize_project_identifier(old_name)
        new_id = _sanitize_project_identifier(parent_name)
        if old_id is None or new_id is None or old_id == new_id:
            continue
        targets.setdefault(old_id, {})[new_id] = parent_name

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tables = _tables_with_project_id(conn)
    folded = 0
    reassigned = 0
    for old_id in sorted(targets):
        if len(targets[old_id]) != 1:
            continue
        (new_id, parent_name), = targets[old_id].items()
        old_row = conn.execute(
            "SELECT account_alias FROM projects WHERE project_id = ?;", (old_id,)
        ).fetchone()
        if old_row is None and _reference_count(conn, tables, old_id) == 0:
            continue
        account_alias = old_row[0] if old_row is not None else "default"
        conn.execute(
            """
            INSERT OR IGNORE INTO projects (
                project_id, project_name, project_alias, account_alias, is_active, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, 1, ?, '{}');
            """,
            (new_id, parent_name, new_id, account_alias, now),
        )
        conn.execute(
            "UPDATE projects SET project_name = ? WHERE project_id = ? AND project_name = project_id;",
            (parent_name, new_id),
        )
        moved = 0
        for table in tables:
            moved += conn.execute(
                f'UPDATE "{table}" SET project_id = ? WHERE project_id = ?;', (new_id, old_id)
            ).rowcount
        reassigned += moved
        conn.execute("DELETE FROM projects WHERE project_id = ?;", (old_id,))
        if old_row is not None or moved:
            folded += 1
    return folded, reassigned


def backfill_project_names(conn: sqlite3.Connection, claude_root: Optional[str] = None) -> Dict[str, int]:
    """Fold worktree projects into their parent repo, then set project_name where it still equals project_id.

    Returns counts: considered (unnamed projects after the fold), named (updated by this run),
    still_unnamed (no name derivable), claude_files_matched (diagnostic), worktree_projects_folded
    (old worktree projects re-homed by this run), and rows_reassigned (rows whose project_id changed).
    The whole pass is one transaction. A second run returns zero for the fold and naming counts.
    """
    claude_files_matched, cwds = _claude_cwds(conn, claude_root)
    with conn:
        worktree_projects_folded, rows_reassigned = _fold_worktrees(conn, cwds)
        wanted: Set[str] = {
            row[0]
            for row in conn.execute("SELECT project_id FROM projects WHERE project_name = project_id;").fetchall()
        }
        found: Dict[str, str] = {}
        if wanted:
            _omp_candidates(conn, wanted, found)
            for cwd in sorted(cwds):
                _note_candidate(cwd, wanted, found)

        named = 0
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
        "worktree_projects_folded": worktree_projects_folded,
        "rows_reassigned": rows_reassigned,
    }
