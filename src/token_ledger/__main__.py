"""Token Ledger CLI Entry Point.

Provides subcommands for local service operation and explicit data operations:
- serve: Start the secure local loopback HTTP service
- ingest: Incrementally ingest a specified log file
- archive: Import an archive partition JSON bundle
- migrate: Run database schema migrations
- reports run-due: Run due scheduled reports and generate local exports
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from token_ledger.api.server import DEFAULT_HOST, DEFAULT_PORT, is_loopback, start_server
from token_ledger.store.database import get_applied_migrations, open_database


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="token_ledger",
        description="Token Ledger: Local-First Observability & Analytics Engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -------------------------------------------------------------------------
    # serve
    # -------------------------------------------------------------------------
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the local loopback HTTP service for the Token Ledger dashboard and API",
    )
    serve_parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database file",
    )
    serve_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host interface to bind (loopback only, default: {DEFAULT_HOST})",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port number to bind (default: {DEFAULT_PORT})",
    )
    serve_parser.add_argument(
        "--web-dist",
        default=os.path.join(os.getcwd(), "web", "dist"),
        help="Path to pre-built static UI directory (default: web/dist)",
    )

    # -------------------------------------------------------------------------
    # ingest
    # -------------------------------------------------------------------------
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Incrementally ingest an explicit log file path into Token Ledger",
    )
    ingest_parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database file",
    )
    ingest_parser.add_argument(
        "--file",
        required=True,
        help="Explicit path to source log file (no automatic collection)",
    )
    ingest_parser.add_argument(
        "--adapter",
        required=True,
        choices=["omp", "claude-code"],
        help="Source adapter identifier ('omp' or 'claude-code')",
    )
    ingest_parser.add_argument(
        "--harness",
        choices=["omp", "claude-code"],
        default=None,
        help="Explicit origin harness override",
    )
    ingest_parser.add_argument(
        "--account",
        default=None,
        help="Account alias for scoping (default: {adapter}_local)",
    )
    ingest_parser.add_argument(
        "--provider",
        default=None,
        help="Explicit provider label override",
    )
    ingest_parser.add_argument(
        "--route",
        default=None,
        help="Explicit access route label override",
    )

    # -------------------------------------------------------------------------
    # archive
    # -------------------------------------------------------------------------
    archive_parser = subparsers.add_parser(
        "archive",
        help="Import an explicit archive bundle JSON file into Token Ledger",
    )
    archive_parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database file",
    )
    archive_parser.add_argument(
        "--file",
        required=True,
        help="Explicit path to archive JSON bundle file",
    )
    archive_parser.add_argument(
        "--account",
        default=None,
        help="Account alias for scoping (default: default)",
    )
    archive_parser.add_argument(
        "--project",
        default=None,
        help="Project ID for scoping",
    )

    # -------------------------------------------------------------------------
    # migrate
    # -------------------------------------------------------------------------
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Apply database schema migrations",
    )
    migrate_parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database file",
    )

    # -------------------------------------------------------------------------
    # backfill-session-ids
    # -------------------------------------------------------------------------
    backfill_parser = subparsers.add_parser(
        "backfill-session-ids",
        help="Fill NULL session ids on oh-my-pi requests from their transcript header (idempotent)",
    )
    backfill_parser.add_argument(
        "--db",
        required=True,
        help="Path to existing SQLite database file",
    )

    # -------------------------------------------------------------------------
    # backfill-omp-project-ids
    # -------------------------------------------------------------------------
    backfill_projects_parser = subparsers.add_parser(
        "backfill-omp-project-ids",
        help="Fill NULL project ids on oh-my-pi requests from their transcript header cwd (idempotent)",
    )
    backfill_projects_parser.add_argument(
        "--db",
        required=True,
        help="Path to existing SQLite database file",
    )

    # -------------------------------------------------------------------------
    # backfill-project-names
    # -------------------------------------------------------------------------
    backfill_names_parser = subparsers.add_parser(
        "backfill-project-names",
        help="Set project_name to the folder base name where it still equals project_id (idempotent)",
    )
    backfill_names_parser.add_argument(
        "--db",
        required=True,
        help="Path to existing SQLite database file (a copy, never the verified database)",
    )
    backfill_names_parser.add_argument(
        "--claude-root",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code projects directory whose logs are matched against stored locators",
    )

    # -------------------------------------------------------------------------
    # recompute-overlap-flags
    # -------------------------------------------------------------------------
    recompute_parser = subparsers.add_parser(
        "recompute-overlap-flags",
        help="Re-derive is_unresolved_overlap from stored evidence (idempotent; run after backfill-session-ids)",
    )
    recompute_parser.add_argument(
        "--db",
        required=True,
        help="Path to existing SQLite database file",
    )

    # -------------------------------------------------------------------------
    # reports
    # -------------------------------------------------------------------------
    reports_parser = subparsers.add_parser(
        "reports",
        help="Run local scheduled report jobs",
    )
    reports_subparsers = reports_parser.add_subparsers(dest="reports_command", required=True)
    run_due_parser = reports_subparsers.add_parser(
        "run-due",
        help="Generate exports for every currently due scheduled report",
    )
    run_due_parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database file",
    )
    run_due_parser.add_argument(
        "--managed-root",
        default=None,
        help="Managed export directory (default: project-local data/exports)",
    )
    run_due_parser.add_argument(
        "--now",
        default=None,
        help="Optional ISO-8601 instant for due checks and report timestamps",
    )

    # -------------------------------------------------------------------------
    # reports daemon
    # -------------------------------------------------------------------------
    daemon_parser = reports_subparsers.add_parser(
        "daemon",
        help="Run unattended reports scheduler daemon or generate service configs",
    )
    daemon_parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database file",
    )
    daemon_parser.add_argument(
        "--managed-root",
        default=None,
        help="Managed export directory (default: project-local data/exports)",
    )
    daemon_parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Scheduler polling interval in seconds (default: 300)",
    )
    daemon_parser.add_argument(
        "--systemd",
        action="store_true",
        help="Print systemd service and timer unit configuration and exit",
    )
    daemon_parser.add_argument(
        "--cron",
        action="store_true",
        help="Print crontab entry for unattended report scheduling and exit",
    )



    args = parser.parse_args(argv)

    if args.command == "serve":
        if not is_loopback(args.host):
            sys.stderr.write(
                f"Error: Host '{args.host}' is not a loopback address. "
                "Token Ledger strictly enforces loopback-only binding (127.0.0.1, ::1, localhost).\n"
            )
            return 1
        start_server(
            db_path=args.db,
            host=args.host,
            port=args.port,
            web_dist_dir=args.web_dist,
        )
        return 0

    elif args.command == "ingest":
        from token_ledger.ingest.runner import ingest_file

        if not os.path.isfile(args.file):
            sys.stderr.write(f"Error: Log file not found: {args.file}\n")
            return 1

        conn = open_database(args.db, run_migrations=True)
        try:
            res = ingest_file(
                conn=conn,
                path=args.file,
                source_adapter=args.adapter,
                account_alias=args.account,
                provider=args.provider,
                access_route=args.route,
                origin_harness=args.harness,
            )
            print(json.dumps(res.to_dict(), indent=2))
            return 0 if res.status in ("ok", "partial") else 1
        finally:
            conn.close()

    elif args.command == "archive":
        from token_ledger.ingest.archive import import_archive_file

        if not os.path.isfile(args.file):
            sys.stderr.write(f"Error: Archive file not found: {args.file}\n")
            return 1

        conn = open_database(args.db, run_migrations=True)
        try:
            summary = import_archive_file(
                conn=conn,
                file_path=args.file,
                account_alias=args.account,
                project_id=args.project,
            )
            print(json.dumps(summary.to_dict(), indent=2))
            return 0
        finally:
            conn.close()

    elif args.command == "migrate":
        conn = open_database(args.db, run_migrations=True)
        try:
            migrations = get_applied_migrations(conn)
            print(f"Applied migrations on {args.db}:")
            for v, name, checksum in migrations:
                print(f"  [{v:03d}] {name} (sha256: {checksum[:8]}...)")
            return 0
        finally:
            conn.close()

    elif args.command == "reports" and args.reports_command == "daemon":
        if args.systemd:
            db_disp = args.db or "/path/to/token-ledger.sqlite3"
            root_disp = f" --managed-root {args.managed_root}" if args.managed_root else ""
            print(f"""# /etc/systemd/system/token-ledger-reports.service
[Unit]
Description=Token Ledger Scheduled Reports Runner
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/token-tracker
ExecStart=/usr/bin/python3 -m token_ledger reports run-due --db {db_disp}{root_disp}
User=token-ledger
Group=token-ledger

# /etc/systemd/system/token-ledger-reports.timer
[Unit]
Description=Run Token Ledger Scheduled Reports every {args.interval}s

[Timer]
OnBootSec=2min
OnUnitActiveSec={args.interval}s
Unit=token-ledger-reports.service

[Install]
WantedBy=timers.target""")
            return 0

        if args.cron:
            db_disp = args.db or "/path/to/token-ledger.sqlite3"
            root_disp = f" --managed-root {args.managed_root}" if args.managed_root else ""
            mins = max(1, args.interval // 60)
            cron_interval = f"*/{mins} * * * *" if mins < 60 else "0 * * * *"
            print(f"""# Token Ledger unattended reports runner (every {args.interval}s)
{cron_interval} cd /path/to/token-tracker && PYTHONPATH=src python3 -m token_ledger reports run-due --db {db_disp}{root_disp} >> /var/log/token-ledger-reports.log 2>&1""")
            return 0

        if not args.db:
            sys.stderr.write("Error: --db is required to run the reports daemon\n")
            return 1

        import time
        from token_ledger.export import run_due_reports_scheduler

        conn = open_database(args.db, run_migrations=True)
        print(f"Token Ledger reports daemon started (interval: {args.interval}s, db: {args.db})", file=sys.stderr)
        try:
            while True:
                try:
                    results = run_due_reports_scheduler(
                        conn,
                        managed_root=args.managed_root,
                    )
                    if results:
                        print(
                            json.dumps(
                                {
                                    "count": len(results),
                                    "executed": [result.to_dict() for result in results],
                                },
                                indent=2,
                                ensure_ascii=False,
                            )
                        )
                except Exception as exc:
                    sys.stderr.write(f"Scheduler execution error: {exc}\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Reports daemon stopped.", file=sys.stderr)
        finally:
            conn.close()
        return 0

    elif args.command == "backfill-session-ids":
        from token_ledger.ingest.session_backfill import backfill_omp_session_ids

        if not os.path.isfile(args.db):
            sys.stderr.write(f"Error: Database not found: {args.db}\n")
            return 1

        conn = open_database(args.db, run_migrations=False)
        try:
            summary = backfill_omp_session_ids(conn)
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            conn.close()

    elif args.command == "backfill-omp-project-ids":
        from token_ledger.ingest.session_backfill import backfill_omp_project_ids

        if not os.path.isfile(args.db):
            sys.stderr.write(f"Error: Database not found: {args.db}\n")
            return 1

        conn = open_database(args.db, run_migrations=False)
        try:
            summary = backfill_omp_project_ids(conn)
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            conn.close()

    elif args.command == "backfill-project-names":
        from token_ledger.ingest.project_names import backfill_project_names

        if "/verified/" in args.db or "/verified/" in os.path.realpath(args.db):
            sys.stderr.write("Error: refusing to write a path under verified/; use a copy of the database.\n")
            return 1
        if not os.path.isfile(args.db):
            sys.stderr.write(f"Error: Database not found: {args.db}\n")
            return 1

        conn = open_database(args.db, run_migrations=False)
        try:
            summary = backfill_project_names(conn, claude_root=args.claude_root)
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            conn.close()

    elif args.command == "recompute-overlap-flags":
        from token_ledger.ingest.overlap import recompute_overlap_flags

        if not os.path.isfile(args.db):
            sys.stderr.write(f"Error: Database not found: {args.db}\n")
            return 1

        conn = open_database(args.db, run_migrations=False)
        try:
            with conn:
                summary = recompute_overlap_flags(conn)
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            conn.close()

    elif args.command == "reports" and args.reports_command == "run-due":
        from token_ledger.export import run_due_reports_scheduler

        conn = open_database(args.db, run_migrations=True)
        try:
            results = run_due_reports_scheduler(
                conn,
                managed_root=args.managed_root,
                now_iso=args.now,
            )
            print(
                json.dumps(
                    {
                        "count": len(results),
                        "executed": [result.to_dict() for result in results],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        finally:
            conn.close()


    return 0


if __name__ == "__main__":
    sys.exit(main())
