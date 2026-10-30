"""Claude Code subagent worktrees belong to their parent repository.

A worktree cwd looks like <repo>/.claude/worktrees/<name>, and may continue into a subfolder
(<repo>/.claude/worktrees/<name>/src). Both fold into <repo>. Separators may be POSIX or Windows.
Paths without that segment are returned unchanged.
"""

from __future__ import annotations

import os
import re
from typing import Optional

_WORKTREE_SEGMENT = re.compile(r"(?:^|[\\/])\.claude[\\/]worktrees[\\/][^\\/]+")


def fold_worktree_cwd(cwd: str) -> str:
    """Return the parent repository path of a worktree cwd, or cwd unchanged when it is not one."""
    match = _WORKTREE_SEGMENT.search(cwd)
    if match is None:
        return cwd
    parent = cwd[: match.start()]
    if not parent.strip("/\\"):
        return cwd
    return parent


def is_worktree_cwd(cwd: str) -> bool:
    """True when cwd sits inside a .claude/worktrees/<name> folder with a parent repository."""
    return fold_worktree_cwd(cwd) != cwd


def project_folder_name(cwd: str) -> Optional[str]:
    """Project folder name for a cwd: the parent repo's base name for worktrees, else the base name.

    Returns None when no name can be derived. This is the single place that decides a project's
    name from a cwd, so ingest, the name backfill, and the worktree fold all agree.
    """
    cleaned = cwd.strip().rstrip("/\\")
    if not cleaned:
        return None
    if is_worktree_cwd(cleaned):
        parent = fold_worktree_cwd(cleaned).rstrip("/\\")
        return re.split(r"[\\/]", parent)[-1] or None
    return os.path.basename(cleaned) or None
