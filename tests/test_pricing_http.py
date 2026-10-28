"""Synthetic loopback HTTP coverage for pricing management and preview."""
import http.client
import json
import os
import tempfile
import threading
import unittest

from token_ledger.api.server import create_server
from token_ledger.store.database import open_database
from token_ledger.query import execute_query


class PricingHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.db")
        self.server = create_server(db_path=self.db, host="127.0.0.1", port=0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, headers, payload = self.request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        self.auth = {"Origin": f"http://127.0.0.1:{self.port}",
                     "Cookie": headers["Set-Cookie"].split(";")[0],
                     "X-CSRF-Token": payload["csrf_token"]}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, data, headers or {})
        response = conn.getresponse()
        body = json.loads(response.read())
        result = response.status, dict(response.getheaders()), body
        conn.close()
        return result

    def post(self, path, payload):
        return self.request("POST", path, payload, self.auth)

    def schedule(self, version, start, end, price):
        return {
            "version_id": version, "effective_start": start, "effective_end": end,
            "currency_code": "USD", "source_provenance": {"source": "synthetic"},
            "rates": [{"provider": "example", "model_identity": "model-base",
                       "access_route": "direct", "input_rate_per_m": price,
                       "output_rate_per_m": price, "status": "estimated",
                       "is_tiered": True, "prompt_threshold": 2000000,
                       "tiered_rates_json": {"input_rate_per_m": price * 2},
                       "source_url": "https://example.invalid/rates"}],
            "model_mappings": [{"provider": "example", "access_route": "direct",
                                "alias": "model-reasoning-high", "model_identity": "model-base",
                                "display_name": "Synthetic model"}],
        }

    def insert_request(self, ident, date, route="direct", missing=False):
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("INSERT INTO canonical_requests (canonical_id, provider, origin_harness, access_route, raw_model, normalized_model, request_timestamp, created_at) VALUES (?, 'example', 'synthetic', ?, 'model-reasoning-high', 'model-reasoning-high', ?, ?)", (ident, route, date, date))
            conn.execute("INSERT INTO normalized_usage (canonical_id, fresh_input_tokens, cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cache_write_unknown_ttl_tokens, output_tokens) VALUES (?, 1000000, ?, 0, 0, 0, 0)", (ident, None if missing else 0))
        conn.close()

    def test_historical_explicit_immutable_and_fx(self):
        versions = "/api/v1/pricing/versions"
        fx = "/api/v1/pricing/fx"
        preview = "/api/v1/pricing/recalculate"
        old = self.schedule("old", "2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z", 2)
        new = self.schedule("new", "2026-07-01T00:00:00Z", None, 5)
        self.assertEqual(self.request("POST", versions, old)[0], 403)
        self.assertEqual(self.post(versions, old)[0], 201)
        self.assertEqual(self.post(versions, new)[0], 201)
        self.assertEqual(self.post(versions, old)[0], 409)
        fx_record = {"base_currency": "USD", "target_currency": "INR", "rate_per_usd": 80,
                     "effective_date": "2026-02-01", "source_provenance": "synthetic"}
        self.assertEqual(self.post(fx, fx_record)[0], 201)
        self.assertEqual(self.post(fx, fx_record)[0], 409)
        self.assertEqual(self.post(fx, {**fx_record, "rate_per_usd": 90, "effective_date": "2026-08-01"})[0], 201)
        status, _, listing = self.request("GET", versions)
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["versions"]), 2)
        self.assertEqual(listing["versions"][0]["model_mappings"][0]["alias"], "model-reasoning-high")
        self.assertEqual(len(self.request("GET", fx)[2]["fx_rates"]), 2)
        self.insert_request("request", "2026-03-01T00:00:00Z")
        self.insert_request("before", "2025-01-01T00:00:00Z")
        self.insert_request("wrong-route", "2026-03-01T00:00:00Z", "other")
        self.insert_request("missing", "2026-03-01T00:00:00Z", missing=True)
        self.insert_request("boundary", "2026-07-01T00:00:00Z")
        self.insert_request("tier", "2026-03-01T00:00:00Z")
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("UPDATE canonical_requests SET pricing_version_id='new' WHERE canonical_id='request'")
            conn.execute("UPDATE normalized_usage SET fresh_input_tokens=3000000 WHERE canonical_id='tier'")
            conn.execute("INSERT INTO financial_facts (canonical_id, actual_billed_amount, actual_billed_currency, subscription_fee_usd, calculated_at) VALUES ('request', 7, 'USD', 4, '2026-03-01T00:00:00Z')")
        before = tuple(conn.execute("SELECT * FROM canonical_requests WHERE canonical_id='request'").fetchone()), tuple(conn.execute("SELECT * FROM normalized_usage WHERE canonical_id='request'").fetchone()), tuple(conn.execute("SELECT * FROM financial_facts WHERE canonical_id='request'").fetchone())
        historical = self.post(preview, {"canonical_id": "request"})
        explicit = self.post(preview, {"canonical_id": "request", "pricing_version_id": "new"})
        self.assertEqual(historical[0], 200)
        self.assertEqual(explicit[0], 200)
        self.assertEqual((historical[2]["mode"], historical[2]["valuation"]["pricing_version_id"], historical[2]["valuation"]["api_equivalent_usd"], historical[2]["valuation"]["api_equivalent_inr"]), ("historical", "old", 2, 160))
        self.assertEqual((explicit[2]["mode"], explicit[2]["valuation"]["pricing_version_id"], explicit[2]["valuation"]["api_equivalent_usd"], explicit[2]["valuation"]["api_equivalent_inr"]), ("explicit", "new", 5, 400))
        self.assertEqual(self.post(preview, {"canonical_id": "before"})[2]["valuation"]["pricing_status"], "unpriced")
        self.assertEqual(self.post(preview, {"canonical_id": "wrong-route"})[2]["valuation"]["pricing_status"], "unpriced")
        self.assertEqual(self.post(preview, {"canonical_id": "missing"})[0], 409)
        self.assertEqual(self.post(preview, {"canonical_id": "request", "pricing_version_id": "absent"})[0], 404)
        boundary = self.post(preview, {"canonical_id": "boundary"})[2]["valuation"]
        self.assertEqual((boundary["pricing_version_id"], boundary["api_equivalent_usd"], boundary["fx_rate_applied"]), ("new", 5, 80))
        tier = self.post(preview, {"canonical_id": "tier"})[2]["valuation"]
        self.assertEqual(tier["api_equivalent_usd"], 12)
        after = tuple(conn.execute("SELECT * FROM canonical_requests WHERE canonical_id='request'").fetchone()), tuple(conn.execute("SELECT * FROM normalized_usage WHERE canonical_id='request'").fetchone()), tuple(conn.execute("SELECT * FROM financial_facts WHERE canonical_id='request'").fetchone())
        self.assertEqual(before, after)
        persisted = self.post(preview, {"canonical_id": "request", "pricing_version_id": "new", "persist": True})
        self.assertEqual(persisted[0], 200)
        self.assertTrue(persisted[2]["persisted"])
        self.assertEqual(tuple(conn.execute("SELECT * FROM canonical_requests WHERE canonical_id='request'").fetchone()), before[0])
        self.assertEqual(tuple(conn.execute("SELECT * FROM normalized_usage WHERE canonical_id='request'").fetchone()), before[1])
        fact = conn.execute("SELECT pricing_version_id, api_equivalent_usd, api_equivalent_inr, actual_billed_amount, actual_billed_currency, subscription_fee_usd FROM financial_facts WHERE canonical_id='request'").fetchone()
        self.assertEqual(tuple(fact), ("new", 5, 400, 7, "USD", 4))
        conn.close()
        print("HTTP smoke: historical old USD 2 INR 160; explicit new USD 5 INR 400; persisted derived fact, original usage and billed facts preserved")

    def test_bulk_filtered_preview_and_persistence_abstains_on_missing_counters(self):
        self.post("/api/v1/pricing/versions", self.schedule("new", "2026-01-01T00:00:00Z", None, 5))
        self.insert_request("selected", "2026-03-01T00:00:00Z")
        self.insert_request("missing", "2026-03-01T00:00:00Z", missing=True)
        self.insert_request("other", "2026-03-01T00:00:00Z", "other")
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("INSERT INTO financial_facts (canonical_id, actual_billed_amount, actual_billed_currency, subscription_fee_usd, calculated_at) VALUES ('selected', 7, 'USD', 4, '2026-03-01T00:00:00Z')")
        before_request = tuple(conn.execute("SELECT * FROM canonical_requests WHERE canonical_id='selected'").fetchone())
        before_usage = tuple(conn.execute("SELECT * FROM normalized_usage WHERE canonical_id='selected'").fetchone())
        envelope = {"contract_version": "1", "harness_scope": "combined",
                    "time_range": {"timezone": "UTC"}, "filters": {"route": ["direct"]}}
        body = {"query": envelope, "pricing_version_id": "new"}
        preview = self.post("/api/v1/pricing/recalculate", body)
        self.assertEqual(preview[0], 200)
        self.assertEqual(preview[2]["selected_count"], 2)
        self.assertEqual({r["canonical_id"] for r in preview[2]["abstentions"]}, {"missing"})
        self.assertEqual(preview[2]["valuations"][0]["api_equivalent_usd"], 5)
        self.assertIsNone(conn.execute("SELECT api_equivalent_usd FROM financial_facts WHERE canonical_id='selected'").fetchone()[0])
        persisted = self.post("/api/v1/pricing/recalculate", {**body, "persist": True})
        self.assertEqual(persisted[0], 200)
        self.assertEqual(tuple(conn.execute("SELECT api_equivalent_usd, actual_billed_amount, actual_billed_currency, subscription_fee_usd FROM financial_facts WHERE canonical_id='selected'").fetchone()), (5, 7, "USD", 4))
        self.assertEqual(tuple(conn.execute("SELECT * FROM canonical_requests WHERE canonical_id='selected'").fetchone()), before_request)
        self.assertEqual(tuple(conn.execute("SELECT * FROM normalized_usage WHERE canonical_id='selected'").fetchone()), before_usage)
        self.assertEqual(self.post("/api/v1/pricing/recalculate", {**body, "query": {"contract_version": "invalid"}})[0], 400)
        self.assertIsNone(conn.execute("SELECT * FROM financial_facts WHERE canonical_id='missing'").fetchone())
        self.assertIsNone(conn.execute("SELECT * FROM financial_facts WHERE canonical_id='other'").fetchone())
        conn.close()

    def test_non_usd_valuation_and_versioned_displays_through_query_and_http(self):
        old = self.schedule("eur", "2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z", 2)
        old["currency_code"] = "eur"
        old["rates"][0].update(cache_read_rate_per_m=1, cache_write_5m_rate_per_m=2,
                               cache_write_1h_rate_per_m=3,
                               tiered_rates_json={"input_rate_per_m": 4, "output_rate_per_m": 6})
        new = self.schedule("usd", "2026-07-01T00:00:00Z", None, 5)
        new["model_mappings"][0]["display_name"] = "Later model"
        self.assertEqual(self.post("/api/v1/pricing/versions", old)[0], 201)
        self.assertEqual(self.post("/api/v1/pricing/versions", new)[0], 201)
        for ident, date, route in (
            ("before-fx", "2026-01-15T00:00:00Z", "direct"),
            ("march", "2026-03-01T00:00:00Z", "direct"),
            ("wrong-route", "2026-03-01T00:00:00Z", "other"),
            ("july", "2026-07-01T00:00:00Z", "direct"),
        ):
            self.insert_request(ident, date, route)
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("UPDATE normalized_usage SET fresh_input_tokens=2000000, cache_read_tokens=1000000, cache_write_5m_tokens=1000000, cache_write_1h_tokens=1000000, output_tokens=1000000 WHERE canonical_id='march'")
            conn.execute("INSERT INTO financial_facts (canonical_id, actual_billed_amount, actual_billed_currency, subscription_fee_usd, calculated_at) VALUES ('march', 7, 'EUR', 4, '2026-03-01T00:00:00Z')")
        fx_path = "/api/v1/pricing/fx"
        for base, target, rate, date, source in (
            ("EUR", "USD", 1.2, "2026-02-01", "synthetic-eur"),
            ("USD", "INR", 80, "2026-02-15", "synthetic-inr"),
            ("EUR", "USD", 1.8, "2026-04-01", "later-eur"),
        ):
            self.assertEqual(self.post(fx_path, {"base_currency": base, "target_currency": target,
                "rate_per_usd": rate, "effective_date": date, "source_provenance": source})[0], 201)
        preview = "/api/v1/pricing/recalculate"
        missing = self.post(preview, {"canonical_id": "before-fx"})[2]["valuation"]
        self.assertIsNone(missing["api_equivalent_usd"])
        self.assertEqual(missing["pricing_status"], "unpriced")
        value = self.post(preview, {"canonical_id": "march"})[2]["valuation"]
        self.assertEqual((value["api_equivalent_usd"], value["api_equivalent_inr"]), (24, 1920))
        self.assertEqual(value["card_to_usd_fx"]["source_provenance"], "synthetic-eur")
        self.assertEqual(value["usd_to_target_fx"]["effective_date"], "2026-02-15")
        for actual, expected in zip((value["fresh_cost_usd"], value["output_cost_usd"],
                                     value["cache_read_cost_usd"], value["cache_write_5m_cost_usd"],
                                     value["cache_write_1h_cost_usd"]), (9.6, 7.2, 1.2, 2.4, 3.6)):
            self.assertAlmostEqual(actual, expected)
        explicit = self.post(preview, {"canonical_id": "march", "pricing_version_id": "usd"})[2]["valuation"]
        self.assertEqual(explicit["pricing_version_id"], "usd")
        self.assertEqual(explicit["card_currency"], "USD")
        saved = self.post(preview, {"canonical_id": "march", "persist": True})
        self.assertEqual(saved[0], 200)
        fact = conn.execute("SELECT actual_billed_amount, actual_billed_currency, subscription_fee_usd, valuation_provenance_json FROM financial_facts WHERE canonical_id='march'").fetchone()
        self.assertEqual(tuple(fact[:3]), (7, "EUR", 4))
        provenance = json.loads(fact[3])
        self.assertEqual(provenance["card_currency"], "EUR")
        self.assertEqual(provenance["card_to_usd_fx"]["source_provenance"], "synthetic-eur")
        self.assertEqual(provenance["usd_to_target_fx"]["source_provenance"], "synthetic-inr")
        envelope = {"contract_version": "1", "harness_scope": "combined", "metric_id": "models"}
        rows = execute_query(conn, envelope)["rows"]
        self.assertEqual({(r["access_route"], r["pricing_version_id"], r["display_name"]) for r in rows},
                         {("direct", "eur", "Synthetic model"), ("other", "eur", None),
                          ("direct", "usd", "Later model")})
        filtered = execute_query(conn, {**envelope, "filters": {"model": ["model-reasoning-high"], "route": ["direct"]}})["rows"]
        self.assertEqual(sum(r["requests_count"] for r in filtered), 3)
        requests = execute_query(conn, {**envelope, "metric_id": "requests"})["rows"]
        self.assertEqual({r["canonical_id"]: r["display_name"] for r in requests}["march"], "Synthetic model")
        request_row = next(r for r in requests if r["canonical_id"] == "march")
        self.assertEqual(request_row["valuation_pricing_version_id"], "eur")
        self.assertEqual(request_row["valuation_provenance"]["card_to_usd_fx"]["effective_date"], "2026-02-01")
        profiles = execute_query(conn, {**envelope, "metric_id": "model_profiles"})["profiles"]
        self.assertEqual(profiles["Synthetic model"]["requests_count"], 2)
        self.assertEqual(profiles["Later model"]["requests_count"], 1)
        scenario_envelope = {"contract_version": "1", "harness_scope": "combined", "metric_id": "efficiency",
            "time_range": {"start": "2026-03-01T00:00:00Z", "end": "2026-03-02T00:00:00Z", "timezone": "UTC"},
            "filters": {"model": ["model-reasoning-high"], "route": ["direct"]},
            "scenario_inputs": {"scenario_type": "output-reduction", "output_reduction_pct": 50}}
        status, _, result = self.post("/api/v1/query", scenario_envelope)
        self.assertEqual(status, 200)
        scenario = result["scenarios"][0]
        self.assertTrue(scenario["is_priced"])
        self.assertEqual(scenario["financials"]["baseline_cost_usd"], 24)
        self.assertEqual(scenario["financials"]["projected_cost_usd"], 20.4)
        self.assertEqual(scenario["pricing_evidence"][0]["card_to_usd_fx"]["source_provenance"], "synthetic-eur")
        conn.close()
    def test_missing_dated_fx_and_assigned_version_in_scenario(self):
        old = self.schedule("eur", "2026-01-01T00:00:00Z", None, 2)
        old["currency_code"] = "EUR"
        self.assertEqual(self.post("/api/v1/pricing/versions", old)[0], 201)
        self.insert_request("before", "2026-02-01T00:30:00+02:00")
        self.insert_request("usd-only", "2026-02-02T00:00:00Z")
        self.assertEqual(self.post("/api/v1/pricing/fx", {
            "base_currency": "EUR", "target_currency": "USD", "rate_per_usd": 1.5,
            "effective_date": "2026-02-01", "source_provenance": "synthetic",
        })[0], 201)
        before = self.post("/api/v1/pricing/recalculate", {"canonical_id": "before"})[2]["valuation"]
        self.assertIsNone(before["api_equivalent_usd"])
        usd_only = self.post("/api/v1/pricing/recalculate", {"canonical_id": "usd-only"})[2]["valuation"]
        self.assertEqual(usd_only["api_equivalent_usd"], 3)
        self.assertIsNone(usd_only["api_equivalent_inr"])
        query = {"contract_version": "1", "harness_scope": "combined", "metric_id": "efficiency",
                 "time_range": {"start": "2026-01-01", "end": "2026-02-02", "timezone": "UTC"},
                 "scenario_inputs": {"scenario_type": "output-reduction", "output_reduction_pct": 50}}
        scenario = self.post("/api/v1/query", query)[2]["scenarios"][0]
        self.assertFalse(scenario["is_priced"])
        self.assertIsNone(scenario["financials"]["baseline_cost_usd"])
        self.assertTrue(any(item["reason"] == "missing_card_currency_to_usd_fx" for item in scenario["pricing_evidence"]))
        future = self.schedule("assigned-usd", "2027-01-01T00:00:00Z", None, 7)
        future["model_mappings"][0]["display_name"] = "Assigned model"
        self.assertEqual(self.post("/api/v1/pricing/versions", future)[0], 201)
        conn = open_database(self.db, run_migrations=False)
        with conn:
            conn.execute("UPDATE canonical_requests SET pricing_version_id='assigned-usd' WHERE canonical_id='usd-only'")
        row = execute_query(conn, {"contract_version": "1", "metric_id": "models",
                                  "time_range": {"start": "2026-02-02", "end": "2026-02-03", "timezone": "UTC"}})["rows"][0]
        self.assertEqual((row["display_name"], row["pricing_version_id"]), ("Assigned model", "assigned-usd"))
        conn.close()
        assigned_query = {**query, "time_range": {"start": "2026-02-02", "end": "2026-02-03", "timezone": "UTC"}}
        assigned = self.post("/api/v1/query", assigned_query)[2]["scenarios"][0]
        self.assertEqual(assigned["financials"]["baseline_cost_usd"], 7)
        self.assertEqual(assigned["pricing_evidence"][0]["pricing_version_id"], "assigned-usd")


    def test_rejects_invalid_input_and_origin(self):
        path = "/api/v1/pricing/versions"
        version = self.schedule("invalid", "2026-01-01T00:00:00Z", None, 2)
        for bad in ({**version, "currency_code": "EURO"},
                    {**version, "effective_start": "2026-01-01"},
                    {**version, "rates": [{**version["rates"][0], "input_rate_per_m": -1}]},
                    {**version, "rates": [{**version["rates"][0], "tiered_rates_json": {"in": 3}}]},
                    {**version, "model_mappings": [{**version["model_mappings"][0], "model_identity": "absent"}]}):
            self.assertEqual(self.post(path, bad)[0], 400)
        headers = {**self.auth, "Origin": "http://attacker.invalid"}
        self.assertEqual(self.request("POST", path, version, headers)[0], 403)
        self.assertEqual(self.request("GET", path, headers={"Host": "attacker.invalid:80"})[0], 403)
        self.assertEqual(self.request("GET", path)[2]["versions"], [])


if __name__ == "__main__":
    unittest.main()
