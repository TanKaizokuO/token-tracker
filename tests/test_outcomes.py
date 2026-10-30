"""Tests for Outcome tracking, task labeling, Git linkage, and effectiveness scoring (WP18).

Covers:
1. Input validation & parameterized CRUD operations on manual_settings_and_outcomes.
2. Ownership, source, and versioning boundaries (user vs. inferred separation).
3. Status boundaries: 'complete', 'partial', 'unsuccessful', 'abandoned'.
4. Git linkage consent gate:
   - Consent OFF: strictly performs NO git command.
   - Consent ON: inspects local git metadata on temporary repository without reading source code or transcripts.
5. Metrics calculation and sample adequacy threshold enforcement:
   - Sparse samples abstain (returns None for per-completion metrics with explicit disclaimers).
   - Adequate samples compute tokens_per_completed_task, cost_per_success, time_per_task, etc.
6. Temporary database smoke integration test.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from token_ledger.metrics.outcomes import analyze
from token_ledger.outcomes import (
    DEFAULT_MIN_SAMPLE_ADEQUACY,
    OUTCOMES_POLICY_VERSION,
    OutcomeRecord,
    create_outcome,
    delete_outcome,
    get_outcome,
    get_outcome_for_entity,
    inspect_git_metadata,
    link_git_outcome,
    list_outcomes,
    update_outcome,
    validate_completion_rating,
    validate_confidence,
    validate_entity,
    validate_manual_rating,
    validate_task_label,
)
from token_ledger.privacy import (
    OPTIN_GIT_LINKAGE,
    PRIVACY_POLICY_VERSION,
    save_privacy_setting,
)
from token_ledger.query import execute_query
from token_ledger.store.database import open_database


class TestOutcomeValidationAndCRUD(unittest.TestCase):
    """Test validation logic and parameterized CRUD operations."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        # Load schema for manual_settings_and_outcomes and privacy_settings
        schema_path = Path(__file__).resolve().parent.parent / "src" / "token_ledger" / "store" / "migrations" / "001_initial.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())
            with open(schema_path.with_name("007_duplicate_of.sql"), "r", encoding="utf-8") as f:
                self.conn.executescript(f.read())

    def tearDown(self) -> None:
        self.conn.close()

    def test_validation_entity(self) -> None:
        self.assertEqual(validate_entity("session", "sess-1"), ("session", "sess-1"))
        self.assertEqual(validate_entity("request", "req-100"), ("request", "req-100"))
        with self.assertRaises(ValueError):
            validate_entity("invalid_type", "id-1")
        with self.assertRaises(ValueError):
            validate_entity("session", "")

    def test_validation_completion_rating(self) -> None:
        self.assertEqual(validate_completion_rating("complete"), "complete")
        self.assertEqual(validate_completion_rating("PARTIAL"), "partial")
        self.assertEqual(validate_completion_rating("unsuccessful"), "unsuccessful")
        self.assertEqual(validate_completion_rating("abandoned"), "abandoned")
        self.assertIsNone(validate_completion_rating(None))
        with self.assertRaises(ValueError):
            validate_completion_rating("done")  # non-standard

    def test_validation_manual_rating(self) -> None:
        self.assertEqual(validate_manual_rating(4.5), 4.5)
        self.assertEqual(validate_manual_rating("5"), 5.0)
        self.assertIsNone(validate_manual_rating(None))
        with self.assertRaises(ValueError):
            validate_manual_rating(-1.0)
        with self.assertRaises(ValueError):
            validate_manual_rating(11.0)

    def test_validation_confidence(self) -> None:
        self.assertEqual(validate_confidence(0.85), 0.85)
        self.assertIsNone(validate_confidence(None))
        with self.assertRaises(ValueError):
            validate_confidence(-0.1)
        with self.assertRaises(ValueError):
            validate_confidence(1.2)

    def test_crud_create_get_list_update_delete(self) -> None:
        # Create user-owned outcome
        outcome = create_outcome(
            self.conn,
            entity_type="session",
            entity_id="sess-001",
            outcome_id="out-test-1",
            task_label="debugging",
            completion_rating="complete",
            manual_rating=4.5,
            notes="Fixed null pointer exception",
            was_corrected=True,
            was_restarted=False,
            user_ownership="user",
        )
        self.assertEqual(outcome.outcome_id, "out-test-1")
        self.assertEqual(outcome.entity_type, "session")
        self.assertEqual(outcome.entity_id, "sess-001")
        self.assertEqual(outcome.completion_rating, "complete")
        self.assertEqual(outcome.manual_rating, 4.5)
        self.assertFalse(outcome.is_inferred)
        self.assertTrue(outcome.was_corrected)
        self.assertFalse(outcome.was_restarted)
        self.assertEqual(outcome.notes, "Fixed null pointer exception")
        self.assertEqual(outcome.version, OUTCOMES_POLICY_VERSION)

        # Get
        retrieved = get_outcome(self.conn, "out-test-1")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.task_label, "debugging")

        # Get for entity
        by_entity = get_outcome_for_entity(self.conn, "session", "sess-001")
        self.assertIsNotNone(by_entity)
        self.assertEqual(by_entity.outcome_id, "out-test-1")

        # List
        all_outcomes = list_outcomes(self.conn, task_label="debugging")
        self.assertEqual(len(all_outcomes), 1)

        # Update
        updated = update_outcome(
            self.conn,
            "out-test-1",
            completion_rating="partial",
            manual_rating=3.0,
            notes="Partially addressed, refactoring still required",
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.completion_rating, "partial")
        self.assertEqual(updated.manual_rating, 3.0)
        self.assertEqual(updated.notes, "Partially addressed, refactoring still required")

        # Delete
        deleted = delete_outcome(self.conn, "out-test-1")
        self.assertTrue(deleted)
        self.assertIsNone(get_outcome(self.conn, "out-test-1"))

    def test_inferred_vs_manual_boundary(self) -> None:
        # Create automated inferred outcome
        inferred = create_outcome(
            self.conn,
            entity_type="session",
            entity_id="sess-inferred-1",
            task_label="feature",
            completion_rating="complete",
            is_inferred=True,
            inference_method="ai",
            inference_confidence=0.92,
        )
        self.assertTrue(inferred.is_inferred)
        self.assertEqual(inferred.inference_method, "ai")
        self.assertEqual(inferred.inference_confidence, 0.92)
        self.assertEqual(inferred.user_ownership, "inferred")
        self.assertEqual(inferred.source, "inference")

        # Verification that manual cannot have AI inference method without is_inferred
        with self.assertRaises(ValueError):
            create_outcome(
                self.conn,
                entity_type="session",
                entity_id="sess-invalid",
                is_inferred=False,
                inference_method="ai",
            )


class TestGitLinkageConsentGate(unittest.TestCase):
    """Test git linkage opt-in boundaries and metadata inspection."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema_path = Path(__file__).resolve().parent.parent / "src" / "token_ledger" / "store" / "migrations" / "001_initial.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())
            with open(schema_path.with_name("007_duplicate_of.sql"), "r", encoding="utf-8") as f:
                self.conn.executescript(f.read())

    def tearDown(self) -> None:
        self.conn.close()

    def test_git_linkage_consent_disabled_runs_no_git_command(self) -> None:
        """When consent is disabled, inspect_git_metadata must NEVER invoke subprocess."""
        with patch("subprocess.run") as mock_run:
            res = inspect_git_metadata(
                repo_path="/some/arbitrary/path",
                conn=self.conn,
                consent_granted=None,  # Not in DB
            )
            self.assertFalse(res["consent_enabled"])
            self.assertEqual(res["status"], "consent_disabled")
            mock_run.assert_not_called()

        # Explicitly consent_granted=False
        with patch("subprocess.run") as mock_run:
            res = inspect_git_metadata(
                repo_path="/some/arbitrary/path",
                consent_granted=False,
            )
            self.assertFalse(res["consent_enabled"])
            self.assertEqual(res["status"], "consent_disabled")
            mock_run.assert_not_called()

    def test_git_linkage_with_actual_temp_git_repo(self) -> None:
        """When consent is enabled, inspects local metadata on explicit git repository."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Initialize temp git repository
            subprocess.run(["git", "init"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "TestUser"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

            # Create test files
            file1 = Path(tmp_dir) / "app.py"
            file1.write_text("print('hello world')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_dir, capture_output=True, check=True)

            # 2nd commit with multiple lines
            file1.write_text("print('hello world')\nprint('line 2')\nprint('line 3')\n", encoding="utf-8")
            file2 = Path(tmp_dir) / "test_app.py"
            file2.write_text("assert True\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "Add feature"], cwd=tmp_dir, capture_output=True, check=True)

            # Record opt-in consent in database
            save_privacy_setting(self.conn, OPTIN_GIT_LINKAGE, enabled=True)

            res = inspect_git_metadata(
                repo_path=tmp_dir,
                conn=self.conn,
                commit_ref="HEAD",
            )
            self.assertTrue(res["consent_enabled"])
            self.assertEqual(res["status"], "success")
            meta = res["metadata"]
            self.assertIsNotNone(meta)
            self.assertTrue(len(meta["commit_hash"]) >= 7)
            self.assertEqual(meta["changed_files_count"], 2)
            self.assertGreaterEqual(meta["lines_added"], 2)

            # Verify safe file aliases (no absolute user directory leaked)
            for alias in meta["file_aliases"]:
                self.assertTrue(alias.startswith("file_"))
                self.assertTrue(alias.endswith((".py", "")))

            # Test link_git_outcome
            out = create_outcome(
                self.conn,
                entity_type="session",
                entity_id="sess-git-1",
                outcome_id="out-git-1",
                task_label="feature",
                completion_rating="complete",
            )
            linked = link_git_outcome(
                self.conn,
                outcome_id="out-git-1",
                repo_path=tmp_dir,
                commit_ref="HEAD",
                test_outcome="passed",
            )
            self.assertEqual(linked.git_commit_link, meta["commit_hash"])
            self.assertIsNotNone(linked.git_metadata)
            self.assertEqual(linked.git_metadata["test_outcome"], "passed")


class TestOutcomesMetricsAndAdequacy(unittest.TestCase):
    """Test sample adequacy enforcement, abstention, and per-completion metrics."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema_path = Path(__file__).resolve().parent.parent / "src" / "token_ledger" / "store" / "migrations" / "001_initial.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())
            with open(schema_path.with_name("007_duplicate_of.sql"), "r", encoding="utf-8") as f:
                self.conn.executescript(f.read())

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_fixture_requests(self, num_sessions: int, completed_count: int, priced: bool = True) -> None:
        """Helper to seed database with canonical requests, usage, and outcomes."""
        for i in range(num_sessions):
            sid = f"sess-{i:03d}"
            cid = f"req-{i:03d}"
            self.conn.execute(
                """
                INSERT INTO canonical_requests (
                    canonical_id, provider, account_alias, origin_harness, access_route,
                    raw_model, normalized_model, session_id, request_status, timing_basis,
                    duration_ms, request_timestamp, created_at
                ) VALUES (?, 'anthropic', 'default', 'claude-code', 'api', 'claude-3-5-sonnet', 'Sonnet 5.5', ?, 'ok', 'measured', 1000.0, '2026-10-26T10:00:00Z', '2026-10-26T10:00:00Z');
                """,
                (cid, sid),
            )
            self.conn.execute(
                """
                INSERT INTO normalized_usage (
                    canonical_id, fresh_input_tokens, cache_read_tokens, output_tokens,
                    cache_write_5m_tokens, cache_write_1h_tokens, cache_write_unknown_ttl_tokens
                ) VALUES (?, 100, 50, 50, 0, 0, 0);
                """,
                (cid,),
            )
            self.conn.execute(
                """
                INSERT INTO financial_facts (
                    canonical_id, pricing_status, api_equivalent_usd, api_equivalent_inr, calculated_at
                ) VALUES (?, ?, ?, ?, '2026-10-26T10:00:00Z');
                """,
                (cid, "priced" if priced else "unpriced", 0.05 if priced else None, 4.25 if priced else None),
            )

            # Insert outcome
            status = "complete" if i < completed_count else "partial"
            create_outcome(
                self.conn,
                entity_type="session",
                entity_id=sid,
                outcome_id=f"outc-{i:03d}",
                task_label="debugging" if i % 2 == 0 else "feature",
                completion_rating=status,
                manual_rating=4.0 if i < completed_count else None,
                is_inferred=False,
            )

    def test_sparse_sample_abstention(self) -> None:
        """When completed sample is below declared threshold, per-completion metrics MUST be None."""
        # Insert 2 completed tasks, but threshold is 3
        self._insert_fixture_requests(num_sessions=2, completed_count=2)

        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "outcomes",
            "min_sample_size": 3,  # declared threshold
        }
        res = analyze(self.conn, envelope)

        self.assertEqual(res["values"]["completed_tasks"], 2)
        self.assertFalse(res["values"]["adequate_sample"])
        self.assertIsNotNone(res["values"]["abstention_reason"])
        # Per-completion metrics are strictly withheld
        self.assertIsNone(res["values"]["tokens_per_completed_task"])
        self.assertIsNone(res["values"]["requests_per_completed_task"])
        self.assertIsNone(res["values"]["time_per_completed_task_seconds"])
        self.assertIsNone(res["values"]["cost_per_success_usd"])

    def test_adequate_sample_calculation(self) -> None:
        """When completed sample meets or exceeds threshold, per-completion metrics are computed."""
        # Insert 4 completed tasks, threshold is 3
        self._insert_fixture_requests(num_sessions=4, completed_count=4, priced=True)

        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "outcomes",
            "min_sample_size": 3,
        }
        res = analyze(self.conn, envelope)

        self.assertEqual(res["values"]["completed_tasks"], 4)
        self.assertTrue(res["values"]["adequate_sample"])
        self.assertIsNone(res["values"]["abstention_reason"])

        # 200 tokens per task * 4 / 4 = 200.0
        self.assertEqual(res["values"]["tokens_per_completed_task"], 200.0)
        self.assertEqual(res["values"]["requests_per_completed_task"], 1.0)
        self.assertEqual(res["values"]["time_per_completed_task_seconds"], 1.0)
        self.assertEqual(res["values"]["cost_per_success_usd"], 0.05)
        self.assertEqual(res["values"]["cost_per_success_inr"], 4.25)
        self.assertEqual(res["values"]["user_ratings_mean"], 4.0)

        # Slices exist
        self.assertIn("task_type", res["slices"])
        self.assertIn("model", res["slices"])
        self.assertIn("harness", res["slices"])
        self.assertIn("completion_status", res["slices"])

    def test_unpriced_completed_tasks_withhold_cost_per_success(self) -> None:
        """When completed tasks contain unpriced requests, cost_per_success is withheld."""
        self._insert_fixture_requests(num_sessions=4, completed_count=4, priced=False)

        envelope = {
            "contract_version": "1",
            "harness_scope": "combined",
            "metric_id": "outcomes",
            "min_sample_size": 3,
        }
        res = analyze(self.conn, envelope)

        self.assertTrue(res["values"]["adequate_sample"])
        self.assertEqual(res["values"]["tokens_per_completed_task"], 200.0)
        # Cost per success is None due to lack of priced coverage
        self.assertIsNone(res["values"]["cost_per_success_usd"])
        self.assertGreater(len(res["warnings"]), 0)


class TestOutcomeCounterCompleteness(unittest.TestCase):
    def test_query_preserves_missing_partial_zero_and_complete_totals(self):
        conn = open_database(":memory:")
        self.addCleanup(conn.close)
        cases = (
            ("missing", (None,) * 6, None),
            ("partial", (7, None, None, None, None, 3), None),
            ("measured_zero", (0,) * 6, 0),
            ("known_sum", (7, 2, 1, 0, 0, 3), 13),
        )
        for name, counters, expected in cases:
            conn.execute(
                """INSERT INTO canonical_requests
                   (canonical_id, provider, origin_harness, raw_model, normalized_model,
                    request_timestamp, created_at)
                   VALUES (?, 'synthetic', 'omp', 'synthetic', 'synthetic',
                           '2026-10-20T10:00:00Z', '2026-10-20T10:00:00Z')""",
                (name,),
            )
            conn.execute(
                """INSERT INTO normalized_usage
                   (canonical_id, fresh_input_tokens, cache_read_tokens,
                    cache_write_5m_tokens, cache_write_1h_tokens,
                    cache_write_unknown_ttl_tokens, output_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", (name, *counters),
            )
            create_outcome(conn, entity_type="request", entity_id=name,
                           completion_rating="complete", task_label="debugging")
            with self.subTest(name=name):
                result = execute_query(conn, {
                    "contract_version": "1", "metric_id": "outcomes",
                    "search": name, "min_sample_size": 1,
                })
                self.assertEqual(result["tasks"][0]["total_tokens"], expected)
                self.assertEqual(result["values"]["tokens_per_completed_task"], expected)
                for dimension in ("task_type", "model", "harness", "completion_status"):
                    self.assertEqual(next(iter(result["slices"][dimension].values()))["total_tokens"], expected)

        # A known task must not make the total or per-completion rate exact
        # when another completed task's counters remain incomplete.
        result = execute_query(conn, {
            "contract_version": "1", "metric_id": "outcomes", "min_sample_size": 1,
        })
        self.assertIsNone(result["values"]["tokens_per_completed_task"])
        self.assertIsNone(result["slices"]["completion_status"]["complete"]["total_tokens"])

    def test_application_preserves_explicit_total_zero_and_complete_fallback(self):
        conn = open_database(":memory:")
        self.addCleanup(conn.close)
        # Exercise the application's row-consumer contract directly. Canonical
        # SQL currently derives total_tokens rather than using reported totals.
        for total, counters, expected in (
            (0, (7, None, None, None, None, 3), 0),
            (None, (7, 2, 1, 0, 0, 3), 13),
            (None, (0,) * 6, 0),
        ):
            row = dict(zip((
                "fresh_input_tokens", "cache_read_tokens", "cache_write_5m_tokens",
                "cache_write_1h_tokens", "cache_write_unknown_ttl_tokens", "output_tokens",
            ), counters))
            row.update(canonical_id="synthetic", total_tokens=total)
            with self.subTest(total=total, counters=counters):
                with patch("token_ledger.metrics.outcomes.filtered_request_rows", return_value=[row]):
                    result = analyze(conn, {"contract_version": "1", "metric_id": "outcomes"})
                self.assertEqual(result["tasks"][0]["total_tokens"], expected)


if __name__ == "__main__":
    unittest.main()
