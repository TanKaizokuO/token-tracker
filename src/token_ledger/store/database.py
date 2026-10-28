"""SQLite database connection and migration runner for Token Ledger.

Follows SQLite best practices:
- Enforces PRAGMA foreign_keys = ON
- Uses WAL journal mode for concurrent file access
- Applies package-relative versioned migrations idempotently inside transactions
- Standard library Python 3.9+ only
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional, Tuple


DEFAULT_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def connect(
    db_path: str = ":memory:",
    pragmas: bool = True,
    row_factory: bool = True,
) -> sqlite3.Connection:
    """Create and configure a SQLite connection.

    Args:
        db_path: Path to database file or ':memory:'.
        pragmas: Whether to apply WAL, foreign_keys, and busy_timeout pragmas.
        row_factory: Whether to use sqlite3.Row for dict-like row access.

    Returns:
        Configured sqlite3.Connection.
    """
    if db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path)

    if row_factory:
        conn.row_factory = sqlite3.Row

    if pragmas:
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON;")
        cur.execute("PRAGMA busy_timeout = 5000;")
        if db_path != ":memory:":
            cur.execute("PRAGMA journal_mode = WAL;")
            cur.execute("PRAGMA synchronous = NORMAL;")
        cur.close()

    return conn


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensure the schema_migrations table exists before reading or writing."""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
            """
        )


def get_applied_migrations(conn: sqlite3.Connection) -> List[Tuple[int, str, str]]:
    """Return all applied migrations as list of (version, name, checksum)."""
    _ensure_migration_table(conn)
    cur = conn.cursor()
    cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC;")
    rows = cur.fetchall()
    cur.close()
    return [(r[0], r[1], r[2]) for r in rows]


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return highest applied schema migration version, or 0 if none applied."""
    applied = get_applied_migrations(conn)
    return applied[-1][0] if applied else 0


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check whether a table exists in the connected database."""
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
        (table_name,),
    )
    res = cur.fetchone() is not None
    cur.close()
    return res


def migrate(
    conn: sqlite3.Connection,
    migrations_dir: Optional[str] = None,
) -> List[Tuple[int, str]]:
    """Apply any pending SQL migrations from migrations_dir.

    Migrations must follow the filename pattern:
        <version>_<name>.sql (e.g. 001_initial.sql)

    Each migration runs inside its own transaction.

    Args:
        conn: Open SQLite connection.
        migrations_dir: Directory containing .sql migration scripts.
                        Defaults to package-relative store/migrations.

    Returns:
        List of (version, name) tuples for newly applied migrations.
    """
    mdir = migrations_dir or DEFAULT_MIGRATIONS_DIR
    if not os.path.isdir(mdir):
        raise FileNotFoundError(f"Migrations directory not found: {mdir}")

    _ensure_migration_table(conn)

    applied_map = {v: (n, c) for v, n, c in get_applied_migrations(conn)}

    sql_files = []
    for fname in sorted(os.listdir(mdir)):
        if fname.endswith(".sql") and not fname.startswith("."):
            parts = fname.split("_", 1)
            if parts[0].isdigit():
                version = int(parts[0])
                name = parts[1][:-4] if len(parts) > 1 else fname[:-4]
                sql_files.append((version, name, os.path.join(mdir, fname)))

    sql_files.sort(key=lambda x: x[0])

    applied_now = []
    for version, name, fpath in sql_files:
        with open(fpath, "r", encoding="utf-8") as fh:
            sql_content = fh.read()

        checksum = hashlib.sha256(sql_content.encode("utf-8")).hexdigest()

        if version in applied_map:
            prev_name, prev_checksum = applied_map[version]
            if prev_checksum != checksum:
                raise ValueError(
                    f"Migration {version:03d}_{name} checksum mismatch! "
                    f"Applied: {prev_checksum}, file: {checksum}"
                )
            continue

        now_utc = datetime.now(timezone.utc).isoformat()

        # Run script and record migration inside a single transaction
        with conn:
            conn.executescript(sql_content)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at, checksum)
                VALUES (?, ?, ?, ?);
                """,
                (version, name, now_utc, checksum),
            )

        applied_now.append((version, name))

    return applied_now


def open_database(
    db_path: str = ":memory:",
    run_migrations: bool = True,
    migrations_dir: Optional[str] = None,
) -> sqlite3.Connection:
    """Connect to SQLite database and optionally apply pending migrations.

    Stable entrypoint for downstream ingestion, query, and metrics packages.
    """
    conn = connect(db_path)
    if run_migrations:
        migrate(conn, migrations_dir=migrations_dir)
    return conn
