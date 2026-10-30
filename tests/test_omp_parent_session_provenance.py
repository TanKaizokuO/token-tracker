"""OMP session header provenance stores only the basename of parentSession.

The raw parentSession value can be an absolute log path, which must not reach
source_observations.provenance_json. Session ids pass through unchanged, and
extract_session_hierarchy (which reads the raw record) is unaffected.
"""

import json
import unittest

from token_ledger.ingest.events import extract_session_hierarchy
from token_ledger.ingest.normalize import _basename_parent_ref, normalize_omp_record


def _header(parent):
    return {
        "type": "session",
        "id": "sess_child_001",
        "cwd": "/home/alice/work/project",
        "timestamp": "2026-10-25T11:00:00Z",
        "parentSession": parent,
    }


def _provenance(parent):
    rec = normalize_omp_record(_header(parent), file_locator="omp.jsonl", line_index=0)
    return rec.observation["provenance_json"]


class TestOmpParentSessionProvenance(unittest.TestCase):
    def test_absolute_posix_path_reduced_to_basename(self):
        raw = "/home/alice/.omp/agent/sessions/proj/2026-10-25_parent.jsonl"
        self.assertEqual(_basename_parent_ref(raw), "2026-10-25_parent.jsonl")
        prov = json.loads(_provenance(raw))
        self.assertEqual(prov["parent_session"], "2026-10-25_parent.jsonl")

    def test_trailing_slash_stripped_before_basename(self):
        self.assertEqual(_basename_parent_ref("/home/alice/sessions/parent/"), "parent")

    def test_windows_path_reduced_to_basename(self):
        raw = "C:\\Users\\alice\\.omp\\sessions\\parent.jsonl"
        self.assertEqual(_basename_parent_ref(raw), "parent.jsonl")
        prov = json.loads(_provenance(raw))
        self.assertEqual(prov["parent_session"], "parent.jsonl")

    def test_none_stays_none(self):
        self.assertIsNone(_basename_parent_ref(None))
        prov = json.loads(_provenance(None))
        self.assertIn("parent_session", prov)
        self.assertIsNone(prov["parent_session"])

    def test_session_id_unchanged(self):
        self.assertEqual(_basename_parent_ref("sess_root_100"), "sess_root_100")
        prov = json.loads(_provenance("sess_root_100"))
        self.assertEqual(prov["parent_session"], "sess_root_100")

    def test_no_home_path_in_provenance_json(self):
        raw = "/home/alice/.omp/agent/sessions/proj/parent.jsonl"
        stored = _provenance(raw)
        self.assertNotIn("/home/", stored)
        self.assertNotIn("alice", stored)

    def test_extract_session_hierarchy_unchanged_for_session_ids(self):
        # Downstream hierarchy derives from the raw record, not provenance.
        hier = extract_session_hierarchy(
            {"type": "session", "id": "sess_child", "parentSessionId": "sess_root"},
            adapter="omp",
        )
        self.assertEqual(hier["parent_session_id"], "sess_root")
        self.assertEqual(hier["is_subagent"], 1)


if __name__ == "__main__":
    unittest.main()
