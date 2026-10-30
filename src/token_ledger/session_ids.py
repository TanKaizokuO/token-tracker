"""Session id normalization shared by the metrics and query layers.

A session count is the number of distinct session ids that are non-null and not blank
after stripping whitespace. Ids that fail this check are never a session.
"""

from __future__ import annotations

from typing import Any, Optional


def session_id_or_none(value: Any) -> Optional[str]:
    """Return the session id only when it is a non-blank string; null, empty or whitespace-only ids give None."""
    if isinstance(value, str) and value.strip():
        return value
    return None
