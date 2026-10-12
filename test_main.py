#  Copyright 2026 Philterd, LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import unittest
import contextlib
import http.server
import io
import json
import os
import csv
import shutil
import ssl
import subprocess
import tempfile
import threading
import urllib.parse
from unittest.mock import patch, MagicMock
import main
from main import PhilterDiffuse, PhilterClient, PhilterApiError, PHILTER_API_KEY_ENV
import mongomock

class TestPhilterDiffuse(unittest.TestCase):
    def setUp(self):
        self.mongo_uri = "mongodb://localhost:27017/test_db"
        self.budget_file = "privacy_budget.json"
        
        # Cleanup budget file if it exists
        if os.path.exists(self.budget_file):
            os.remove(self.budget_file)

        # Use mongomock to simulate MongoDB
        with patch('main.MongoClient', mongomock.MongoClient):
            self.engine = PhilterDiffuse(self.mongo_uri)
            
        # Add some data to the mock collection
        self.engine.collection.insert_many([
            {"ssn": "123", "age": 30, "zipcode": "12345"},
            {"ssn": "456", "age": 25},
            {"ssn": "789"},
            {"age": 40}
        ])

    def tearDown(self):
        if os.path.exists(self.budget_file):
            os.remove(self.budget_file)

    def test_budget_tracker_json(self):
        # Test budget tracking with JSON (no mongo)
        # Use a specific collection name to simulate filename
        engine = PhilterDiffuse(collection_name="test_counts.json")
        raw_counts = {"ssn": 10}
        
        # Query 1: scale=1.0, epsilon=1.0
        _, epsilon1, _ = engine.privatize_counts(raw_counts, scale=1.0, budget_ceiling=2.5)
        self.assertEqual(epsilon1, 1.0)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        
        # Verify the key in the JSON file
        with open(self.budget_file, 'r') as f:
            budget_data = json.load(f)
            self.assertIn("test_counts.json", budget_data)
            self.assertEqual(budget_data["test_counts.json"], 1.0)
        
        # Query 2: scale=1.0, epsilon=1.0 (Total=2.0)
        _, epsilon2, _ = engine.privatize_counts(raw_counts, scale=1.0, budget_ceiling=2.5)
        self.assertEqual(epsilon2, 1.0)
        self.assertEqual(engine._get_spent_epsilon(), 2.0)
        
        # Query 3: scale=1.0, epsilon=1.0 (Total would be 3.0 > 2.5)
        private3, epsilon3, exhausted3 = engine.privatize_counts(raw_counts, scale=1.0, budget_ceiling=2.5)
        self.assertEqual(epsilon3, 0.0)
        self.assertTrue(exhausted3)
        self.assertEqual(engine._get_spent_epsilon(), 2.0)
        self.assertTrue(all(v == 0 for v in private3.values()))

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_budget_tracker_mongo(self):
        # Test budget tracking with Mock MongoDB
        engine = PhilterDiffuse(self.mongo_uri)
        raw_counts = {"ssn": 10}
        
        # Query 1: scale=2.0, epsilon=0.5
        _, epsilon1, _ = engine.privatize_counts(raw_counts, scale=2.0, budget_ceiling=1.0)
        self.assertEqual(epsilon1, 0.5)
        self.assertEqual(engine._get_spent_epsilon(), 0.5)
        
        # Query 2: scale=2.0, epsilon=0.5 (Total=1.0)
        _, epsilon2, _ = engine.privatize_counts(raw_counts, scale=2.0, budget_ceiling=1.0)
        self.assertEqual(epsilon2, 0.5)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        
        # Query 3: scale=2.0, epsilon=0.5 (Total would be 1.5 > 1.0)
        private3, epsilon3, exhausted3 = engine.privatize_counts(raw_counts, scale=2.0, budget_ceiling=1.0)
        self.assertEqual(epsilon3, 0.0)
        self.assertTrue(exhausted3)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        self.assertTrue(all(v == 0 for v in private3.values()))

    def test_fetch_pii_counts(self):
        fields = ["ssn", "age", "zipcode"]
        counts = self.engine.fetch_pii_counts(fields)
        
        self.assertEqual(counts["ssn"], 3)
        self.assertEqual(counts["age"], 3)
        self.assertEqual(counts["zipcode"], 1)
        self.assertIn("ssn", counts)
        self.assertIn("age", counts)
        self.assertIn("zipcode", counts)

    def test_fetch_pii_count_aggregates_sums_across_buckets(self):
        # Philter writes one document per (context, day) with a document-presence counts map.
        self.engine.db["pii_count_aggregates"].insert_many([
            {"context": "ctx-a", "bucket_start": "2026-05-30", "counts": {"SSN": 5, "EMAIL_ADDRESS": 100}, "total_documents": 120},
            {"context": "ctx-a", "bucket_start": "2026-05-31", "counts": {"SSN": 3, "PERSON": 9}, "total_documents": 50},
            {"context": "ctx-b", "bucket_start": "2026-05-31", "counts": {"SSN": 2}, "total_documents": 10},
        ])

        totals = self.engine.fetch_pii_count_aggregates()
        self.assertEqual(totals["SSN"], 10)  # 5 + 3 + 2 across all buckets
        self.assertEqual(totals["EMAIL_ADDRESS"], 100)
        self.assertEqual(totals["PERSON"], 9)

    def test_fetch_pii_count_aggregates_filters_by_context(self):
        self.engine.db["pii_count_aggregates"].insert_many([
            {"context": "ctx-a", "bucket_start": "2026-05-31", "counts": {"SSN": 3}},
            {"context": "ctx-b", "bucket_start": "2026-05-31", "counts": {"SSN": 2}},
        ])
        self.assertEqual(self.engine.fetch_pii_count_aggregates(context="ctx-a"), {"SSN": 3})

    def test_fetch_pii_count_aggregates_no_client(self):
        engine_no_client = PhilterDiffuse(collection_name="json_mode")
        with self.assertRaises(ValueError):
            engine_no_client.fetch_pii_count_aggregates()

    def test_load_counts_from_json(self):
        test_data = {"field1": 10, "field2": 20}
        file_path = "test_counts.json"
        with open(file_path, 'w') as f:
            json.dump(test_data, f)
        
        try:
            loaded_data = self.engine.load_counts_from_json(file_path)
            self.assertEqual(loaded_data, test_data)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    def test_fetch_pii_counts_no_client(self):
        engine_no_client = PhilterDiffuse()
        with self.assertRaises(ValueError):
            engine_no_client.fetch_pii_counts(["ssn"])

    def test_privatize_counts(self):
        raw_counts = {"ssn": 10, "age": 20}
        scale = 1.0
        private_results, epsilon, exhausted = self.engine.privatize_counts(raw_counts, scale=scale)
        
        self.assertEqual(epsilon, 1.0 / scale)
        self.assertFalse(exhausted)
        self.assertIn("ssn", private_results)
        self.assertIn("age", private_results)
        self.assertIsInstance(private_results["ssn"], int)
        self.assertIsInstance(private_results["age"], int)
        # Verify no negative numbers are returned
        self.assertTrue(all(v >= 0 for v in private_results.values()))

    def test_privatize_counts_negative_clamping(self):
        # Force the measurement to return a negative number to test clamping
        raw_counts = {"ssn": 1}
        scale = 100.0 # High scale increases chance of negative noise
        
        # We need to patch the measurement function inside privatize_counts
        # Since it's created inside the method, we can patch opendp.measurements.make_laplace
        with patch('main.dp.m.make_laplace') as mock_make:
            mock_measurement = MagicMock()
            mock_measurement.return_value = -5
            mock_make.return_value = mock_measurement
            
            private_results, _, _ = self.engine.privatize_counts(raw_counts, scale=scale)
            self.assertEqual(private_results["ssn"], 0)

    def test_interpret_epsilon(self):
        self.assertEqual(self.engine._interpret_epsilon(0.05), "EXTREME")
        self.assertEqual(self.engine._interpret_epsilon(0.5), "HIGH")
        self.assertEqual(self.engine._interpret_epsilon(2.5), "MODERATE")
        self.assertEqual(self.engine._interpret_epsilon(10.0), "LOW (Testing Only)")

    def test_write_counts_to_csv(self):
        private_counts = {"ssn": 10, "age": 20, "zipcode": 5}
        file_path = "test_output.csv"
        try:
            # Test without threshold
            self.engine.write_counts_to_csv(private_counts, file_path)
            with open(file_path, 'r', newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)
                expected_rows = sorted([['ssn', '10'], ['age', '20'], ['zipcode', '5']])
                self.assertEqual(sorted(rows[1:]), expected_rows)
            
            # Test with threshold = 7
            self.engine.write_counts_to_csv(private_counts, file_path, threshold=7)
            with open(file_path, 'r', newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)
                expected_rows = sorted([['ssn', '10'], ['age', '20'], ['zipcode', 'None']])
                self.assertEqual(sorted(rows[1:]), expected_rows)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    def test_threshold_application(self):
        raw = {"ssn": 5}
        private = {"ssn": 3}
        epsilon = 0.5
        # This will print to console, we just verify it runs with threshold
        self.engine.generate_audit_report(raw, private, epsilon, threshold=5)

    def test_generate_audit_report(self):
        raw = {"ssn": 5}
        private = {"ssn": 6}
        epsilon = 0.5
        
        # We want to see the report, so we don't mock print here anymore
        self.engine.generate_audit_report(raw, private, epsilon)

    def test_privatize_counts_edge_cases(self):
        # Empty counts
        res, eps, exh = self.engine.privatize_counts({}, scale=1.0)
        self.assertEqual(res, {})
        self.assertEqual(eps, 1.0)
        self.assertFalse(exh)

        # Scale zero or negative
        with self.assertRaises(ValueError):
            self.engine.privatize_counts({"ssn": 10}, scale=0)
        with self.assertRaises(ValueError):
            self.engine.privatize_counts({"ssn": 10}, scale=-1.0)

        # Non-integer counts (float)
        # Should be cast to int and run without error
        res, eps, exh = self.engine.privatize_counts({"ssn": 10.9}, scale=1.0)
        self.assertIn("ssn", res)
        self.assertIsInstance(res["ssn"], int)

    def test_mongo_uri_parsing(self):
        # Test default DB name
        with patch('main.MongoClient', mongomock.MongoClient):
            engine = PhilterDiffuse("mongodb://localhost:27017/")
            self.assertEqual(engine.db.name, "philter")
            
            # Test specified DB name
            engine2 = PhilterDiffuse("mongodb://localhost:27017/my_analytics")
            self.assertEqual(engine2.db.name, "my_analytics")

    def test_budget_exact_ceiling(self):
        engine = PhilterDiffuse(collection_name="exact_budget.json")
        raw = {"ssn": 10}
        # Ceiling is 1.0. Spent is 0.
        # Query with scale 1.0 -> epsilon 1.0.
        # Total spent becomes 1.0. This should be allowed.
        _, eps, exh = engine.privatize_counts(raw, scale=1.0, budget_ceiling=1.0)
        self.assertEqual(eps, 1.0)
        self.assertFalse(exh)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        
        # Next query should be blocked
        _, eps2, exh2 = engine.privatize_counts(raw, scale=1.0, budget_ceiling=1.0)
        self.assertEqual(eps2, 0.0)
        self.assertTrue(exh2)

class TestCLI(unittest.TestCase):
    def test_cli_json_loading(self):
        """Test that the CLI correctly loads data when --input is provided."""
        test_data = {"ssn": 10, "age": 20}
        file_path = "cli_test.json"
        output_path = "cli_test.csv"
        with open(file_path, 'w') as f:
            json.dump(test_data, f)

        try:
            with patch('sys.argv', ['main.py', '--input', file_path, '--scale', '1.0', '--output', output_path]):
                # Manually trigger the argument parsing and logic from main.py
                import main
                import argparse
                
                # Create a fresh parser like in main.py to simulate the same behavior
                parser = argparse.ArgumentParser()
                parser.add_argument("--input", type=str)
                parser.add_argument("--mongo-uri", type=str, default="mongodb://localhost:27017/analytics_db")
                parser.add_argument("--scale", type=float, default=2.0)
                parser.add_argument("--output", type=str, required=True)
                parser.add_argument("--threshold", type=int, default=0)
                
                args = parser.parse_args(['--input', file_path, '--scale', '1.0', '--output', output_path, '--threshold', '15'])
                
                import os
                json_filename = os.path.basename(args.input)
                engine = main.PhilterDiffuse(collection_name=json_filename)
                loaded_data = engine.load_counts_from_json(args.input)
                self.assertEqual(loaded_data, test_data)
                
                private_data, epsilon, exhausted = engine.privatize_counts(loaded_data, scale=args.scale)
                self.assertEqual(epsilon, 1.0 / 1.0)
                self.assertFalse(exhausted)
                self.assertIn("ssn", private_data)

                if args.output:
                    engine.write_counts_to_csv(private_data, args.output, threshold=args.threshold)
                    self.assertTrue(os.path.exists(output_path))
                    with open(output_path, 'r') as f:
                        reader = csv.reader(f)
                        rows = list(reader)
                        # test_data has ssn=10, age=20. scale=1.0 noise is usually small.
                        # threshold is 15. age should be >= 15 usually, ssn should be < 15.
                        # But noise is random, so we just check if "None" or a number is present.
                        for row in rows[1:]:
                            if row[0] == 'ssn':
                                # ssn=10 + noise, threshold=15. Likely "None"
                                pass

                # Show the report in the test output
                engine.generate_audit_report(loaded_data, private_data, epsilon, csv_file=args.output, threshold=args.threshold, budget_exhausted=exhausted)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
            if os.path.exists(output_path):
                os.remove(output_path)

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_cli_mongo_defaults(self):
        """Test the CLI logic for MongoDB when no --input is provided."""
        # This tests the logic block's interaction with the PhilterDiffuse class
        output_path = "cli_mongo_test.csv"
        with patch('sys.argv', ['main.py', '--output', output_path]):
            import main
            # Simulate the MongoDB path
            engine = main.PhilterDiffuse("mongodb://localhost:27017/analytics_db")
            self.assertIsNotNone(engine.client)
            
            # Mock fetch_pii_counts to return fixed data
            with patch.object(main.PhilterDiffuse, 'fetch_pii_counts', return_value={"age": 10}):
                raw_data = engine.fetch_pii_counts(["age"])
                private_data, epsilon, exhausted = engine.privatize_counts(raw_data, scale=2.0)
                self.assertEqual(raw_data, {"age": 10})
                self.assertEqual(epsilon, 0.5)
                self.assertFalse(exhausted)
                
                # Show the report in the test output
                engine.generate_audit_report(raw_data, private_data, epsilon, csv_file=output_path, budget_exhausted=exhausted)
        
        if os.path.exists(output_path):
            os.remove(output_path)

    @patch('sys.exit')
    def test_cli_missing_output(self, mock_exit):
        """Test that the application exits when --output is missing."""
        import runpy
        import main
        
        # argparse prints to stderr and exits with 2 for missing required arguments
        with patch('sys.argv', ['main.py', '--input', 'some_file.json']):
            try:
                # We expect argparse to raise SystemExit(2)
                runpy.run_path('main.py', run_name='__main__')
            except SystemExit as e:
                self.assertEqual(e.code, 2)

    @patch('main.PhilterDiffuse.fetch_pii_counts')
    @patch('sys.exit')
    @patch('main.MongoClient')
    def test_cli_mongo_connection_failure(self, mock_mongo, mock_exit, mock_fetch):
        """Test that the application exits when MongoDB connection fails."""
        # Setup mock_mongo to return something so PhilterDiffuse(args.mongo_uri) succeeds
        # but fetch_pii_counts fails.
        mock_fetch.side_effect = Exception("Connection refused")
        mock_exit.side_effect = SystemExit(1)
        
        # We need to simulate the CLI execution environment
        import main
        import sys
        
        with patch('sys.argv', ['main.py', '--output', 'test.csv']):
            # To test the block under `if __name__ == "__main__":`, we can use runpy
            import runpy
            try:
                runpy.run_path('main.py', run_name='__main__')
            except SystemExit:
                pass # Expected
            
            mock_exit.assert_called_once_with(1)

class StubPhilter:
    """
    A stand-in for Philter's GET /api/pii-counts that serves canned entries with Philter's paging,
    filtering, error shape, and X-Request-Id header, and records each request it receives.
    """

    def __init__(self, entries=None, recording_enabled=True, refusal=None, ssl_context=None):
        self.entries = entries or []
        self.recording_enabled = recording_enabled
        self.refusal = refusal  # (status, {"message", "reason"}) to answer every request with
        self.requests = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                stub.requests.append({"path": parsed.path, "params": params, "headers": dict(self.headers)})
                if stub.refusal:
                    status, body = stub.refusal
                    self._send(status, body)
                    return
                matching = [e for e in stub.entries
                            if ("context" not in params or e["context"] == params["context"])
                            and ("from" not in params or e["day"] >= params["from"])
                            and ("to" not in params or e["day"] <= params["to"])]
                offset, limit = int(params.get("offset", 0)), min(int(params.get("limit", 25)), 100)
                self._send(200, {"recordingEnabled": stub.recording_enabled,
                                 "counts": matching[offset:offset + limit], "total": len(matching)})

            def _send(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Request-Id", f"req-{len(stub.requests)}")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        scheme = "http"
        if ssl_context:
            self.server.socket = ssl_context.wrap_socket(self.server.socket, server_side=True)
            scheme = "https"
        self.url = f"{scheme}://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _entry(context, day, counts):
    return {"context": context, "day": day, "counts": counts, "totalDocuments": max(counts.values()),
            "updatedAt": f"{day}T12:00:00.000Z"}


API_KEY = "test-api-key-do-not-print"


class TestPhilterApi(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)  # keep privacy_budget.json and output files out of the checkout

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv, api_key=API_KEY):
        """
        Runs main() with the given arguments and returns (exit_code, stdout, stderr).
        """
        env = {k: v for k, v in os.environ.items() if k != PHILTER_API_KEY_ENV}
        if api_key is not None:
            env[PHILTER_API_KEY_ENV] = api_key
        out, err = io.StringIO(), io.StringIO()
        code = 0
        # Any MongoDB connection on the API path is a failure.
        with patch.dict(os.environ, env, clear=True), \
                patch('main.MongoClient', side_effect=AssertionError("MongoDB must not be used")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_sums_counts_across_every_page(self):
        # 250 (context, day) entries span three pages of 100.
        # Philter returns one entry per (context, day), so the keys are distinct.
        entries = [_entry(f"ctx-{i}", f"2026-09-{(i % 28) + 1:02d}", {"SSN": 1, "PERSON": 2}) for i in range(250)]
        with StubPhilter(entries) as philter:
            client = PhilterClient(philter.url, API_KEY)
            totals = client.fetch_pii_count_aggregates()
        self.assertEqual(totals, {"SSN": 250, "PERSON": 500})
        self.assertEqual([r["params"]["offset"] for r in philter.requests], ["0", "100", "200"])
        self.assertTrue(all(r["params"]["limit"] == "100" for r in philter.requests))
        self.assertTrue(all(r["path"] == "/api/pii-counts" for r in philter.requests))

    def test_entry_repeated_across_pages_is_counted_once(self):
        # A day bucket created between page reads shifts the newest-first listing down by one.
        entries = [_entry(f"ctx-{i:03d}", "2026-09-01", {"SSN": 1}) for i in range(150)]
        with StubPhilter(entries) as philter:
            original = philter.server.RequestHandlerClass.do_GET

            def shifting_get(handler):
                if len(philter.requests) == 1:
                    philter.entries.insert(0, _entry("new-ctx", "2026-09-02", {"SSN": 1}))
                original(handler)

            philter.server.RequestHandlerClass.do_GET = shifting_get
            totals = PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
        # ctx-099 comes back first on the second page and must not be added again.
        self.assertEqual(totals, {"SSN": 150})

    def test_rejects_a_response_that_is_not_the_listing(self):
        class NotPhilter(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotPhilter)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(PhilterApiError) as raised:
                PhilterClient(f"http://127.0.0.1:{server.server_address[1]}", API_KEY).fetch_pii_count_aggregates()
        finally:
            server.shutdown()
            server.server_close()
        # Not mistaken for recording being off.
        self.assertIn("not Philter's PII counts listing", str(raised.exception))

    def test_api_key_with_a_line_break_is_refused_without_printing_it(self):
        code, out, err = self.run_cli("--philter-url", "http://127.0.0.1:9", "--output", "out.csv",
                                      api_key="secret\nkey")
        self.assertEqual(code, 1)
        self.assertIn("control character", err)
        self.assertNotIn("secret", out + err)

    def test_api_key_surrounding_whitespace_is_ignored(self):
        with StubPhilter([]) as philter:
            PhilterClient(philter.url, f"  {API_KEY}\n").fetch_pii_count_aggregates()
        self.assertEqual(philter.requests[0]["headers"]["Authorization"], f"Bearer {API_KEY}")

    def test_budget_key_does_not_depend_on_url_spelling(self):
        self.assertEqual(PhilterClient("HTTPS://LocalHost:8080/", API_KEY).base_url, "https://localhost:8080")

    def test_sends_bearer_api_key(self):
        with StubPhilter([]) as philter:
            PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
        self.assertEqual(philter.requests[0]["headers"]["Authorization"], f"Bearer {API_KEY}")

    def test_passes_context_and_date_range_as_filters(self):
        entries = [
            _entry("claims", "2026-08-31", {"SSN": 100}),
            _entry("claims", "2026-09-01", {"SSN": 1}),
            _entry("claims", "2026-09-30", {"SSN": 2}),
            _entry("billing", "2026-09-15", {"SSN": 50}),
            _entry("claims", "2026-10-01", {"SSN": 100}),
        ]
        with StubPhilter(entries) as philter:
            totals = PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates(
                context="claims", date_from="2026-09-01", date_to="2026-09-30")
        self.assertEqual(totals, {"SSN": 3})
        self.assertEqual(philter.requests[0]["params"],
                         {"context": "claims", "from": "2026-09-01", "to": "2026-09-30", "offset": "0", "limit": "100"})

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_matches_the_mongodb_path_for_the_same_data(self):
        buckets = [
            ("ctx-a", "2026-05-30", {"SSN": 5, "EMAIL_ADDRESS": 100}),
            ("ctx-a", "2026-05-31", {"SSN": 3, "PERSON": 9}),
            ("ctx-b", "2026-05-31", {"SSN": 2}),
        ]
        engine = PhilterDiffuse("mongodb://localhost:27017/test_db")
        engine.db["pii_count_aggregates"].insert_many(
            [{"context": c, "bucket_start": d, "counts": counts} for c, d, counts in buckets])
        with StubPhilter([_entry(c, d, counts) for c, d, counts in buckets]) as philter:
            client = PhilterClient(philter.url, API_KEY)
            self.assertEqual(client.fetch_pii_count_aggregates(), engine.fetch_pii_count_aggregates())
            self.assertEqual(client.fetch_pii_count_aggregates(context="ctx-a"),
                             engine.fetch_pii_count_aggregates(context="ctx-a"))

    def test_cli_privatizes_api_counts_and_tracks_budget_locally(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 40, "PERSON": 90})]) as philter:
            code, out, err = self.run_cli("--philter-url", philter.url, "--scale", "2.0", "--output", "out.csv")
        self.assertEqual(code, 0, err)
        with open("out.csv", newline="") as f:
            self.assertEqual(sorted(row[0] for row in list(csv.reader(f))[1:]), ["PERSON", "SSN"])
        with open("privacy_budget.json") as f:
            self.assertEqual(json.load(f), {f"pii_count_aggregates@{philter.url}": 0.5})
        self.assertNotIn("deprecated", err)

    def test_cli_never_prints_the_api_key(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            _, out, err = self.run_cli("--philter-url", philter.url, "--output", "out.csv")
        with StubPhilter(refusal=(403, {"message": "Forbidden.", "reason": "missing_scope"})) as philter:
            _, out2, err2 = self.run_cli("--philter-url", philter.url, "--output", "out.csv")
        for text in (out, err, out2, err2):
            self.assertNotIn(API_KEY, text)

    def test_cli_requires_the_api_key(self):
        code, _, err = self.run_cli("--philter-url", "https://localhost:8080", "--output", "out.csv", api_key=None)
        self.assertEqual(code, 1)
        self.assertIn(PHILTER_API_KEY_ENV, err)

    def test_cli_recording_off_writes_nothing(self):
        # Counts recorded before recording was turned off are still returned, but the result is
        # not a reliable picture of the PII Philter has found.
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})], recording_enabled=False) as philter:
            code, _, err = self.run_cli("--philter-url", philter.url, "--output", "out.csv")
        self.assertEqual(code, 1)
        self.assertIn("recording is turned off", err)
        self.assertIn("No privatized output was written", err)
        self.assertFalse(os.path.exists("out.csv"))
        self.assertFalse(os.path.exists("privacy_budget.json"))

    def test_refusals_name_the_reason_and_request_id(self):
        cases = [
            (403, {"message": "This API key does not have the 'pii-counts:read' scope.", "reason": "missing_scope"},
             "pii-counts:read scope"),
            (403, {"message": "Reading the PII counts requires an administrator.", "reason": "admin_required"},
             "must be a Philter administrator"),
            (401, {"message": "Unauthorized.", "reason": "invalid_credentials"}, PHILTER_API_KEY_ENV),
            (400, {"message": "The from date must not be after the to date.", "reason": "invalid_request"},
             "The from date must not be after the to date."),
        ]
        for status, body, hint in cases:
            with self.subTest(reason=body["reason"]):
                with StubPhilter(refusal=(status, body)) as philter:
                    with self.assertRaises(PhilterApiError) as raised:
                        PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
                message = str(raised.exception)
                self.assertIn(f"HTTP {status}, {body['reason']}", message)
                self.assertIn(hint, message)
                self.assertIn("Philter request ID: req-1", message)

    def test_cli_refusal_exits_without_output(self):
        with StubPhilter(refusal=(403, {"message": "Forbidden.", "reason": "admin_required"})) as philter:
            code, _, err = self.run_cli("--philter-url", philter.url, "--output", "out.csv")
        self.assertEqual(code, 1)
        self.assertIn("admin_required", err)
        self.assertFalse(os.path.exists("out.csv"))

    def test_unreachable_philter(self):
        with StubPhilter() as philter:
            url = philter.url  # the port is free once the stub has stopped
        with self.assertRaises(PhilterApiError) as raised:
            PhilterClient(url, API_KEY, timeout=5).fetch_pii_count_aggregates()
        self.assertIn(f"Could not reach Philter at {url}", str(raised.exception))

    def test_philter_without_the_endpoint(self):
        for body in ({}, {"message": "Not found.", "reason": "not_found"}):
            with self.subTest(body=body):
                with StubPhilter(refusal=(404, body)) as philter:
                    with self.assertRaises(PhilterApiError) as raised:
                        PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
                self.assertIn("Philter 4.0 or later", str(raised.exception))

    def test_redirects_are_not_followed(self):
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/steal")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(PhilterApiError) as raised:
                PhilterClient(f"http://127.0.0.1:{server.server_address[1]}", API_KEY).fetch_pii_count_aggregates()
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("redirect", str(raised.exception))

    def test_rejects_a_url_without_a_scheme(self):
        with self.assertRaises(PhilterApiError):
            PhilterClient("localhost:8080", API_KEY)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make a test certificate")
    def test_https_verifies_and_trusts_a_self_signed_certificate(self):
        # Like the certificate Philter's Docker image generates: self-signed, not a CA, with
        # 127.0.0.1 and localhost in its subject alternative name.
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", "key.pem", "-out", "cert.pem", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-addext", "basicConstraints=critical,CA:FALSE",
                        "-addext", "keyUsage=digitalSignature,keyEncipherment",
                        "-addext", "extendedKeyUsage=serverAuth"],
                       check=True, capture_output=True)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain("cert.pem", "key.pem")
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 7})], ssl_context=server_context) as philter:
            # Verification is on by default, so the self-signed certificate is refused...
            with self.assertRaises(PhilterApiError) as raised:
                PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
            self.assertIn("--philter-ca-cert", str(raised.exception))
            # ...until it is trusted explicitly.
            totals = PhilterClient(philter.url, API_KEY, ca_cert="cert.pem").fetch_pii_count_aggregates()
        self.assertEqual(totals, {"SSN": 7})

    def test_cli_option_validation(self):
        for argv, expected in [
            (["--from", "2026-09-01", "--output", "out.csv"], "--from and --to require --philter-url"),
            (["--philter-ca-cert", "c.pem", "--output", "out.csv"], "--philter-ca-cert requires --philter-url"),
            (["--philter-url", "https://x", "--input", "c.json", "--output", "out.csv"], "cannot be used together"),
            (["--philter-url", "https://x", "--from", "09/01/2026", "--output", "out.csv"], "YYYY-MM-DD"),
            (["--philter-url", "https://x", "--to", "2026-9-1", "--output", "out.csv"], "YYYY-MM-DD"),
            (["--philter-url", "https://x", "--from", "2026-09-30", "--to", "2026-09-01", "--output", "out.csv"],
             "--from must not be after --to"),
        ]:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(expected, err)

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_mongodb_aggregates_still_work_with_a_deprecation_warning(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.main(["--mongo-uri", "mongodb://localhost:27017/test_db", "--aggregates", "--output", "out.csv"])
        self.assertIn("deprecated", err.getvalue())
        self.assertIn("--philter-url", err.getvalue())
        self.assertTrue(os.path.exists("out.csv"))


if __name__ == "__main__":
    unittest.main()
