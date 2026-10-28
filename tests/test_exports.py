"""Tests for report scheduling, safe snapshot rendering, and export inventory."""

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from token_ledger.export import (
    ReportDefinition,
    execute_report,
    get_due_reports,
    generate_csv_export,
    generate_html_export,
    generate_json_export,
    generate_pdf_export,
    generate_png_export,
    get_export_by_id,
    list_exports,
    list_report_definitions,
    run_due_reports_scheduler,
    save_report_definition,
    delete_report_definition,
)
from token_ledger.store.database import open_database


class _HtmlAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []

    def _record(self, tag, attrs):
        self.tags.append(tag.lower())
        self.attributes.extend((name.lower(), value or "") for name, value in attrs)

    def handle_starttag(self, tag, attrs):
        self._record(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._record(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)



class TestSnapshotExports(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = self.tempdir.name
        self.data = {
            "title": "<script>alert('title')</script>",
            "generated_at": "2026-10-26T12:00:00Z",
            "query": {
                "contract_version": "1",
                "family": "models",
                "metric_id": "tokens",
                "filters": {"model": ["<img src=x onerror=alert(1)>"]},
                "time_range": {"timezone": "UTC", "start": None, "end": None},
            },
            "values": {"total_tokens": 42, "requests_count": 2},
            "rows": [
                {
                    "model": "=HYPERLINK(\"https://attacker.invalid\",\"click\")",
                    "total_tokens": 42,
                    "prompt": "private prompt body",
                    "source_path": "/home/private/source.jsonl",
                    "annotation": "<img src=x onerror=alert(1)>",
                    "api_key": "sk-" + "a" * 32,
                }
            ],
            "observed": 1,
            "coverage": {"requests": 1, "status": "complete"},
            "pricing_fx": {"fx_rates_available": [83.25], "pricing_version_hashes": ["priced-v1"]},
            "warnings": [],
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def test_all_five_formats_are_real_safe_files(self):
        paths = {suffix: os.path.join(self.root, "report." + suffix) for suffix in ("csv", "json", "html", "pdf", "png")}
        records = [
            generate_csv_export(self.data, paths["csv"], managed_root=self.root),
            generate_json_export(self.data, paths["json"], managed_root=self.root),
            generate_html_export(self.data, paths["html"], managed_root=self.root),
            generate_pdf_export(self.data, paths["pdf"], managed_root=self.root),
            generate_png_export(self.data, paths["png"], managed_root=self.root),
        ]

        self.assertEqual([record.export_format for record in records], ["csv", "json", "html", "pdf", "png"])
        raw = {suffix: Path(path).read_bytes() for suffix, path in paths.items()}
        self.assertTrue(raw["csv"].startswith(b"model,total_tokens"))
        self.assertTrue(raw["json"].lstrip().startswith(b"{"))
        self.assertTrue(raw["html"].lower().startswith(b"<!doctype html>"))
        self.assertTrue(raw["pdf"].startswith(b"%PDF-"))
        self.assertTrue(raw["png"].startswith(b"\x89PNG\r\n\x1a\n"))

        csv_text = raw["csv"].decode("utf-8")
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertNotIn("private prompt body", csv_text)
        self.assertNotIn("/home/private/source.jsonl", csv_text)

        json_text = raw["json"].decode("utf-8")
        self.assertIn("[REDACTED_CONTENT]", json_text)
        self.assertNotIn("private prompt body", json_text)
        self.assertNotIn("sk-" + "a" * 32, json_text)
        self.assertNotIn("/home/private/source.jsonl", json_text)

        html_text = raw["html"].decode("utf-8")
        audit = _HtmlAudit()
        audit.feed(html_text)
        audit.close()
        self.assertIn("svg", audit.tags)
        self.assertIn("table", audit.tags)
        self.assertIn("Query filters", "".join(audit.text))
        self.assertIn("<script>alert('title')</script>", "".join(audit.text))
        self.assertIn("<img src=x onerror=alert(1)>", "".join(audit.text))
        for unsafe_tag in ("script", "img", "iframe", "object", "embed", "base", "link"):
            self.assertNotIn(unsafe_tag, audit.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in audit.attributes))
        self.assertFalse(any(name in {"src", "srcset", "href", "xlink:href"} for name, _ in audit.attributes))
        self.assertNotIn("@import", html_text.lower())
        self.assertNotIn("url(", html_text.lower())
        self.assertNotIn("private prompt body", html_text)
        self.assertNotIn("/home/private/source.jsonl", html_text)

        for record, suffix in zip(records, ("csv", "json", "html", "pdf", "png")):
            self.assertEqual(record.byte_size, len(raw[suffix]))
            self.assertEqual(record.sha256_hash, hashlib.sha256(raw[suffix]).hexdigest())

    def test_direct_exporters_require_safe_redaction_and_formula_protection(self):
        with self.assertRaises(ValueError):
            generate_json_export(self.data, os.path.join(self.root, "unsafe.json"), redact_secrets=False)
        with self.assertRaises(ValueError):
            generate_csv_export(self.data, os.path.join(self.root, "unsafe.csv"), formula_safe=False)

    def test_export_path_is_confined_to_managed_root(self):
        outside = os.path.join(os.path.dirname(self.root), "outside.csv")
        with self.assertRaises(Exception):
            generate_csv_export(self.data, outside, managed_root=self.root)
        self.assertFalse(os.path.exists(outside))


class TestReportDefinitionsAndScheduling(unittest.TestCase):
    def setUp(self):
        self.conn = open_database(":memory:")
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def save(self, name, frequency="daily", state=None, last_generated_at=None, created_at=None):
        definition = ReportDefinition(
            item_id=None,
            name=name,
            frequency=frequency,
            filter_state=state or {"contract_version": "1", "metric_id": "summary", "filters": {}},
            threshold_config={},
            pricing_assumptions={},
            project_id=None,
            account_alias="default",
            last_generated_at=last_generated_at,
            created_at=created_at,
            updated_at=None,
        )
        return save_report_definition(self.conn, definition)

    def test_report_crud_is_scoped_to_report_item_type(self):
        report = self.save("Daily totals")
        self.assertTrue(report.item_id)
        self.assertEqual(report.name, "Daily totals")
        self.assertEqual([item.item_id for item in list_report_definitions(self.conn)], [report.item_id])

        updated = save_report_definition(
            self.conn,
            ReportDefinition(
                item_id=report.item_id,
                name="Renamed report",
                frequency="weekly",
                filter_state=report.filter_state,
                threshold_config={},
                pricing_assumptions={},
                project_id=None,
                account_alias="default",
            ),
        )
        self.assertEqual(updated.name, "Renamed report")
        self.assertEqual(updated.frequency, "weekly")
        self.assertTrue(delete_report_definition(self.conn, report.item_id))
        self.assertIsNone(list_report_definitions(self.conn)[0] if list_report_definitions(self.conn) else None)

    def test_daily_due_uses_local_calendar_time_through_dst(self):
        state = {"contract_version": "1", "metric_id": "summary", "filters": {}, "timezone": "America/New_York"}
        report = self.save("DST daily", state=state, last_generated_at="2026-03-07T17:00:00Z", created_at="2026-03-07T17:00:00Z")
        before = get_due_reports(self.conn, "2026-03-08T15:59:00Z")
        after = get_due_reports(self.conn, "2026-03-08T16:05:00Z")
        self.assertNotIn(report.item_id, {item.item_id for item in before})
        self.assertIn(report.item_id, {item.item_id for item in after})

    def test_weekly_due_uses_local_calendar_time_through_dst(self):
        state = {"contract_version": "1", "metric_id": "summary", "filters": {}, "timezone": "America/New_York"}
        report = self.save("DST weekly", frequency="weekly", state=state, last_generated_at="2026-03-01T14:00:00Z", created_at="2026-03-01T14:00:00Z")
        due = get_due_reports(self.conn, "2026-03-08T13:00:00Z")
        self.assertIn(report.item_id, {item.item_id for item in due})

    def test_monthly_due_clamps_end_of_month_by_calendar(self):
        report = self.save("Month end", frequency="monthly", state={"contract_version": "1", "metric_id": "summary", "filters": {}, "timezone": "UTC"}, last_generated_at="2026-01-31T12:00:00Z", created_at="2026-01-31T12:00:00Z")
        before = get_due_reports(self.conn, "2026-02-28T11:59:00Z")
        due = get_due_reports(self.conn, "2026-02-28T12:00:00Z")
        self.assertNotIn(report.item_id, {item.item_id for item in before})
        self.assertIn(report.item_id, {item.item_id for item in due})

    def test_execution_uses_query_engine_and_persists_complete_inventory(self):
        report = self.save("Empty local report")
        result = execute_report(
            self.conn,
            report.item_id,
            managed_root=self.tempdir.name,
            now_iso="2026-10-26T12:00:00Z",
        )
        self.assertEqual(result.report_id, report.item_id)
        self.assertEqual(result.query["contract_version"], "1")
        self.assertIsInstance(result.coverage, dict)
        self.assertEqual({record.export_format for record in result.exports}, {"csv", "json", "html", "pdf", "png"})
        inventory = list_exports(self.conn, limit=10)
        self.assertEqual(len(inventory), 5)
        for record in inventory:
            self.assertIsNotNone(get_export_by_id(self.conn, record.export_id))
            payload = Path(record.file_path).read_bytes()
            self.assertEqual(record.byte_size, len(payload))
            self.assertEqual(record.sha256_hash, hashlib.sha256(payload).hexdigest())
            self.assertTrue(record.redaction_applied)
            self.assertEqual(record.account_alias, "default")

    def test_due_scheduler_runs_local_jobs_and_advances_timestamp(self):
        report = self.save("Due local report", last_generated_at=None, created_at="2026-10-25T12:00:00Z")
        due = get_due_reports(self.conn, "2026-10-26T12:00:00Z")
        self.assertIn(report.item_id, {item.item_id for item in due})
        results = run_due_reports_scheduler(
            self.conn,
            managed_root=self.tempdir.name,
            now_iso="2026-10-26T12:00:00Z",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].exports), 5)

    def test_cli_run_due_generates_all_offline_exports_with_integrity_metadata(self):
        database_path = os.path.join(self.tempdir.name, "ledger.sqlite3")
        managed_root = os.path.join(self.tempdir.name, "exports")
        conn = open_database(database_path)
        try:
            report = save_report_definition(
                conn,
                ReportDefinition(
                    name='<svg onload="alert(1)"> & <script>report</script>',
                    frequency="daily",
                    filter_state={
                        "contract_version": "1",
                        "metric_id": "summary",
                        "filters": {"model": ["<img src=x onerror=alert(1)>"]},
                    },
                ),
            )
        finally:
            conn.close()

        repository_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        source_path = str(repository_root / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (source_path, environment.get("PYTHONPATH", "")) if part
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "token_ledger",
                "reports",
                "run-due",
                "--db",
                database_path,
                "--managed-root",
                managed_root,
                "--now",
                "2026-10-26T12:00:00Z",
            ],
            cwd=repository_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["executed"]), 1)
        execution = result["executed"][0]
        self.assertEqual(execution["report_id"], report.item_id)
        self.assertEqual(
            {record["export_format"] for record in execution["exports"]},
            {"csv", "json", "html", "png", "pdf"},
        )

        signatures = {
            "csv": lambda payload: payload.startswith(b"metric,value"),
            "json": lambda payload: payload.lstrip().startswith(b"{"),
            "html": lambda payload: payload.lower().startswith(b"<!doctype html>"),
            "png": lambda payload: payload.startswith(b"\x89PNG\r\n\x1a\n"),
            "pdf": lambda payload: payload.startswith(b"%PDF-"),
        }
        conn = open_database(database_path)
        try:
            for record in execution["exports"]:
                payload = Path(record["file_path"]).read_bytes()
                self.assertTrue(signatures[record["export_format"]](payload))
                if record["export_format"] == "csv":
                    self.assertEqual(
                        next(csv.reader(payload.decode("utf-8").splitlines())),
                        ["metric", "value"],
                    )
                self.assertEqual(record["byte_size"], len(payload))
                self.assertEqual(record["sha256_hash"], hashlib.sha256(payload).hexdigest())
                self.assertTrue(record["redaction_applied"])
                self.assertEqual(record["created_at"], "2026-10-26T12:00:00Z")
                self.assertEqual(record["query_filter"]["metric_id"], "summary")
                self.assertTrue(Path(record["file_path"]).is_relative_to(Path(managed_root)))
                stored = get_export_by_id(conn, record["export_id"])
                self.assertIsNotNone(stored)
                self.assertEqual(stored.byte_size, len(payload))
                self.assertEqual(stored.sha256_hash, hashlib.sha256(payload).hexdigest())
                self.assertTrue(stored.redaction_applied)
        finally:
            conn.close()

        html_text = Path(
            next(record["file_path"] for record in execution["exports"] if record["export_format"] == "html")
        ).read_text(encoding="utf-8")
        audit = _HtmlAudit()
        audit.feed(html_text)
        audit.close()
        self.assertIn('<svg onload="alert(1)"> & <script>report</script>', "".join(audit.text))
        self.assertIn("<img src=x onerror=alert(1)>", "".join(audit.text))
        self.assertNotIn("script", audit.tags)
        self.assertNotIn("img", audit.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in audit.attributes))
        self.assertFalse(any(name in {"src", "srcset", "href", "xlink:href"} for name, _ in audit.attributes))
        self.assertNotIn("@import", html_text.lower())
        self.assertNotIn("url(", html_text.lower())


if __name__ == "__main__":
    unittest.main()
