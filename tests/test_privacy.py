"""Unit tests for Token Ledger privacy controls, redaction, and retention.

Tests:
1. Synthetic secrets redaction across multiple providers, token types, and formats.
2. Nested metadata allowlisting, key stripping, and redaction before persistence.
3. Export redaction for CSV/JSON/HTML datasets.
4. Source and project inclusion/exclusion policy checks.
5. Explicit versioned opt-in consent gates (content_capture, fingerprints, git, notifications).
6. Safe non-secret account aliasing and source locator sanitization.
7. Content fingerprinting blocked without explicit opt-in.
8. Cascading retention and deletion across canonical store entities.
9. Deletion isolation (only targeted/expired entities are deleted).
10. Strict path traversal defense on snapshot file deletion.
11. Missing schema entities audit reporting without schema alteration.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from token_ledger.privacy import (
    DEFAULT_METADATA_ALLOWLIST,
    MISSING_SCHEMA_ENTITIES,
    OPTIN_CONTENT_CAPTURE,
    OPTIN_CONTENT_FINGERPRINTS,
    OPTIN_GIT_LINKAGE,
    OPTIN_NOTIFICATIONS,
    PRIVACY_POLICY_VERSION,
    REDACTED_CONTENT_PLACEHOLDER,
    REDACTED_SECRET_PLACEHOLDER,
    PathTraversalError,
    PrivacySettings,
    ProjectInclusionPolicy,
    RetentionResult,
    SourceInclusionPolicy,
    cascade_delete_retention,
    delete_snapshot_file,
    hash_content_fingerprint,
    is_opt_in_enabled,
    load_privacy_settings,
    redact_for_export,
    redact_secrets,
    sanitize_account_alias,
    sanitize_file_locator,
    sanitize_metadata,
    save_privacy_setting,
    verify_path_within_root,
)
from token_ledger.store.database import open_database


class TestSecretsRedaction(unittest.TestCase):
    """Test synthetic secret detection and redaction across all supported patterns."""

    def test_openai_api_key_redaction(self):
        text = "Client initialized with key sk-abc12345678901234567890abcdef and org org-test"
        redacted = redact_secrets(text)
        self.assertNotIn("sk-abc12345678901234567890abcdef", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)
        self.assertIn("and org org-test", redacted)

    def test_anthropic_api_key_redaction(self):
        text = "x-api-key: sk-ant-api03-abcdef1234567890abcdef1234567890-test"
        redacted = redact_secrets(text)
        self.assertNotIn("sk-ant-api03-abcdef1234567890abcdef1234567890-test", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)

    def test_google_api_key_redaction(self):
        text = "https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyD1234567890123456789012345678901"
        redacted = redact_secrets(text)
        self.assertNotIn("AIzaSyD1234567890123456789012345678901", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)

    def test_github_tokens_redaction(self):
        text = "Tokens: ghp_123456789012345678901234567890123456 and github_pat_11AAAAAAA0123456789012345678901234567890123456789012345678901234"
        redacted = redact_secrets(text)
        self.assertNotIn("ghp_123456789012345678901234567890123456", redacted)
        self.assertNotIn("github_pat_11AAAAAAA0123456789012345678901234567890123456789012345678901234", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)

    def test_aws_credentials_redaction(self):
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'"
        redacted = redact_secrets(text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redacted)
        self.assertNotIn("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)

    def test_bearer_token_redaction(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMCda8Yhe3iZaWbvV5XKSTbuAn0M"
        redacted = redact_secrets(text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)

    def test_private_key_redaction(self):
        priv_key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Y1+synthetic+key+data+not+real+for+test+purposes\n"
            "only+testing+redaction+of+multiline+private+keys+in+logs+here==\n"
            "-----END RSA PRIVATE KEY-----"
        )
        text = f"Host key:\n{priv_key}\nEnd of host key."
        redacted = redact_secrets(text)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", redacted)
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, redacted)
        self.assertIn("End of host key.", redacted)

    def test_generic_json_secret_assignment(self):
        text = '{"api_key": "secret12345pass", "normal_field": "safe_value"}'
        redacted = redact_secrets(text)
        self.assertNotIn("secret12345pass", redacted)
        self.assertIn("safe_value", redacted)


class TestMetadataSanitizationAndExport(unittest.TestCase):
    """Test recursive metadata allowlisting and export redaction."""

    def test_sensitive_keys_scrubbed_in_nested_dict(self):
        metadata = {
            "session_id": "sess-1",
            "provider": "anthropic",
            "auth": {"api_key": "secret-value-1234", "password": "super-password"},
            "headers": ["Authorization: Bearer my-secret-jwt-token-string"],
            "model": "claude-sonnet-5-5",
        }
        sanitized = sanitize_metadata(metadata)
        self.assertEqual(sanitized["session_id"], "sess-1")
        self.assertEqual(sanitized["auth"]["api_key"], REDACTED_SECRET_PLACEHOLDER)
        self.assertEqual(sanitized["auth"]["password"], REDACTED_SECRET_PLACEHOLDER)
        self.assertNotIn("my-secret-jwt-token-string", sanitized["headers"][0])

    def test_bodies_and_code_scrubbed_without_opt_in(self):
        metadata = {
            "model": "gpt-6.1",
            "prompt": "Here is my secret user query and code",
            "response": "Here is the assistant answer with code: def hello(): pass",
            "tool_output": "file content: secret data",
            "duration_ms": 1200,
        }
        sanitized = sanitize_metadata(metadata, opt_ins=PrivacySettings(content_capture=False))
        self.assertEqual(sanitized["prompt"], REDACTED_CONTENT_PLACEHOLDER)
        self.assertEqual(sanitized["response"], REDACTED_CONTENT_PLACEHOLDER)
        self.assertEqual(sanitized["tool_output"], REDACTED_CONTENT_PLACEHOLDER)
        self.assertEqual(sanitized["duration_ms"], 1200)

    def test_content_preserved_when_opted_in_but_secrets_still_redacted(self):
        metadata = {
            "prompt": "Please test key sk-12345678901234567890abcdef and code snippet",
            "response": "Output: ok",
        }
        sanitized = sanitize_metadata(metadata, opt_ins=PrivacySettings(content_capture=True))
        self.assertIn("Please test key", sanitized["prompt"])
        self.assertNotIn("sk-12345678901234567890abcdef", sanitized["prompt"])
        self.assertIn(REDACTED_SECRET_PLACEHOLDER, sanitized["prompt"])
        self.assertEqual(sanitized["response"], "Output: ok")

    def test_metadata_allowlist_filtering(self):
        metadata = {
            "provider": "google",
            "model": "gemini-3.8-flash",
            "arbitrary_user_field": "leaked_info",
            "secret_field": "top_secret",
            "duration_ms": 450,
        }
        allowed = {"provider", "model", "duration_ms"}
        sanitized = sanitize_metadata(metadata, allowed_keys=allowed)
        self.assertIn("provider", sanitized)
        self.assertIn("model", sanitized)
        self.assertIn("duration_ms", sanitized)
        self.assertNotIn("arbitrary_user_field", sanitized)
        self.assertNotIn("secret_field", sanitized)

    def test_redact_for_export(self):
        dataset = [
            {
                "session_id": "sess-100",
                "provider": "anthropic",
                "tokens": 1500,
                "prompt": "Private prompt",
                "api_key": "sk-12345678901234567890abcdef",
            },
            {
                "session_id": "sess-101",
                "provider": "google",
                "tokens": 800,
                "prompt": "Another prompt",
                "api_key": "AIzaSyD1234567890123456789012345678901",
            },
        ]
        redacted = redact_for_export(dataset, opt_ins={"content_capture": False})
        for row in redacted:
            self.assertEqual(row["prompt"], REDACTED_CONTENT_PLACEHOLDER)
            self.assertEqual(row["api_key"], REDACTED_SECRET_PLACEHOLDER)
            self.assertIn("tokens", row)


class TestInclusionPolicies(unittest.TestCase):
    """Test project and source inclusion/exclusion filtering rules."""

    def test_project_inclusion_defaults(self):
        policy = ProjectInclusionPolicy()
        self.assertTrue(policy.is_included("any-project"))

    def test_project_explicit_exclusion(self):
        policy = ProjectInclusionPolicy(excluded_projects={"secret-repo", "internal-core"})
        self.assertFalse(policy.is_included("secret-repo"))
        self.assertTrue(policy.is_included("public-repo"))

    def test_project_pattern_exclusion(self):
        policy = ProjectInclusionPolicy(exclude_patterns=["*/confidential/*", "secret-*"])
        self.assertFalse(policy.is_included("/home/user/confidential/work"))
        self.assertFalse(policy.is_included("secret-research"))
        self.assertTrue(policy.is_included("/home/user/open-source/project"))

    def test_project_allowlist_requirement(self):
        policy = ProjectInclusionPolicy(allowed_projects={"repo-a", "repo-b"})
        self.assertTrue(policy.is_included("repo-a"))
        self.assertFalse(policy.is_included("repo-c"))

    def test_source_inclusion_policy(self):
        policy = SourceInclusionPolicy(allowed_sources={"claude_code", "omp"})
        self.assertTrue(policy.is_included("claude_code"))
        self.assertTrue(policy.is_included("OMP"))
        self.assertFalse(policy.is_included("unknown_harness"))


class TestVersionedOptInsAndConsentGates(unittest.TestCase):
    """Test explicit opt-in state, version alignment, and consent gates."""

    def setUp(self):
        self.conn = open_database(":memory:", run_migrations=True)

    def tearDown(self):
        self.conn.close()

    def test_default_opt_ins_are_disabled(self):
        settings = load_privacy_settings(self.conn)
        self.assertFalse(settings.content_capture)
        self.assertFalse(settings.content_fingerprints)
        self.assertFalse(settings.git_linkage)
        self.assertFalse(settings.notifications)
        self.assertFalse(is_opt_in_enabled(self.conn, OPTIN_CONTENT_CAPTURE))

    def test_opt_in_enable_and_persistence(self):
        save_privacy_setting(
            self.conn,
            OPTIN_CONTENT_CAPTURE,
            enabled=True,
            description="Capture prompt bodies",
            version=PRIVACY_POLICY_VERSION,
        )
        self.assertTrue(is_opt_in_enabled(self.conn, OPTIN_CONTENT_CAPTURE))

        # Check other opt-ins remain disabled
        self.assertFalse(is_opt_in_enabled(self.conn, OPTIN_CONTENT_FINGERPRINTS))

    def test_policy_version_mismatch_blocks_consent(self):
        # Record consent under an older policy version (e.g. 0.9)
        save_privacy_setting(
            self.conn,
            OPTIN_GIT_LINKAGE,
            enabled=True,
            version="0.9",
        )
        # Explicit version mismatch
        self.assertFalse(is_opt_in_enabled(self.conn, OPTIN_GIT_LINKAGE, expected_version="1.0"))

    def test_unknown_opt_in_key_rejected(self):
        self.assertFalse(is_opt_in_enabled(self.conn, "arbitrary_tracking_key"))


class TestAccountAliasingAndLocators(unittest.TestCase):
    """Test safe account aliases and locator hashing without content hashing."""

    def test_default_account_alias_hashed(self):
        # Default is to hash non-default account names to prevent username leakage
        alias_work = sanitize_account_alias("work")
        self.assertTrue(alias_work.startswith("acct_"))
        self.assertEqual(len(alias_work), 17)  # acct_ + 12 hex chars

        alias_personal = sanitize_account_alias("personal")
        self.assertTrue(alias_personal.startswith("acct_"))

        # 'default' string and empty/None are preserved as 'default'
        self.assertEqual(sanitize_account_alias("default"), "default")
        self.assertEqual(sanitize_account_alias(""), "default")
        self.assertEqual(sanitize_account_alias(None), "default")

    def test_opt_out_account_alias_slug_preserved(self):
        # When hash_alias=False, safe slugs are preserved
        self.assertEqual(sanitize_account_alias("work", hash_alias=False), "work")
        self.assertEqual(sanitize_account_alias("personal", hash_alias=False), "personal")
        self.assertEqual(sanitize_account_alias("team_alpha", hash_alias=False), "team_alpha")

    def test_sensitive_account_alias_always_masked(self):
        # Even with hash_alias=False, emails and paths are masked
        email_alias = sanitize_account_alias("user.name@example.com", hash_alias=False)
        self.assertTrue(email_alias.startswith("acct_"))
        self.assertNotIn("example.com", email_alias)
        self.assertNotIn("user.name", email_alias)

        path_alias = sanitize_account_alias("/Users/alice/credentials", hash_alias=False)
        self.assertTrue(path_alias.startswith("acct_"))
        self.assertNotIn("alice", path_alias)

    def test_file_locator_sanitization(self):
        home = os.path.expanduser("~")
        raw_path = os.path.join(home, "Code", "project", "log.jsonl")

        # Default: locator path is hashed to loc_<hash>
        default_loc = sanitize_file_locator(raw_path)
        self.assertTrue(default_loc.startswith("loc_"))
        self.assertNotIn("Code", default_loc)
        self.assertNotIn(home, default_loc)

        # When hash_locator=False, never leaks ~ or home directory paths
        unhashed_home = sanitize_file_locator(raw_path, hash_locator=False)
        self.assertFalse(unhashed_home.startswith("~"))
        self.assertNotIn(home, unhashed_home)
        # Home paths are safely converted to loc_<hash>
        self.assertTrue(unhashed_home.startswith("loc_"))

        # Relative path with base_dir returns relative path
        base = "/workspace/project"
        rel_file = "/workspace/project/logs/test.jsonl"
        sanitized_rel = sanitize_file_locator(rel_file, base_dir=base, hash_locator=False)
        self.assertEqual(sanitized_rel, "logs/test.jsonl")

    def test_content_fingerprint_gate(self):
        content = "def sensitive_algorithm(): pass"
        # Blocked without opt-in
        self.assertIsNone(hash_content_fingerprint(content, opt_in=False))

        # Allowed with opt-in
        fp = hash_content_fingerprint(content, opt_in=True)
        self.assertIsNotNone(fp)
        self.assertEqual(len(fp), 64)

class TestRetentionCascadingAndPathTraversal(unittest.TestCase):
    """Test cascading retention/deletion and strict path traversal defense."""

    def setUp(self):
        self.conn = open_database(":memory:", run_migrations=True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.managed_root = self.temp_dir.name

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _seed_test_database(self):
        """Populate database with sample sessions, requests, events, and exports."""
        now = "2026-01-15T12:00:00Z"
        old_time = "2025-01-01T00:00:00Z"

        # 1. Projects
        self.conn.execute(
            "INSERT INTO projects (project_id, project_name, created_at) VALUES ('p-1', 'Project One', ?)",
            (now,),
        )

        # 2. Sessions (one old, one new)
        self.conn.execute(
            "INSERT INTO sessions (session_id, origin_harness, project_id, created_at) VALUES ('sess-old', 'claude_code', 'p-1', ?)",
            (old_time,),
        )
        self.conn.execute(
            "INSERT INTO sessions (session_id, origin_harness, project_id, created_at) VALUES ('sess-new', 'omp', 'p-1', ?)",
            (now,),
        )

        # 3. Prompts
        self.conn.execute(
            "INSERT INTO prompts (prompt_id, session_id, created_at) VALUES ('prompt-old', 'sess-old', ?)",
            (old_time,),
        )
        self.conn.execute(
            "INSERT INTO prompts (prompt_id, session_id, created_at) VALUES ('prompt-new', 'sess-new', ?)",
            (now,),
        )

        # 4. Canonical requests
        self.conn.execute(
            """
            INSERT INTO canonical_requests
            (canonical_id, provider, account_alias, origin_harness, raw_model, normalized_model, session_id, request_timestamp, created_at)
            VALUES ('req-old', 'anthropic', 'default', 'claude_code', 'claude-haiku', 'claude-haiku-5-5', 'sess-old', ?, ?)
            """,
            (old_time, old_time),
        )
        self.conn.execute(
            """
            INSERT INTO canonical_requests
            (canonical_id, provider, account_alias, origin_harness, raw_model, normalized_model, session_id, request_timestamp, created_at)
            VALUES ('req-new', 'google', 'default', 'omp', 'gemini-flash', 'gemini-3.8-flash', 'sess-new', ?, ?)
            """,
            (now, now),
        )

        # 5. Usage & Financial facts
        self.conn.execute(
            "INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, output_tokens) VALUES ('req-old', 100, 50)"
        )
        self.conn.execute(
            "INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, output_tokens) VALUES ('req-new', 200, 80)"
        )
        self.conn.execute(
            "INSERT INTO financial_facts (canonical_id, calculated_at) VALUES ('req-old', ?)",
            (old_time,),
        )
        self.conn.execute(
            "INSERT INTO financial_facts (canonical_id, calculated_at) VALUES ('req-new', ?)",
            (now,),
        )

        # 6. Events
        self.conn.execute(
            "INSERT INTO events (event_id, session_id, canonical_id, event_kind, event_name, started_at) VALUES ('evt-old', 'sess-old', 'req-old', 'tool', 'grep', ?)",
            (old_time,),
        )
        self.conn.execute(
            "INSERT INTO events (event_id, session_id, canonical_id, event_kind, event_name, started_at) VALUES ('evt-new', 'sess-new', 'req-new', 'tool', 'read', ?)",
            (now,),
        )

        # 7. Manual outcomes
        self.conn.execute(
            "INSERT INTO manual_settings_and_outcomes (outcome_id, entity_type, entity_id, created_at, updated_at) VALUES ('out-old', 'session', 'sess-old', ?, ?)",
            (old_time, old_time),
        )

        # 8. Archive partitions (rollups): one for sess-old, one for sess-new
        self.conn.execute(
            """
            INSERT INTO archive_partitions
            (partition_id, partition_type, grain, source_name, start_time, end_time, payload_json, created_at)
            VALUES ('part-old', 'hourly', 'hour', 'omp', '2024-12-31T00:00:00Z', '2024-12-31T23:59:59Z', '{"session_ids": ["sess-old"]}', ?)
            """,
            (old_time,),
        )
        self.conn.execute(
            """
            INSERT INTO archive_partitions
            (partition_id, partition_type, grain, source_name, start_time, end_time, payload_json, created_at)
            VALUES ('part-new', 'hourly', 'hour', 'omp', '2026-01-01T00:00:00Z', '2026-01-01T23:59:59Z', '{"session_ids": ["sess-new"]}', ?)
            """,
            (now,),
        )

        # 9. Alerts and reports: one for sess-old, one for sess-new
        self.conn.execute(
            """
            INSERT INTO alerts_and_reports
            (item_id, item_type, name, filter_state_json, created_at, updated_at)
            VALUES ('rep-old', 'report', 'Old Session Report', '{"session_id": "sess-old"}', ?, ?)
            """,
            (old_time, old_time),
        )
        self.conn.execute(
            """
            INSERT INTO alerts_and_reports
            (item_id, item_type, name, filter_state_json, created_at, updated_at)
            VALUES ('rep-new', 'report', 'New Session Report', '{"session_id": "sess-new"}', ?, ?)
            """,
            (now, now),
        )

        # 10. Source observations and observation links
        # - obs-exclusive: linked only to req-old
        # - obs-shared: linked to req-old and req-new
        # - obs-new: linked only to req-new
        self.conn.execute(
            "INSERT INTO source_observations (observation_id, source_adapter, origin_harness, file_locator, observed_at, parser_version, created_at) VALUES ('obs-old-excl', 'claude_code', 'claude_code', 'loc1', ?, '1.0', ?)",
            (old_time, old_time),
        )
        self.conn.execute(
            "INSERT INTO source_observations (observation_id, source_adapter, origin_harness, file_locator, observed_at, parser_version, created_at) VALUES ('obs-shared', 'omp', 'omp', 'loc2', ?, '1.0', ?)",
            (old_time, old_time),
        )
        self.conn.execute(
            "INSERT INTO source_observations (observation_id, source_adapter, origin_harness, file_locator, observed_at, parser_version, created_at) VALUES ('obs-new-excl', 'omp', 'omp', 'loc3', ?, '1.0', ?)",
            (now, now),
        )
        self.conn.execute(
            "INSERT INTO request_observation_links (canonical_id, observation_id, linked_at) VALUES ('req-old', 'obs-old-excl', ?)",
            (old_time,),
        )
        self.conn.execute(
            "INSERT INTO request_observation_links (canonical_id, observation_id, linked_at) VALUES ('req-old', 'obs-shared', ?)",
            (old_time,),
        )
        self.conn.execute(
            "INSERT INTO request_observation_links (canonical_id, observation_id, linked_at) VALUES ('req-new', 'obs-shared', ?)",
            (now,),
        )
        self.conn.execute(
            "INSERT INTO request_observation_links (canonical_id, observation_id, linked_at) VALUES ('req-new', 'obs-new-excl', ?)",
            (now,),
        )

        # 11. Snapshot export files inside managed_root
        self.safe_snapshot_old = os.path.join(self.managed_root, "snapshot_old.json")
        with open(self.safe_snapshot_old, "w") as f:
            f.write('{"sample": "data_old"}')

        self.safe_snapshot_new = os.path.join(self.managed_root, "snapshot_new.json")
        with open(self.safe_snapshot_new, "w") as f:
            f.write('{"sample": "data_new"}')

        # Scoped exports in export_inventory
        self.conn.execute(
            """
            INSERT INTO export_inventory
            (export_id, export_format, file_path, query_filter_json, created_at)
            VALUES ('exp-old-scoped', 'json', ?, '{"session_id": "sess-old"}', ?)
            """,
            (self.safe_snapshot_old, old_time),
        )
        self.conn.execute(
            """
            INSERT INTO export_inventory
            (export_id, export_format, file_path, query_filter_json, created_at)
            VALUES ('exp-new-scoped', 'json', ?, '{"session_id": "sess-new"}', ?)
            """,
            (self.safe_snapshot_new, now),
        )
        self.conn.commit()

    def test_cascade_delete_by_session_id_with_scoped_artifacts_and_orphans(self):
        self._seed_test_database()
        result = cascade_delete_retention(
            self.conn,
            managed_root=self.managed_root,
            session_id="sess-old",
            delete_snapshot_files=True,
        )

        # Core session entities deleted
        self.assertEqual(result.deleted_sessions, 1)
        self.assertEqual(result.deleted_requests, 1)
        self.assertEqual(result.deleted_prompts, 1)
        self.assertEqual(result.deleted_events, 1)
        self.assertEqual(result.deleted_usage, 1)
        self.assertEqual(result.deleted_financial_facts, 1)
        self.assertEqual(result.deleted_outcomes, 1)

        # P1 verification: Orphan observation deleted, shared observation preserved!
        self.assertEqual(result.deleted_observations, 1)
        cur = self.conn.cursor()
        cur.execute("SELECT observation_id FROM source_observations WHERE observation_id = 'obs-old-excl'")
        self.assertIsNone(cur.fetchone(), "Orphan observation must be deleted")
        cur.execute("SELECT observation_id FROM source_observations WHERE observation_id = 'obs-shared'")
        self.assertIsNotNone(cur.fetchone(), "Shared observation must be preserved")
        cur.execute("SELECT observation_id FROM source_observations WHERE observation_id = 'obs-new-excl'")
        self.assertIsNotNone(cur.fetchone(), "Unrelated new observation must be preserved")

        # P1 verification: Scoped export and snapshot file deleted, unrelated export PRESERVED!
        self.assertEqual(result.deleted_exports, 1)
        self.assertEqual(result.deleted_snapshot_files, [self.safe_snapshot_old])
        self.assertFalse(os.path.exists(self.safe_snapshot_old), "Old snapshot file must be deleted")
        self.assertTrue(os.path.exists(self.safe_snapshot_new), "New snapshot file must be PRESERVED")
        cur.execute("SELECT export_id FROM export_inventory WHERE export_id = 'exp-new-scoped'")
        self.assertIsNotNone(cur.fetchone(), "Unrelated export record must be PRESERVED")

        # P1 verification: Scoped partition and report deleted, unrelated PRESERVED!
        self.assertEqual(result.deleted_archive_partitions, 1)
        cur.execute("SELECT partition_id FROM archive_partitions WHERE partition_id = 'part-new'")
        self.assertIsNotNone(cur.fetchone(), "Unrelated archive partition must be PRESERVED")
        self.assertEqual(result.deleted_reports, 1)
        cur.execute("SELECT item_id FROM alerts_and_reports WHERE item_id = 'rep-new'")
        self.assertIsNotNone(cur.fetchone(), "Unrelated report must be PRESERVED")

        # Verify sess-new is untouched (deletion isolation)
        cur.execute("SELECT session_id FROM sessions WHERE session_id = 'sess-new'")
        self.assertIsNotNone(cur.fetchone())
        cur.execute("SELECT canonical_id FROM canonical_requests WHERE canonical_id = 'req-new'")
        self.assertIsNotNone(cur.fetchone())
        cur.close()

    def test_retention_cutoff_by_timestamp(self):
        self._seed_test_database()
        cutoff = "2025-06-01T00:00:00Z"

        result = cascade_delete_retention(
            self.conn,
            managed_root=self.managed_root,
            older_than_iso=cutoff,
            delete_snapshot_files=True,
        )

        self.assertEqual(result.deleted_sessions, 1)
        self.assertEqual(result.deleted_archive_partitions, 1)
        self.assertEqual(result.deleted_exports, 1)
        self.assertEqual(len(result.deleted_snapshot_files), 1)

        # File should be removed from disk
        self.assertFalse(os.path.exists(result.deleted_snapshot_files[0]))

        # sess-new created in 2026 is retained
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sessions")
        self.assertEqual(cur.fetchone()[0], 1)
        cur.close()

    def test_missing_schema_entities_identified(self):
        result = RetentionResult()
        self.assertIn("insights", result.missing_schema_entities)
        self.assertIn("rollups", result.missing_schema_entities)

    def test_path_traversal_prevention(self):
        # File within root is allowed
        safe_path = os.path.join(self.managed_root, "sub", "test.csv")
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w") as f:
            f.write("a,b,c")

        resolved = verify_path_within_root(safe_path, self.managed_root)
        self.assertEqual(resolved, os.path.realpath(safe_path))

        # Traversal with ../ outside root is rejected
        outside_path = os.path.join(self.managed_root, "..", "outside.txt")
        with self.assertRaises(PathTraversalError):
            verify_path_within_root(outside_path, self.managed_root)

        # Root itself is rejected as a file target
        with self.assertRaises(PathTraversalError):
            verify_path_within_root(self.managed_root, self.managed_root)

        # System absolute path is rejected
        with self.assertRaises(PathTraversalError):
            verify_path_within_root("/etc/passwd", self.managed_root)

    def test_symlink_race_traversal_defense(self):
        # P2 verification: Descriptor-relative traversal blocks symlink directory traversals
        outside_dir = tempfile.mkdtemp()
        try:
            victim_file = os.path.join(outside_dir, "victim.txt")
            with open(victim_file, "w") as f:
                f.write("important data")

            symlink_dir = os.path.join(self.managed_root, "sym_dir")
            os.symlink(outside_dir, symlink_dir)

            traversal_target = os.path.join(symlink_dir, "victim.txt")
            # Should be blocked by descriptor-relative traversal
            with self.assertRaises(PathTraversalError):
                delete_snapshot_file(traversal_target, self.managed_root)

            # Outside file must NOT be deleted
            self.assertTrue(os.path.exists(victim_file))
        finally:
            if os.path.exists(victim_file):
                os.remove(victim_file)
            os.rmdir(outside_dir)

    def test_retention_skips_traversal_paths_safely(self):
        # Insert malicious export inventory record pointing outside managed_root
        malicious_path = "/tmp/malicious_outside_path.json"
        self.conn.execute(
            "INSERT INTO export_inventory (export_id, export_format, file_path, created_at) VALUES ('exp-bad', 'json', ?, '2024-01-01T00:00:00Z')",
            (malicious_path,),
        )
        self.conn.commit()

        result = cascade_delete_retention(
            self.conn,
            managed_root=self.managed_root,
            older_than_iso="2025-01-01T00:00:00Z",
            delete_snapshot_files=True,
        )

        self.assertIn(malicious_path, result.skipped_paths_due_to_traversal)


if __name__ == "__main__":
    unittest.main()
