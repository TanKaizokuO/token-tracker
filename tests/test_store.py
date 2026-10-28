"""Unit and integration tests for Token Ledger SQLite storage and migration runner.

Verifies:
- Migration execution, version tracking, and idempotency (001_initial and 002_archive_reconciliation)
- Migration 001 -> 002 row preservation, schema expansion, and nullable cost semantics
- Table existence across all canonical schema domains
- Foreign key enforcement and cascading deletion
- Provider/account-scoped identity constraints accepting source-local account aliases
- Scoped retention foreign keys on archive_partitions, export_inventory, and alerts_and_reports
- Live observations vs archive partition independence
- Transaction integrity and on-disk database reopening
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from token_ledger.store.database import (
    DEFAULT_MIGRATIONS_DIR,
    connect,
    get_applied_migrations,
    get_schema_version,
    migrate,
    open_database,
    table_exists,
)


class TestStoreMigrations(unittest.TestCase):
    """Test schema migration runner behavior, version tracking, and cross-version upgrades."""

    def test_migration_and_idempotency(self):
        conn = connect(":memory:")
        self.assertEqual(get_schema_version(conn), 0)

        applied = migrate(conn)
        self.assertEqual(len(applied), 6)
        self.assertEqual(applied[0][0], 1)
        self.assertEqual(applied[0][1], "initial")
        self.assertEqual(applied[1][0], 2)
        self.assertEqual(applied[1][1], "archive_reconciliation_and_scoping")
        self.assertEqual(applied[2][0], 3)
        self.assertEqual(applied[2][1], "export_integrity")
        self.assertEqual(applied[3], (4, "valuation_provenance"))
        self.assertEqual(applied[4], (5, "subscription_fees"))
        self.assertEqual(applied[5], (6, "checkpoint_file_identity"))

        self.assertEqual(get_schema_version(conn), 6)
        applied_list = get_applied_migrations(conn)
        self.assertEqual(len(applied_list), 6)
        self.assertEqual(applied_list[0][0], 1)
        self.assertEqual(applied_list[1][0], 2)
        self.assertEqual(applied_list[2][0], 3)
        self.assertEqual(applied_list[3][0], 4)
        self.assertEqual(applied_list[4][0], 5)
        self.assertEqual(applied_list[5][0], 6)

        # Second migration call must be an idempotent no-op
        second_applied = migrate(conn)
        self.assertEqual(len(second_applied), 0)
        self.assertEqual(get_schema_version(conn), 6)
        conn.close()

    def test_open_database_convenience_helper(self):
        conn = open_database(":memory:", run_migrations=True)
        self.assertEqual(get_schema_version(conn), 6)
        self.assertTrue(table_exists(conn, "canonical_requests"))
        self.assertTrue(table_exists(conn, "archive_partitions"))
        conn.close()

    def test_migrate_001_existing_db_to_latest_preservation(self):
        """Upgrade an existing migration-001 store through all migrations and preserve seeded data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy_001.db")
            conn = connect(db_path)

            # Step 1: Run ONLY 001 migration from a single-file directory
            m1_dir = os.path.join(tmpdir, "m1_only")
            os.makedirs(m1_dir, exist_ok=True)
            shutil.copyfile(
                os.path.join(DEFAULT_MIGRATIONS_DIR, "001_initial.sql"),
                os.path.join(m1_dir, "001_initial.sql"),
            )
            applied_001 = migrate(conn, migrations_dir=m1_dir)
            self.assertEqual(len(applied_001), 1)
            self.assertEqual(get_schema_version(conn), 1)

            # Seed 001 schema with project, session, archive partition, export, and report rows
            with conn:
                conn.execute(
                    "INSERT INTO projects (project_id, project_name, created_at) VALUES (?, ?, ?);",
                    ("proj_test", "Test Project", "2026-10-26T10:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO sessions (session_id, origin_harness, created_at) VALUES (?, ?, ?);",
                    ("sess_test", "claude-code", "2026-10-26T10:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO canonical_requests (canonical_id, provider, origin_harness, raw_model, normalized_model, request_timestamp, created_at) VALUES ('old-request', 'example', 'synthetic', 'model', 'model', '2026-01-01', '2026-01-01')"
                )
                conn.execute(
                    "INSERT INTO financial_facts (canonical_id, actual_billed_amount, actual_billed_currency, api_equivalent_usd, calculated_at) VALUES ('old-request', 7, 'EUR', 5, '2026-01-01')"
                )
                conn.execute(
                    """
                    INSERT INTO archive_partitions (
                        partition_id, partition_type, grain, source_name, start_time, end_time,
                        total_tokens, total_cost_usd, records_count, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "arch_pre_002",
                        "legacy_archive",
                        "daily",
                        "tokentrack",
                        "2026-09-01T00:00:00Z",
                        "2026-09-30T23:59:59Z",
                        50000,
                        15.75,
                        120,
                        '{"note": "pre-migration"}',
                        "2026-10-26T10:00:00Z",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO export_inventory (
                        export_id, export_format, file_path, created_at
                    ) VALUES (?, ?, ?, ?);
                    """,
                    ("exp_pre_002", "json", "/tmp/exp1.json", "2026-10-26T10:00:00Z"),
                )
                conn.execute(
                    """
                    INSERT INTO alerts_and_reports (
                        item_id, item_type, name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    ("alert_pre_002", "alert", "Weekly Threshold", "2026-10-26T10:00:00Z", "2026-10-26T10:00:00Z"),
                )
            # Close connection to simulate persistent disk store before upgrade
            conn.close()

            # Step 2: Open and apply pending migrations from DEFAULT_MIGRATIONS_DIR
            conn_upgraded = open_database(db_path, run_migrations=True)
            self.assertEqual(get_schema_version(conn_upgraded), 6)
            old_fact = conn_upgraded.execute(
                "SELECT actual_billed_amount, actual_billed_currency, api_equivalent_usd, valuation_provenance_json FROM financial_facts WHERE canonical_id='old-request'"
            ).fetchone()
            self.assertEqual(tuple(old_fact), (7, "EUR", 5, None))

            # Step 3: Verify pre-existing data was preserved exactly
            cur = conn_upgraded.cursor()
            cur.execute(
                """
                SELECT partition_id, total_tokens, total_cost_usd, pricing_status, account_alias, project_id, payload_json
                FROM archive_partitions WHERE partition_id = 'arch_pre_002';
                """
            )
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "arch_pre_002")
            self.assertEqual(row[1], 50000)
            self.assertEqual(row[2], 15.75)
            self.assertEqual(row[3], "priced")
            self.assertEqual(row[4], "default")
            self.assertIsNone(row[5])
            self.assertIn("pre-migration", row[6])
            cur.execute(
                "SELECT sha256_hash, byte_size FROM export_inventory WHERE export_id = ?;",
                ("exp_pre_002",),
            )
            legacy_export = cur.fetchone()
            self.assertIsNotNone(legacy_export)
            self.assertEqual(tuple(legacy_export), ("", 0))


            # Step 4: Verify NULL total_cost_usd vs 0.0 distinction works in 002
            with conn_upgraded:
                # Row with unknown cost (NULL)
                conn_upgraded.execute(
                    """
                    INSERT INTO archive_partitions (
                        partition_id, partition_type, grain, source_name, start_time, end_time,
                        total_tokens, total_cost_usd, pricing_status, missingness_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "arch_unknown_cost",
                        "legacy_archive",
                        "daily",
                        "omp_history",
                        "2026-08-01T00:00:00Z",
                        "2026-08-31T23:59:59Z",
                        20000,
                        None,  # Unknown cost is NULL
                        "unpriced",
                        '{"missing_prices": ["unknown-model-x"]}',
                        "2026-10-26T10:05:00Z",
                    ),
                )
                # Row with measured zero cost (0.0)
                conn_upgraded.execute(
                    """
                    INSERT INTO archive_partitions (
                        partition_id, partition_type, grain, source_name, start_time, end_time,
                        total_tokens, total_cost_usd, pricing_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "arch_zero_cost",
                        "legacy_archive",
                        "daily",
                        "free_tier",
                        "2026-07-01T00:00:00Z",
                        "2026-07-31T23:59:59Z",
                        10000,
                        0.0,  # Measured zero is 0.0
                        "priced",
                        "2026-10-26T10:06:00Z",
                    ),
                )

            # Query NULL vs 0.0
            cur.execute("SELECT partition_id FROM archive_partitions WHERE total_cost_usd IS NULL;")
            null_cost_rows = [r[0] for r in cur.fetchall()]
            self.assertEqual(null_cost_rows, ["arch_unknown_cost"])

            cur.execute("SELECT partition_id FROM archive_partitions WHERE total_cost_usd = 0.0;")
            zero_cost_rows = [r[0] for r in cur.fetchall()]
            self.assertEqual(zero_cost_rows, ["arch_zero_cost"])

            # Step 5: Verify new scoped foreign keys and ON DELETE SET NULL cascades
            with conn_upgraded:
                # Associate archive partition, export, and alert with proj_test
                conn_upgraded.execute(
                    "UPDATE archive_partitions SET project_id = 'proj_test' WHERE partition_id = 'arch_pre_002';"
                )
                conn_upgraded.execute(
                    "UPDATE export_inventory SET project_id = 'proj_test', session_id = 'sess_test' WHERE export_id = 'exp_pre_002';"
                )
                conn_upgraded.execute(
                    "UPDATE alerts_and_reports SET project_id = 'proj_test' WHERE item_id = 'alert_pre_002';"
                )

            # Verify associations are set
            cur.execute("SELECT project_id FROM archive_partitions WHERE partition_id = 'arch_pre_002';")
            self.assertEqual(cur.fetchone()[0], "proj_test")
            cur.execute("SELECT project_id, session_id FROM export_inventory WHERE export_id = 'exp_pre_002';")
            self.assertEqual(tuple(cur.fetchone()), ("proj_test", "sess_test"))

            # Delete session -> export_inventory session_id becomes NULL, export row preserved
            with conn_upgraded:
                conn_upgraded.execute("DELETE FROM sessions WHERE session_id = 'sess_test';")
            cur.execute("SELECT session_id, project_id FROM export_inventory WHERE export_id = 'exp_pre_002';")
            self.assertEqual(tuple(cur.fetchone()), (None, "proj_test"))

            # Delete project -> project_id becomes NULL on archive, export, and alert rows
            with conn_upgraded:
                conn_upgraded.execute("DELETE FROM projects WHERE project_id = 'proj_test';")
            cur.execute("SELECT project_id FROM archive_partitions WHERE partition_id = 'arch_pre_002';")
            self.assertEqual(cur.fetchone()[0], None)
            cur.execute("SELECT project_id FROM alerts_and_reports WHERE item_id = 'alert_pre_002';")
            self.assertEqual(cur.fetchone()[0], None)
            cur.execute("SELECT project_id FROM export_inventory WHERE export_id = 'exp_pre_002';")
            self.assertEqual(cur.fetchone()[0], None)

            conn_upgraded.close()


class TestCanonicalSchemaEntities(unittest.TestCase):
    """Verify table structures, foreign keys, constraints, and cascade deletions."""

    def setUp(self):
        self.conn = open_database(":memory:", run_migrations=True)

    def tearDown(self):
        self.conn.close()

    def test_all_expected_tables_exist(self):
        expected_tables = [
            "schema_migrations",
            "source_checkpoints",
            "source_observations",
            "projects",
            "sessions",
            "prompts",
            "pricing_versions",
            "model_pricing_rates",
            "historical_fx",
            "canonical_requests",
            "request_observation_links",
            "normalized_usage",
            "financial_facts",
            "events",
            "subscription_quotas",
            "archive_partitions",
            "manual_settings_and_outcomes",
            "privacy_settings",
            "saved_views_and_layouts",
            "alerts_and_reports",
            "export_inventory",
        ]
        for tbl in expected_tables:
            self.assertTrue(table_exists(self.conn, tbl), f"Table {tbl} must exist")

    def test_foreign_key_enforcement(self):
        # Inserting canonical_request referencing non-existent session_id must fail
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO canonical_requests (
                        canonical_id, provider, account_alias, origin_harness,
                        raw_model, normalized_model, session_id, request_timestamp, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "req_test_001",
                        "anthropic",
                        "default",
                        "claude-code",
                        "claude-3-7-sonnet",
                        "claude-3-7-sonnet",
                        "non_existent_session_id",
                        "2026-10-26T12:00:00Z",
                        "2026-10-26T12:00:00Z",
                    ),
                )

    def test_cascading_deletion_on_session(self):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sessions (
                    session_id, origin_harness, account_alias, created_at
                ) VALUES (?, ?, ?, ?);
                """,
                ("sess_001", "omp", "default", "2026-10-26T12:00:00Z"),
            )
            self.conn.execute(
                """
                INSERT INTO prompts (prompt_id, session_id, created_at)
                VALUES (?, ?, ?);
                """,
                ("p_001", "sess_001", "2026-10-26T12:00:00Z"),
            )
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, origin_harness,
                    raw_model, normalized_model, session_id, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "req_001",
                    "google",
                    "default",
                    "omp",
                    "gemini-3.8-flash",
                    "gemini-3.8-flash",
                    "sess_001",
                    "2026-10-26T12:00:00Z",
                    "2026-10-26T12:00:00Z",
                ),
            )
            self.conn.execute(
                """
                INSERT INTO normalized_usage (
                    canonical_id, fresh_input_tokens, output_tokens
                ) VALUES (?, ?, ?);
                """,
                ("req_001", 100, 20),
            )
            self.conn.execute(
                """
                INSERT INTO events (
                    event_id, session_id, canonical_id, event_kind, event_name, started_at
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                ("evt_001", "sess_001", "req_001", "tool_use", "read", "2026-10-26T12:00:01Z"),
            )

        # Confirm rows exist
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests WHERE session_id = 'sess_001';")
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT COUNT(*) FROM normalized_usage WHERE canonical_id = 'req_001';")
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT COUNT(*) FROM events WHERE session_id = 'sess_001';")
        self.assertEqual(cur.fetchone()[0], 1)

        # Deleting parent session must cascade delete prompts, requests, usage, and events
        with self.conn:
            self.conn.execute("DELETE FROM sessions WHERE session_id = 'sess_001';")

        cur.execute("SELECT COUNT(*) FROM prompts WHERE prompt_id = 'p_001';")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT COUNT(*) FROM canonical_requests WHERE canonical_id = 'req_001';")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT COUNT(*) FROM normalized_usage WHERE canonical_id = 'req_001';")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT COUNT(*) FROM events WHERE event_id = 'evt_001';")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_provider_account_scoped_identity_uniqueness(self):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id, origin_harness,
                    raw_model, normalized_model, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "can_1",
                    "anthropic",
                    "acct_a",
                    "req_shared_id",
                    "claude-code",
                    "claude-sonnet-5-5",
                    "claude-sonnet-5-5",
                    "2026-10-26T10:00:00Z",
                    "2026-10-26T10:00:00Z",
                ),
            )

        # Same provider_request_id in distinct account MUST succeed
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id, origin_harness,
                    raw_model, normalized_model, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "can_2",
                    "anthropic",
                    "acct_b",
                    "req_shared_id",
                    "claude-code",
                    "claude-sonnet-5-5",
                    "claude-sonnet-5-5",
                    "2026-10-26T10:00:00Z",
                    "2026-10-26T10:00:00Z",
                ),
            )

        # Duplicate (provider, account_alias, provider_request_id) MUST fail
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO canonical_requests (
                        canonical_id, provider, account_alias, provider_request_id, origin_harness,
                        raw_model, normalized_model, request_timestamp, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "can_3",
                        "anthropic",
                        "acct_a",
                        "req_shared_id",
                        "claude-code",
                        "claude-sonnet-5-5",
                        "claude-sonnet-5-5",
                        "2026-10-26T10:00:00Z",
                        "2026-10-26T10:00:00Z",
                    ),
                )

    def test_source_local_account_aliases_and_fallback_identity(self):
        """Verify normalization supplied source-local aliases and fallback identities."""
        # 1. Source-local account aliases (e.g. omp:sess_1 vs omp:sess_2 or claude:default)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id, origin_harness,
                    raw_model, normalized_model, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "req_omp_1",
                    "google",
                    "omp:session_alpha",
                    "resp_gemini_common_id",
                    "omp",
                    "gemini-3.8-flash",
                    "gemini-3.8-flash",
                    "2026-10-26T11:00:00Z",
                    "2026-10-26T11:00:00Z",
                ),
            )
            # Distinct source-local account alias with same provider request ID succeeds
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id, origin_harness,
                    raw_model, normalized_model, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "req_omp_2",
                    "google",
                    "omp:session_beta",
                    "resp_gemini_common_id",
                    "omp",
                    "gemini-3.8-flash",
                    "gemini-3.8-flash",
                    "2026-10-26T11:00:00Z",
                    "2026-10-26T11:00:00Z",
                ),
            )

        # 2. Requests without provider_request_id (fallback identity) can coexist within same account
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id, fallback_identity, origin_harness,
                    raw_model, normalized_model, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "fallback_can_1",
                    "openai",
                    "default",
                    None,
                    "evt_omp_fallback_001",
                    "omp",
                    "gpt-6.1-sol",
                    "gpt-6.1-sol",
                    "2026-10-26T11:05:00Z",
                    "2026-10-26T11:05:00Z",
                ),
            )
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, provider_request_id, fallback_identity, origin_harness,
                    raw_model, normalized_model, request_timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "fallback_can_2",
                    "openai",
                    "default",
                    None,
                    "evt_omp_fallback_002",
                    "omp",
                    "gpt-6.1-sol",
                    "gpt-6.1-sol",
                    "2026-10-26T11:06:00Z",
                    "2026-10-26T11:06:00Z",
                ),
            )

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM canonical_requests WHERE provider = 'google';")
        self.assertEqual(cur.fetchone()[0], 2)
        cur.execute("SELECT COUNT(*) FROM canonical_requests WHERE provider = 'openai';")
        self.assertEqual(cur.fetchone()[0], 2)

    def test_archive_partitions_distinct_from_live(self):
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO archive_partitions (
                    partition_id, partition_type, grain, source_name, start_time, end_time,
                    total_tokens, total_cost_usd, records_count, pricing_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "part_archive_202609",
                    "legacy_splice",
                    "daily",
                    "tokentrack_archive",
                    "2026-09-01T00:00:00Z",
                    "2026-09-30T23:59:59Z",
                    1250000,
                    12.45,
                    540,
                    "priced",
                    "2026-10-26T12:00:00Z",
                ),
            )

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM archive_partitions;")
        self.assertEqual(cur.fetchone()[0], 1)
        # Live canonical requests table remains independent
        cur.execute("SELECT COUNT(*) FROM canonical_requests;")
        self.assertEqual(cur.fetchone()[0], 0)


class TestOnDiskReopenSmoke(unittest.TestCase):
    """Verify on-disk database creation, migration, insertion, close, and reopen."""

    def test_disk_database_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "smoke.db")

            # 1. Initialize and migrate on disk
            conn1 = open_database(db_path, run_migrations=True)
            self.assertEqual(get_schema_version(conn1), 6)

            with conn1:
                conn1.execute(
                    """
                    INSERT INTO projects (project_id, project_name, created_at)
                    VALUES (?, ?, ?);
                    """,
                    ("p_proj_1", "Project Phoenix", "2026-10-26T10:00:00Z"),
                )
            conn1.close()

            # 2. Reopen existing database and verify data persisted
            conn2 = open_database(db_path, run_migrations=True)
            self.assertEqual(get_schema_version(conn2), 6)

            cur = conn2.cursor()
            cur.execute("SELECT project_name FROM projects WHERE project_id = 'p_proj_1';")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Project Phoenix")
            conn2.close()


if __name__ == "__main__":
    unittest.main()
