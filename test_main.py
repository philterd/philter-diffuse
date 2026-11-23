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
import hashlib
import http.server
import io
import json
import math
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
from main import PhilterDiffuse, PhilterClient, PhilterApiError, BudgetExceeded, BUDGET_FILE_ENV, PHILTER_API_KEY_ENV, restrict_to_declared_types
import mongomock
from concurrent.futures import ThreadPoolExecutor

# Tests must never touch the real budget under the home directory.
os.environ[BUDGET_FILE_ENV] = "privacy_budget.json"

class TempDirTestCase(unittest.TestCase):
    """Runs each test in its own directory, so nothing it writes lands in the checkout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def write_counts(self, counts, path="counts.json"):
        with open(path, "w") as f:
            f.write(counts if isinstance(counts, str) else json.dumps(counts))

    def run_main(self, *argv):
        """
        Runs main() and returns (exit_code, stdout, stderr).
        """
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def spent(self, source="claims", path="privacy_budget.json"):
        with open(path) as f:
            return json.load(f)[source]


class TestPhilterDiffuse(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.mongo_uri = "mongodb://localhost:27017/test_db"
        self.budget_file = "privacy_budget.json"

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

    def test_budget_tracker_json(self):
        # Test budget tracking with JSON (no mongo)
        # Use a specific collection name to simulate filename
        engine = PhilterDiffuse(collection_name="test_counts.json")
        raw_counts = {"ssn": 10}
        
        # Query 1: scale=1.0, epsilon=1.0
        _, epsilon1 = engine.privatize_counts(raw_counts, scale=1.0, budget_ceiling=2.5)
        self.assertEqual(epsilon1, 1.0)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        
        # Verify the key in the JSON file
        with open(self.budget_file, 'r') as f:
            budget_data = json.load(f)
            self.assertIn("test_counts.json", budget_data)
            self.assertEqual(budget_data["test_counts.json"], 1.0)
        
        # Query 2: scale=1.0, epsilon=1.0 (Total=2.0)
        _, epsilon2 = engine.privatize_counts(raw_counts, scale=1.0, budget_ceiling=2.5)
        self.assertEqual(epsilon2, 1.0)
        self.assertEqual(engine._get_spent_epsilon(), 2.0)
        
        # Query 3: scale=1.0, epsilon=1.0 (Total would be 3.0 > 2.5)
        with self.assertRaises(BudgetExceeded):
            engine.privatize_counts(raw_counts, scale=1.0, budget_ceiling=2.5)
        self.assertEqual(engine._get_spent_epsilon(), 2.0)

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_budget_tracker_mongo(self):
        # Test budget tracking with Mock MongoDB
        engine = PhilterDiffuse(self.mongo_uri)
        raw_counts = {"ssn": 10}
        
        # Query 1: scale=2.0, epsilon=0.5
        _, epsilon1 = engine.privatize_counts(raw_counts, scale=2.0, budget_ceiling=1.0)
        self.assertEqual(epsilon1, 0.5)
        self.assertEqual(engine._get_spent_epsilon(), 0.5)
        
        # Query 2: scale=2.0, epsilon=0.5 (Total=1.0)
        _, epsilon2 = engine.privatize_counts(raw_counts, scale=2.0, budget_ceiling=1.0)
        self.assertEqual(epsilon2, 0.5)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        
        # Query 3: scale=2.0, epsilon=0.5 (Total would be 1.5 > 1.0)
        with self.assertRaises(BudgetExceeded):
            engine.privatize_counts(raw_counts, scale=2.0, budget_ceiling=1.0)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)

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
        private_results, epsilon = self.engine.privatize_counts(raw_counts, scale=scale)
        
        # Two types: one document can change both counts.
        self.assertEqual(epsilon, 2.0 / scale)
        self.assertIn("ssn", private_results)
        self.assertIn("age", private_results)
        self.assertIsInstance(private_results["ssn"], int)
        self.assertIsInstance(private_results["age"], int)
        # Verify no negative numbers are returned
        self.assertTrue(all(v >= 0 for v in private_results.values()))

    def test_epsilon_counts_every_type_one_document_can_change(self):
        # One redaction adds one to each type it found, so a release of n counts has an L1
        # sensitivity of n and spends n / scale, not 1 / scale.
        for n in (1, 2, 5, 10):
            with self.subTest(types=n):
                raw_counts = {f"type-{i}": 100 for i in range(n)}
                _, epsilon = PhilterDiffuse(collection_name=f"types-{n}.json").privatize_counts(
                    raw_counts, scale=2.0, budget_ceiling=100.0)
                self.assertEqual(epsilon, n / 2.0)

    def test_privatize_counts_negative_clamping(self):
        # Force the measurement to return a negative number to test clamping
        raw_counts = {"ssn": 1}
        scale = 100.0 # High scale increases chance of negative noise
        
        # We need to patch the measurement function inside privatize_counts
        # Since it's created inside the method, we can patch opendp.measurements.make_laplace
        with patch('main.dp.m.make_laplace') as mock_make:
            mock_measurement = MagicMock()
            mock_measurement.return_value = [-5]
            mock_measurement.map.return_value = 1.0 / scale
            mock_make.return_value = mock_measurement
            
            private_results, _ = self.engine.privatize_counts(raw_counts, scale=scale)
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
        self.engine.generate_audit_report(private, epsilon, threshold=5)

    def test_generate_audit_report(self):
        raw = {"ssn": 5}
        private = {"ssn": 6}
        epsilon = 0.5
        
        # We want to see the report, so we don't mock print here anymore
        self.engine.generate_audit_report(private, epsilon)

    def test_privatize_counts_edge_cases(self):
        # Empty counts
        res, eps = self.engine.privatize_counts({}, scale=1.0)
        self.assertEqual(res, {})
        # Nothing is released, so nothing is spent.
        self.assertEqual(eps, 0.0)

        # Scale zero or negative
        with self.assertRaises(ValueError):
            self.engine.privatize_counts({"ssn": 10}, scale=0)
        with self.assertRaises(ValueError):
            self.engine.privatize_counts({"ssn": 10}, scale=-1.0)

        # Non-integer counts (float)
        # Should be cast to int and run without error
        res, eps = self.engine.privatize_counts({"ssn": 10.9}, scale=1.0)
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
        _, eps = engine.privatize_counts(raw, scale=1.0, budget_ceiling=1.0)
        self.assertEqual(eps, 1.0)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        
        # Next query should be blocked
        with self.assertRaises(BudgetExceeded):
            engine.privatize_counts(raw, scale=1.0, budget_ceiling=1.0)

class TestCLI(TempDirTestCase):
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
                
                private_data, epsilon = engine.privatize_counts(loaded_data, scale=args.scale)
                self.assertEqual(epsilon, 2.0 / 1.0)
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
                engine.generate_audit_report(private_data, epsilon, csv_file=args.output, threshold=args.threshold)
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
                private_data, epsilon = engine.privatize_counts(raw_data, scale=2.0)
                self.assertEqual(raw_data, {"age": 10})
                self.assertEqual(epsilon, 0.5)
                
                # Show the report in the test output
                engine.generate_audit_report(private_data, epsilon, csv_file=output_path)
        
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
                runpy.run_path(main.__file__, run_name='__main__')
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
        
        with patch('sys.argv', ['main.py', '--mongo-uri', 'mongodb://localhost:27017/test_db', '--pii-types', 'ssn', '--output', 'test.csv']):
            # To test the block under `if __name__ == "__main__":`, we can use runpy
            import runpy
            try:
                runpy.run_path(main.__file__, run_name='__main__')
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


def _entry(context, day, counts, owner="jordan"):
    return {"id": hashlib.sha256(f"{owner}|{context}|{day}".encode()).hexdigest()[:32], "owner": owner,
            "context": context, "day": day, "counts": counts, "totalDocuments": max(counts.values()),
            "updatedAt": f"{day}T12:00:00.000Z"}


API_KEY = "test-api-key-do-not-print"


class TestPhilterApi(TempDirTestCase):

    def run_cli(self, *argv, api_key=API_KEY):
        """
        Runs main() with the given arguments and returns (exit_code, stdout, stderr).
        """
        if "--pii-types" not in argv:
            argv = ("--pii-types", "SSN,PERSON") + argv
        if "--philter-url" in argv and "--from" not in argv and "--to" not in argv:
            argv = argv + ("--from", "2026-09-01", "--to", "2026-09-30")
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

    def test_two_users_contexts_of_one_name_are_both_counted(self):
        # Philter keeps each user's counts apart, and may leave another user's context unnamed.
        # With cross-user access both are named "default"; without it, another user's comes unnamed.
        mine = _entry("default", "2026-09-01", {"SSN": 3}, owner="jordan")
        named = _entry("default", "2026-09-01", {"SSN": 5}, owner="riley")
        unnamed = _entry("default", "2026-09-01", {"SSN": 7}, owner="sam")
        del unnamed["owner"], unnamed["context"]
        with StubPhilter([mine, named, unnamed]) as philter:
            totals = PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
        self.assertEqual(totals, {"SSN": 15})

    def test_an_entry_without_an_id_is_refused(self):
        entry = _entry("default", "2026-09-01", {"SSN": 1})
        del entry["id"]
        with StubPhilter([entry]) as philter:
            with self.assertRaises(PhilterApiError) as raised:
                PhilterClient(philter.url, API_KEY).fetch_pii_count_aggregates()
        self.assertIn("has no id", str(raised.exception))

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
            self.assertEqual(json.load(f), {f"pii_count_aggregates@{philter.url}": {
                "spent_on_every_day": 0.0,
                "releases": [{"from": "2026-09-01", "to": "2026-09-30", "epsilon": 1.0}]}})
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

    def test_plain_http_to_an_https_port_suggests_https(self):
        class TlsPort(http.server.BaseHTTPRequestHandler):
            # Philter's Tomcat answers plain HTTP on its HTTPS port like this.
            def do_GET(self):
                payload = b"Bad Request\nThis combination of host and port requires TLS.\n"
                self.send_response(400)
                self.send_header("Content-Type", "text/plain;charset=UTF-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TlsPort)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(PhilterApiError) as raised:
                PhilterClient(f"http://127.0.0.1:{server.server_address[1]}", API_KEY).fetch_pii_count_aggregates()
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("HTTP 400", str(raised.exception))
        self.assertIn("https://", str(raised.exception))

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
            main.main(["--mongo-uri", "mongodb://localhost:27017/test_db", "--aggregates", "--pii-types", "ssn", "--output", "out.csv"])
        self.assertIn("deprecated", err.getvalue())
        self.assertIn("--philter-url", err.getvalue())
        self.assertTrue(os.path.exists("out.csv"))


class TestDailyBudget(TempDirTestCase):
    """
    With --philter-url, the budget is kept per UTC day. Two types at the default scale of 2.0 cost
    epsilon 1.0 per release.
    """

    run_cli = TestPhilterApi.run_cli

    def release(self, philter, first_day, last_day, ceiling="1.0"):
        return self.run_cli("--philter-url", philter.url, "--from", first_day, "--to", last_day,
                            "--budget-ceiling", ceiling, "--output", "out.csv")

    def ledger(self, philter):
        with open("privacy_budget.json") as f:
            return json.load(f)[f"pii_count_aggregates@{philter.url}"]

    def spent_on(self, philter, day):
        return main._busiest_day(self.ledger(philter), day, day)[1]

    def test_every_day_in_the_window_is_charged(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            code, _, err = self.release(philter, "2026-09-01", "2026-09-30")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.ledger(philter), {
                "spent_on_every_day": 0.0,
                "releases": [{"from": "2026-09-01", "to": "2026-09-30", "epsilon": 1.0}]})
            for day, spent in (("2026-08-31", 0.0), ("2026-09-01", 1.0), ("2026-09-30", 1.0), ("2026-10-01", 0.0)):
                self.assertEqual(self.spent_on(philter, day), spent, day)

    def test_releases_over_separate_windows_each_spend_up_to_the_ceiling(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            for first_day, last_day in (("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-31")):
                code, _, err = self.release(philter, first_day, last_day)
                self.assertEqual(code, 0, err)
            code, _, err = self.release(philter, "2026-09-30", "2026-10-01")
            self.assertEqual(code, 1)
            self.assertIn("2026-09-30 has spent 1", err)

    def test_overlapping_releases_are_charged_twice_on_the_days_they_share(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            for first_day, last_day in (("2026-09-01", "2026-09-30"), ("2026-09-15", "2026-10-15")):
                code, _, err = self.release(philter, first_day, last_day, ceiling="2.0")
                self.assertEqual(code, 0, err)
            for day, spent in (("2026-09-14", 1.0), ("2026-09-15", 2.0), ("2026-09-30", 2.0), ("2026-10-01", 1.0)):
                self.assertEqual(self.spent_on(philter, day), spent, day)
            code, _, err = self.release(philter, "2026-09-20", "2026-09-20", ceiling="2.0")
            self.assertEqual(code, 1)
            code, _, err = self.release(philter, "2026-10-10", "2026-10-31", ceiling="2.0")
            self.assertEqual(code, 0, err)

    def test_refusal_names_the_day_and_writes_and_charges_nothing(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            self.assertEqual(self.release(philter, "2026-09-10", "2026-09-20")[0], 0)
            os.remove("out.csv")
            before = self.ledger(philter)
            code, out, err = self.release(philter, "2026-09-01", "2026-09-30")
            self.assertEqual(code, 1)
            self.assertIn("2026-09-10 has spent 1 of its budget ceiling of 1", err)
            self.assertFalse(os.path.exists("out.csv"))
            self.assertNotIn("PRIVACY AUDIT REPORT", out)
            self.assertEqual(self.ledger(philter), before)

    def test_philter_url_requires_from_and_to(self):
        for dates in ([], ["--from", "2026-09-01"], ["--to", "2026-09-30"]):
            with self.subTest(dates=dates):
                code, _, err = self.run_main("--philter-url", "https://localhost:8080", "--pii-types", "SSN",
                                             "--output", "out.csv", *dates)
                self.assertEqual(code, 2)
                self.assertIn("--philter-url requires --from and --to", err)

    def test_a_0_1_0_entry_counts_as_spent_on_every_day(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            key = f"pii_count_aggregates@{philter.url}"
            self.write_counts({key: 9.5}, "privacy_budget.json")
            code, _, err = self.release(philter, "2026-09-01", "2026-09-30", ceiling="10")
            self.assertEqual(code, 1)
            self.assertIn("2026-09-01 has spent 9.5", err)

            self.write_counts({key: 9.0}, "privacy_budget.json")
            code, _, err = self.release(philter, "2026-09-01", "2026-09-30", ceiling="10")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.ledger(philter), {
                "spent_on_every_day": 9.0,
                "releases": [{"from": "2026-09-01", "to": "2026-09-30", "epsilon": 1.0}]})
            self.assertEqual(self.release(philter, "2026-09-30", "2026-10-31", ceiling="10")[0], 1)

    def test_concurrent_releases_never_pass_the_ceiling_on_any_day(self):
        windows = [("2026-09-01", "2026-09-30"), ("2026-09-15", "2026-10-15")]
        def charge(i):
            try:
                PhilterDiffuse(collection_name="deployment", budget_file="budget.json")._charge(
                    1.0, 5.0, windows[i % 2])
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(charge, range(32)))
        # Both windows hold 2026-09-15 to 2026-09-30, so five releases in all fit under the ceiling.
        self.assertEqual(sum(results), 5)
        with open("budget.json") as f:
            ledger = json.load(f)["deployment"]
        self.assertLessEqual(main._busiest_day(ledger, "2026-09-01", "2026-10-15")[1], 5.0)

    def test_the_charge_to_a_day_is_the_composed_loss_of_the_releases_that_read_it(self):
        # A document is recorded under one day, so it is in the counts of exactly the releases whose
        # windows hold that day. Composing those releases' measurements gives its privacy loss.
        releases = [("2026-09-01", "2026-09-30", 2.0), ("2026-09-15", "2026-10-15", 4.0),
                    ("2026-09-20", "2026-09-20", 1.0), ("2026-10-01", "2026-10-31", 8.0)]
        pii_types = ["SSN", "PERSON", "EMAIL"]
        engine = PhilterDiffuse(collection_name="deployment", budget_file="budget.json")
        for first_day, last_day, scale in releases:
            engine.privatize_counts({t: 10 for t in pii_types}, scale=scale, budget_ceiling=100.0,
                                    window=(first_day, last_day))
        with open("budget.json") as f:
            ledger = json.load(f)["deployment"]

        compose = getattr(main.dp.c, "make_composition", None) or main.dp.c.make_basic_composition
        def laplace(scale):
            return main.dp.m.make_laplace(input_domain=main.dp.vector_domain(main.dp.atom_domain(T="i64")),
                                          input_metric=main.dp.l1_distance(T="i64"), scale=scale)
        for day in ("2026-08-31", "2026-09-01", "2026-09-15", "2026-09-20", "2026-09-30", "2026-10-01",
                    "2026-10-15", "2026-10-16", "2026-11-01"):
            with self.subTest(day=day):
                reading = [scale for first_day, last_day, scale in releases if first_day <= day <= last_day]
                loss = compose([laplace(scale) for scale in reading]).map(len(pii_types)) if reading else 0.0
                self.assertAlmostEqual(main._busiest_day(ledger, day, day)[1], loss)


class TestDeclaredTypes(TempDirTestCase):
    """The released rows and the epsilon charged must not depend on which types the data holds."""

    def run_json(self, counts, *argv):
        with open("counts.json", "w") as f:
            json.dump(counts, f)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main.main(["--input", "counts.json", "--source", "claims", "--output", "out.csv", *argv])
        with open("out.csv", newline="") as f:
            rows = [row[0] for row in list(csv.reader(f))[1:]]
        with open("privacy_budget.json") as f:
            return rows, json.load(f)["claims"], err.getvalue()

    def test_restrict_keeps_declared_types_in_declared_order(self):
        counts, left_out = restrict_to_declared_types({"ssn": 5, "age": 7, "passport-number": 1}, ["email-address", "ssn"])
        self.assertEqual(list(counts.items()), [("email-address", 0), ("ssn", 5)])
        self.assertEqual(left_out, 2)

    def test_rows_and_epsilon_do_not_reveal_a_type_only_one_document_holds(self):
        # These two inputs differ by one document. Neither the rows nor the charge may differ.
        without = self.run_json({"ssn": 500}, "--pii-types", "ssn,passport-number", "--threshold", "25")
        os.remove("privacy_budget.json")
        with_rare = self.run_json({"ssn": 500, "passport-number": 1}, "--pii-types", "ssn,passport-number", "--threshold", "25")
        self.assertEqual(without[0], ["ssn", "passport-number"])
        self.assertEqual(without[:2], with_rare[:2])
        self.assertEqual(without[1], 1.0)

    def test_undeclared_type_is_left_out_and_reported_only_as_a_count(self):
        rows, _, err = self.run_json({"ssn": 500, "passport-number": 1}, "--pii-types", "ssn")
        self.assertEqual(rows, ["ssn"])
        self.assertIn("1 PII type(s)", err)
        self.assertNotIn("passport-number", err)

    def test_no_warning_when_every_type_is_declared(self):
        self.assertEqual(self.run_json({"ssn": 500}, "--pii-types", "ssn")[2], "")

    def test_repeated_type_is_charged_once(self):
        rows, spent, _ = self.run_json({"ssn": 500}, "--pii-types", "ssn, ssn", "--scale", "2.0")
        self.assertEqual(rows, ["ssn"])
        self.assertEqual(spent, 0.5)

    def test_pii_types_is_required_and_cannot_be_empty(self):
        for argv, expected in [([], "--pii-types"), (["--pii-types", "ssn,,age"], "comma-separated"), (["--pii-types", ""], "comma-separated")]:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    main.main(["--input", "counts.json", "--source", "claims", "--output", "out.csv", *argv])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(expected, err.getvalue())

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_field_counting_reads_the_declared_fields(self):
        with patch.object(main.PhilterDiffuse, 'fetch_pii_counts', return_value={"ssn": 3, "age": 0}) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            main.main(["--mongo-uri", "mongodb://localhost:27017/test_db", "--pii-types", "ssn,age", "--output", "out.csv"])
        fetch.assert_called_once_with(["ssn", "age"])


class TestBudgetDurability(TempDirTestCase):

    def test_budget_does_not_depend_on_the_working_directory(self):
        self.write_counts({"ssn": 500})
        counts = os.path.abspath("counts.json")
        os.mkdir("elsewhere")
        with patch.dict(os.environ, {BUDGET_FILE_ENV: os.path.abspath("budget/store.json")}):
            for directory in (".", "elsewhere"):
                os.chdir(directory)
                code, _, err = self.run_main("--input", counts, "--source", "claims", "--pii-types", "ssn",
                                             "--scale", "1", "--output", "out.csv")
                self.assertEqual(code, 0, err)
            self.assertEqual(self.spent(path=os.path.join(self.tmp, "budget/store.json")), 2.0)

    def test_default_location_is_under_the_home_directory(self):
        env = {k: v for k, v in os.environ.items() if k != BUDGET_FILE_ENV}
        with patch.dict(os.environ, dict(env, HOME=self.tmp), clear=True):
            engine = PhilterDiffuse(collection_name="claims")
        self.assertEqual(engine.budget_file, os.path.join(self.tmp, ".philter-diffuse", "privacy_budget.json"))

    def test_budget_file_flag_overrides_the_environment(self):
        self.write_counts({"ssn": 500})
        code, _, err = self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn",
                                     "--budget-file", "chosen.json", "--output", "out.csv")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.spent(path="chosen.json"), 0.5)
        self.assertFalse(os.path.exists("privacy_budget.json"))

    def test_renaming_or_copying_the_input_does_not_start_a_fresh_budget(self):
        for name in ("counts.json", "renamed.json"):
            self.write_counts({"ssn": 500}, name)
            self.run_main("--input", name, "--source", "claims", "--pii-types", "ssn", "--scale", "1", "--output", "out.csv")
        self.assertEqual(self.spent(), 2.0)

    def test_concurrent_charges_never_pass_the_ceiling(self):
        engine = PhilterDiffuse(collection_name="claims", budget_file="budget.json")
        def charge(_):
            try:
                PhilterDiffuse(collection_name="claims", budget_file="budget.json")._charge(1.0, 5.0)
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(charge, range(32)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(engine._get_spent_epsilon(), 5.0)

    def test_a_failed_write_keeps_the_previous_budget(self):
        engine = PhilterDiffuse(collection_name="claims", budget_file="budget.json")
        engine._charge(1.0, 5.0)
        with patch('main.os.replace', side_effect=OSError("disk full")), self.assertRaises(OSError):
            engine._charge(1.0, 5.0)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        self.assertEqual(sorted(os.listdir(".")), ["budget.json", "budget.json.lock"])

    def test_rewriting_the_budget_keeps_its_permissions(self):
        engine = PhilterDiffuse(collection_name="claims", budget_file="budget.json")
        engine._charge(1.0, 5.0)
        os.chmod("budget.json", 0o640)
        engine._charge(1.0, 5.0)
        self.assertEqual(os.stat("budget.json").st_mode & 0o777, 0o640)

    def test_a_corrupt_budget_file_is_refused_rather_than_reset(self):
        self.write_counts({"ssn": 500})
        self.write_counts("{not json", "privacy_budget.json")
        code, out, _ = self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn", "--output", "out.csv")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", out)
        self.assertFalse(os.path.exists("out.csv"))

    def test_noise_is_not_sampled_until_the_charge_is_recorded(self):
        engine = PhilterDiffuse(collection_name="claims", budget_file="budget.json")
        measurement = MagicMock()
        measurement.map.return_value = 1.0
        with patch('main.dp.m.make_laplace', return_value=measurement), \
                patch('main.os.replace', side_effect=OSError("disk full")), self.assertRaises(OSError):
            engine.privatize_counts({"ssn": 500})
        measurement.assert_not_called()

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_mongo_charge_is_refused_at_the_ceiling_without_a_second_record(self):
        engine = PhilterDiffuse("mongodb://localhost:27017/charge_db")
        engine._charge(1.0, 1.5)
        with self.assertRaises(BudgetExceeded):
            engine._charge(1.0, 1.5)
        self.assertEqual(engine._get_spent_epsilon(), 1.0)
        self.assertEqual(engine.metadata_collection.count_documents({}), 1)


class TestRefusedRelease(TempDirTestCase):

    def refused(self, *argv):
        self.write_counts({"ssn": 500, "age": 700})
        return self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn,age",
                             "--scale", "1", "--budget-ceiling", "3", "--output", "out.csv", *argv)

    def test_refusal_exits_non_zero_and_keeps_the_earlier_output(self):
        self.assertEqual(self.refused()[0], 0)
        with open("out.csv") as f:
            released = f.read()
        code, out, err = self.refused()
        self.assertEqual(code, 1)
        self.assertIn("costs epsilon 2", err)
        self.assertIn("spent 2 of its budget ceiling of 3", err)
        with open("out.csv") as f:
            self.assertEqual(f.read(), released)
        self.assertEqual(self.spent(), 2.0)

    def test_refusal_writes_no_output_and_no_report(self):
        code, out, _ = self.run_main_with_too_many_types()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("out.csv"))
        self.assertNotIn("PRIVACY AUDIT REPORT", out)

    def run_main_with_too_many_types(self):
        types = [f"type-{i}" for i in range(25)]
        self.write_counts({t: 100 for t in types})
        return self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", ",".join(types), "--output", "out.csv")


class TestAuditReport(TempDirTestCase):

    def test_report_holds_private_counts_but_never_raw_ones(self):
        self.write_counts({"ssn": 987654321})
        measurement = MagicMock(return_value=[123456789])
        measurement.map.return_value = 0.5
        with patch('main.dp.m.make_laplace', return_value=measurement):
            code, out, err = self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn", "--output", "out.csv")
        self.assertEqual(code, 0, err)
        self.assertIn("123456789", out)
        self.assertNotIn("987654321", out + err)
        self.assertNotIn("Raw", out)


class TestInputValidation(TempDirTestCase):

    def run_json(self, counts, *argv):
        self.write_counts(counts)
        return self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn", "--output", "out.csv", *argv)

    def test_bad_counts_are_refused_without_output_or_charge(self):
        for counts, expected in [
            ({"ssn": 3.9}, "whole number"), ({"ssn": 10.0}, "whole number"), ({"ssn": -5}, "whole number"),
            ({"ssn": "12"}, "whole number"), ({"ssn": True}, "whole number"), ({"ssn": None}, "whole number"),
            ({"ssn": 2**63}, "whole number"), ({"ssn": 1, "age": -1}, "age"), ([1, 2], "JSON object"), ("{oops", "not valid JSON"),
        ]:
            with self.subTest(counts=counts):
                code, out, _ = self.run_json(counts)
                self.assertEqual(code, 1)
                self.assertIn(expected, out)
                self.assertFalse(os.path.exists("out.csv"))
                self.assertFalse(os.path.exists("privacy_budget.json"))

    def test_counts_past_32_bits_are_privatized(self):
        code, _, err = self.run_json({"ssn": 3000000000}, "--scale", "1")
        self.assertEqual(code, 0, err)
        with open("out.csv", newline="") as f:
            count = int(list(csv.reader(f))[1][1])
        self.assertLess(abs(count - 3000000000), 1000)

    def test_flags_that_would_be_ignored_are_refused(self):
        uri = "mongodb://localhost:27017/test_db"
        source = ["--input", "c.json", "--source", "claims"]
        for argv, expected in [
            ([], "One of --input, --philter-url, or --mongo-uri is required"),
            ([*source, "--mongo-uri", uri], "cannot be used together"),
            (["--philter-url", "https://x", "--mongo-uri", uri], "cannot be used together"),
            ([*source, "--aggregates"], "--aggregates requires"),
            ([*source, "--context", "c"], "--context requires"),
            (["--mongo-uri", uri, "--context", "c"], "--context requires"),
            (["--input", "c.json"], "--input requires --source"),
            (["--philter-url", "https://x", "--source", "claims"], "--source requires --input"),
            (["--mongo-uri", uri, "--budget-file", "b.json"], "--budget-file cannot be used"),
        ]:
            with self.subTest(argv=argv):
                code, _, err = self.run_main("--pii-types", "ssn", "--output", "out.csv", *argv)
                self.assertEqual(code, 2)
                self.assertIn(expected, err)

    def test_numeric_options_are_checked(self):
        for flag, value in [("--scale", "nan"), ("--scale", "inf"), ("--scale", "0"), ("--scale", "-1"), ("--scale", "x"),
                            ("--budget-ceiling", "inf"), ("--budget-ceiling", "-1"), ("--threshold", "-1"), ("--threshold", "1.5")]:
            with self.subTest(flag=flag, value=value):
                code, _, err = self.run_json({"ssn": 1}, f"{flag}={value}")
                self.assertEqual(code, 2)
                self.assertIn(flag, err)


class TestBudgetStatus(TempDirTestCase):
    """--budget-status prints what each source has spent and has left, and changes nothing."""

    def test_prints_spent_and_remaining_for_each_source_and_day(self):
        self.write_counts({
            "claims": 2.0,
            "notes": 4.5,
            "pii_count_aggregates@https://philter:8080": {"spent_on_every_day": 0.0, "releases": [
                {"from": "2026-09-01", "to": "2026-09-30", "epsilon": 1.0},
                {"from": "2026-09-15", "to": "2026-10-15", "epsilon": 1.0}]},
            "pii_count_aggregates@https://upgraded:8080": {"spent_on_every_day": 3.0, "releases": [
                {"from": "2026-08-01", "to": "2026-08-15", "epsilon": 0.5},
                {"from": "2026-08-16", "to": "2026-08-31", "epsilon": 0.5},
                {"from": "2026-09-20", "to": "2026-09-20", "epsilon": 1.5}]},
        }, "privacy_budget.json")
        code, out, err = self.run_main("--budget-status", "--budget-ceiling", "5")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "Privacy budget in privacy_budget.json, against a ceiling of 5:",
            "claims: spent 2, remaining 3",
            "notes: spent 4.5, remaining 0.5",
            "pii_count_aggregates@https://philter:8080, per UTC day:",
            "  2026-09-01 to 2026-09-14: spent 1, remaining 4",
            "  2026-09-15 to 2026-09-30: spent 2, remaining 3",
            "  2026-10-01 to 2026-10-15: spent 1, remaining 4",
            "  every other day: spent 0, remaining 5",
            "pii_count_aggregates@https://upgraded:8080, per UTC day:",
            "  2026-08-01 to 2026-08-31: spent 3.5, remaining 1.5",
            "  2026-09-20: spent 4.5, remaining 0.5",
            "  every other day: spent 3, remaining 2",
        ])

    def test_remaining_is_never_below_zero(self):
        self.write_counts({"claims": 12.0, "pii_count_aggregates@https://philter:8080": 11.0}, "privacy_budget.json")
        code, out, err = self.run_main("--budget-status")
        self.assertEqual(code, 0, err)
        self.assertIn("claims: spent 12, remaining 0", out)
        # A 0.1.0 entry is one total, as for any other source.
        self.assertIn("pii_count_aggregates@https://philter:8080: spent 11, remaining 0", out)

    def test_matches_what_the_releases_spent(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            for first_day, last_day in (("2026-09-01", "2026-09-30"), ("2026-09-15", "2026-10-15")):
                code, _, err = TestPhilterApi.run_cli(self, "--philter-url", philter.url, "--from", first_day,
                                                      "--to", last_day, "--output", "out.csv")
                self.assertEqual(code, 0, err)
            requests = len(philter.requests)
            code, out, err = self.run_main("--budget-status")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(philter.requests), requests)
        self.assertIn("  2026-09-15 to 2026-09-30: spent 2, remaining 8", out)

    def test_needs_no_types_or_output_and_reads_and_changes_nothing(self):
        self.write_counts({"claims": 2.0}, "privacy_budget.json")
        before = os.stat("privacy_budget.json")
        with patch('main.PhilterClient', side_effect=AssertionError("Philter must not be called")), \
                patch('main.PhilterDiffuse._charge', side_effect=AssertionError("nothing may be charged")), \
                patch('main.PhilterDiffuse.load_counts_from_json', side_effect=AssertionError("no counts may be read")):
            code, out, err = self.run_main("--budget-status")
        self.assertEqual(code, 0, err)
        self.assertIn("claims: spent 2, remaining 8", out)
        self.assertEqual(sorted(os.listdir(".")), ["privacy_budget.json"])
        after = os.stat("privacy_budget.json")
        self.assertEqual((after.st_mtime_ns, after.st_size), (before.st_mtime_ns, before.st_size))

    def test_an_empty_budget_is_reported_without_creating_a_file(self):
        os.mkdir("store")
        code, out, err = self.run_main("--budget-status", "--budget-file", "store/budget.json")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "No privacy budget has been spent in store/budget.json.")
        self.assertEqual(os.listdir("store"), [])

    def test_flags_for_a_release_are_refused(self):
        for argv in (["--pii-types", "ssn"], ["--output", "out.csv"], ["--input", "c.json"], ["--source", "claims"],
                     ["--philter-url", "https://x"], ["--scale", "2"], ["--threshold", "0"], ["--aggregates"],
                     ["--context", "c"], ["--from", "2026-09-01"], ["--budget-warning-share", "0.5"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_main("--budget-status", *argv)
                self.assertEqual(code, 2)
                self.assertIn(f"--budget-status only reads the budget and cannot be used with {argv[0]}", err)
        code, _, err = self.run_main("--budget-status", "--mongo-uri", "mongodb://localhost/db", "--budget-file", "b.json")
        self.assertEqual(code, 2)
        self.assertIn("--budget-file cannot be used", err)

    def test_a_release_still_needs_types_and_output(self):
        self.write_counts({"ssn": 5})
        code, _, err = self.run_main("--input", "counts.json", "--source", "claims")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: --pii-types, --output", err)

    def test_reads_mongodb_without_printing_the_uri_or_writing(self):
        client = mongomock.MongoClient()
        client["status_db"]["privacy_metadata"].insert_many([
            {"collection": "pii_counts", "spent_epsilon": 3.0}, {"collection": "pii_count_aggregates", "spent_epsilon": 9.5}])
        with patch('main.MongoClient', return_value=client):
            code, out, err = self.run_main("--budget-status", "--mongo-uri", "mongodb://reader:s3cret@localhost:27017/status_db")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "Privacy budget in the privacy_metadata collection of MongoDB database status_db, against a ceiling of 10:",
            "pii_count_aggregates: spent 9.5, remaining 0.5",
            "pii_counts: spent 3, remaining 7",
        ])
        self.assertNotIn("s3cret", out + err)
        self.assertEqual(client["status_db"]["privacy_metadata"].count_documents({}), 2)
        self.assertNotIn(main.RELEASE_LOG_COLLECTION, client["status_db"].list_collection_names())


class TestBudgetInReport(TempDirTestCase):
    """The audit report gives what is spent and left after the release, and warns past a share of it."""

    def release(self, *argv, types="ssn"):
        self.write_counts({"ssn": 500, "age": 700})
        return self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", types,
                             "--scale", "1", "--output", "out.csv", *argv)

    def test_report_shows_spent_and_remaining_after_the_release(self):
        for spent, remaining in (("1.0000", "2.0000"), ("2.0000", "1.0000")):
            code, out, err = self.release("--budget-ceiling", "3")
            self.assertEqual(code, 0, err)
            self.assertIn(f"Budget Spent:         {spent} of 3.0000\n", out)
            self.assertIn(f"Budget Remaining:     {remaining}\n", out)

    def test_report_names_the_busiest_day_of_the_window(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            for first_day, last_day, spent, remaining, day in (
                    ("2026-09-15", "2026-09-20", "1.0000", "2.0000", "2026-09-15"),
                    ("2026-09-01", "2026-09-30", "2.0000", "1.0000", "2026-09-15")):
                code, out, err = TestPhilterApi.run_cli(self, "--philter-url", philter.url, "--from", first_day,
                                                        "--to", last_day, "--budget-ceiling", "3", "--output", "out.csv")
                self.assertEqual(code, 0, err)
                self.assertIn(f"Budget Spent:         {spent} of 3.0000 on {day} (the busiest day from --from to --to)", out)
                self.assertIn(f"Budget Remaining:     {remaining} on {day}", out)

    def test_warns_when_a_release_leaves_the_source_past_the_default_share(self):
        warning = "WARNING: claims has spent 5 of its budget ceiling of 5, past the warning share of 80%."
        for run in range(1, 6):
            code, out, err = self.release("--budget-ceiling", "5")
            self.assertEqual(code, 0, err)
            # 4 of 5 is at the share, not past it.
            self.assertEqual(warning in err, run == 5, run)
            self.assertNotIn("WARNING", out)

    def test_the_warning_share_is_configurable(self):
        for share, warned in (("0.5", True), ("1", False)):
            with self.subTest(share=share):
                if os.path.exists("privacy_budget.json"):
                    os.remove("privacy_budget.json")
                code, _, err = self.release("--budget-ceiling", "5", "--budget-warning-share", share, types="ssn,age")
                self.assertEqual(code, 0, err)
                code, _, err = self.release("--budget-ceiling", "5", "--budget-warning-share", share, types="ssn,age")
                self.assertEqual(code, 0, err)
                self.assertEqual("claims has spent 4 of its budget ceiling of 5, past the warning share of 50%" in err, warned)
                self.assertEqual("WARNING: claims" in err, warned)
        for share in ("1.5", "-0.1", "x"):
            with self.subTest(share=share):
                code, _, err = self.release("--budget-warning-share", share)
                self.assertEqual(code, 2)
                self.assertIn("--budget-warning-share", err)

    def test_a_day_past_the_share_is_named(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            for first_day, last_day in (("2026-09-15", "2026-09-20"), ("2026-09-01", "2026-09-30")):
                code, _, err = TestPhilterApi.run_cli(self, "--philter-url", philter.url, "--from", first_day,
                                                      "--to", last_day, "--budget-ceiling", "2.4", "--output", "out.csv")
                self.assertEqual(code, 0, err)
        self.assertIn(f"WARNING: 2026-09-15 has spent 2 of its budget ceiling of 2.4 at pii_count_aggregates@{philter.url}, "
                      f"past the warning share of 80%.", err)


class TestReleaseLog(TempDirTestCase):
    """Each release adds one entry to a log next to the budget, with no counts in it."""

    LOG = "privacy_budget.releases.jsonl"

    def log(self, path=LOG):
        with open(path) as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def test_each_release_appends_one_entry_without_counts(self):
        self.write_counts({"ssn": 987654321, "age": 876543219})
        measurement = MagicMock(return_value=[123456789, 234567891])
        measurement.map.return_value = 0.5
        with patch('main.dp.m.make_laplace', return_value=measurement):
            for _ in range(2):
                code, _, err = self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn,age",
                                             "--scale", "4", "--output", "out.csv")
                self.assertEqual(code, 0, err)
        entries = self.log()
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertRegex(entry.pop("time"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(entry, {"source": "claims", "epsilon": 0.5, "scale": 4.0, "pii_types": ["ssn", "age"]})
        with open(self.LOG) as f:
            log = f.read()
        for count in ("987654321", "876543219", "123456789", "234567891"):
            self.assertNotIn(count, log)

    def test_a_release_through_philter_logs_its_window(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            code, _, err = TestPhilterApi.run_cli(self, "--philter-url", philter.url, "--from", "2026-09-01",
                                                  "--to", "2026-09-30", "--output", "out.csv")
            self.assertEqual(code, 0, err)
        entry = self.log()[0]
        del entry["time"]
        self.assertEqual(entry, {"source": f"pii_count_aggregates@{philter.url}", "epsilon": 1.0, "scale": 2.0,
                                 "pii_types": ["SSN", "PERSON"], "from": "2026-09-01", "to": "2026-09-30"})

    def test_the_log_sits_next_to_the_budget_file(self):
        os.mkdir("store")
        self.write_counts({"ssn": 5})
        code, _, err = self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn",
                                     "--budget-file", "store/budget.json", "--output", "out.csv")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir("store")), ["budget.json", "budget.json.lock", "budget.releases.jsonl"])
        self.assertEqual(len(self.log("store/budget.releases.jsonl")), 1)

    def test_a_refused_release_adds_no_entry(self):
        self.write_counts({"ssn": 5})
        codes = [self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn", "--scale", "1",
                               "--budget-ceiling", "2", "--output", "out.csv")[0] for _ in range(3)]
        self.assertEqual(codes, [0, 0, 1])
        self.assertEqual(len(self.log()), 2)

    def test_concurrent_releases_lose_no_entries(self):
        def release(_):
            try:
                PhilterDiffuse(collection_name="claims", budget_file="budget.json").privatize_counts(
                    {"ssn": 5}, scale=1.0, budget_ceiling=5.0)
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(release, range(32)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(len(self.log("budget.releases.jsonl")), 5)

    def test_a_crash_while_logging_leaves_the_log_readable(self):
        engine = PhilterDiffuse(collection_name="claims", budget_file="budget.json")
        engine.privatize_counts({"ssn": 5}, scale=1.0)
        with open("budget.releases.jsonl") as f:
            log = f.read()
        replace = os.replace
        def fail_on_the_log(source, target):
            if target.endswith(".releases.jsonl"):
                raise OSError("disk full")
            replace(source, target)
        measurement = MagicMock()
        measurement.map.return_value = 1.0
        with patch('main.os.replace', side_effect=fail_on_the_log), \
                patch('main.dp.m.make_laplace', return_value=measurement), self.assertRaises(OSError):
            engine.privatize_counts({"ssn": 5}, scale=1.0)
        with open("budget.releases.jsonl") as f:
            self.assertEqual(f.read(), log)
        self.assertEqual(sorted(os.listdir(".")), ["budget.json", "budget.json.lock", "budget.releases.jsonl"])
        # The charge was written first, so the release is paid for but not logged, and it was never sampled.
        self.assertEqual(engine._get_spent_epsilon(), 2.0)
        measurement.assert_not_called()

    def test_a_new_log_takes_the_access_of_the_budget_file(self):
        engine = PhilterDiffuse(collection_name="claims", budget_file="budget.json")
        engine._charge(1.0, 5.0)
        os.chmod("budget.json", 0o640)
        engine.privatize_counts({"ssn": 5}, scale=1.0)
        self.assertEqual(os.stat("budget.releases.jsonl").st_mode & 0o777, 0o640)

    @patch('main.MongoClient', mongomock.MongoClient)
    def test_mongodb_logs_each_release_next_to_the_budget(self):
        engine = PhilterDiffuse("mongodb://localhost:27017/log_db")
        for _ in range(2):
            engine.privatize_counts({"ssn": 5, "age": 7}, scale=2.0, budget_ceiling=2.5)
            self.assertEqual(engine.spent_after_release, (engine._get_spent_epsilon(), None))
        with self.assertRaises(BudgetExceeded):
            engine.privatize_counts({"ssn": 5, "age": 7}, scale=2.0, budget_ceiling=2.5)
        entries = list(engine.db[main.RELEASE_LOG_COLLECTION].find({}, {"_id": False}))
        self.assertEqual(len(entries), 2)
        for entry in entries:
            del entry["time"]
            self.assertEqual(entry, {"source": "pii_counts", "epsilon": 1.0, "scale": 2.0, "pii_types": ["ssn", "age"]})

def _scope_report(**extra):
    """A Philter Scope report.json with the fields Philter Diffuse reads."""
    report = {
        "timestamp": "2026-11-20T10:00:00Z",
        "total_documents": 27,
        "entity_metrics": {"SSN": 1.0, "EMAIL_ADDRESS": 0.75},
        "entity_stats": {"SSN": {"true_positives": 9, "false_positives": 1, "false_negatives": 0,
                                 "precision": 0.9, "recall": 1.0}},
        "policy": {"name": "default", "identifiers": {"ssn": {}}},
    }
    report.update(extra)
    return report


class TestAuditReportFile(TempDirTestCase):
    """--report writes the audit report as JSON, and --evaluation adds a Philter Scope evaluation."""

    run_cli = TestPhilterApi.run_cli

    def write_evaluation(self, report=None, path="report.json"):
        with open(path, "w") as f:
            f.write(report if isinstance(report, str) else json.dumps(report or _scope_report()))
        return path

    def release(self, *argv):
        self.write_counts({"ssn": 12, "email-address": 30})
        return self.run_main("--input", "counts.json", "--source", "claims", "--pii-types",
                             "ssn,email-address,person", "--output", "out.csv", "--scale", "3.0", *argv)

    def read_report(self, path="audit.json"):
        with open(path) as f:
            return json.load(f)

    def test_membership_risk_is_the_posterior_bound_from_even_odds(self):
        self.assertEqual(main.membership_risk(0.0), 0.5)
        self.assertAlmostEqual(main.membership_risk(math.log(3)), 0.75)
        self.assertAlmostEqual(main.membership_risk(1.0), math.e / (1 + math.e))

    def test_report_holds_the_release_budget_and_risk(self):
        code, out, err = self.release("--report", "audit.json")
        self.assertEqual(code, 0, err)
        report = self.read_report()
        self.assertEqual(report["epsilon"], 1.0)
        self.assertEqual(report["source"], "claims")
        self.assertIsNone(report["window"])
        self.assertEqual(report["mechanism"], {"name": "Discrete Laplace", "scale": 3.0, "types": 3})
        self.assertEqual(report["budget"], {"spent": 1.0, "ceiling": 10.0, "remaining": 9.0, "day": None})
        self.assertAlmostEqual(report["risk"]["membership_risk"], main.membership_risk(1.0))
        self.assertIsNone(report["evaluation"])
        self.assertIn("Membership Risk:      73.1% at most", out)
        self.assertIn("Audit report written to audit.json", out)

    def test_report_file_never_holds_raw_counts(self):
        self.write_counts({"ssn": 987654321})
        measurement = MagicMock(return_value=[123456789])
        measurement.map.return_value = 0.5
        with patch('main.dp.m.make_laplace', return_value=measurement):
            code, _, err = self.run_main("--input", "counts.json", "--source", "claims", "--pii-types", "ssn",
                                         "--output", "out.csv", "--report", "audit.json")
        self.assertEqual(code, 0, err)
        with open("audit.json") as f:
            text = f.read()
        self.assertIn("123456789", text)
        self.assertNotIn("987654321", text)

    def test_report_counts_match_the_csv(self):
        code, _, err = self.release("--report", "audit.json", "--threshold", "5")
        self.assertEqual(code, 0, err)
        with open("out.csv", newline="") as f:
            released = {row[0]: row[1] for row in list(csv.reader(f))[1:]}
        counts = self.read_report()["counts"]
        self.assertEqual(list(counts), ["ssn", "email-address", "person"])
        for pii_type, count in counts.items():
            self.assertEqual("None" if count is None else str(count), released[pii_type])

    def test_a_second_release_reports_what_the_source_has_spent_in_total(self):
        self.assertEqual(self.release()[0], 0)
        code, out, err = self.release("--report", "audit.json")
        self.assertEqual(code, 0, err)
        report = self.read_report()
        self.assertEqual(report["epsilon"], 1.0)
        self.assertEqual(report["budget"]["spent"], 2.0)
        self.assertAlmostEqual(report["risk"]["membership_risk"], main.membership_risk(2.0))

    def test_a_window_reports_its_busiest_day(self):
        with StubPhilter([_entry("claims", "2026-09-01", {"SSN": 4})]) as philter:
            self.assertEqual(self.run_cli("--philter-url", philter.url, "--from", "2026-09-10", "--to", "2026-09-12",
                                          "--output", "out.csv")[0], 0)
            code, out, err = self.run_cli("--philter-url", philter.url, "--from", "2026-09-01", "--to", "2026-09-30",
                                          "--output", "out.csv", "--report", "audit.json")
        self.assertEqual(code, 0, err)
        report = self.read_report()
        self.assertEqual(report["window"], {"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(report["budget"], {"spent": 2.0, "ceiling": 10.0, "remaining": 8.0, "day": "2026-09-10"})

    def test_nothing_released_means_no_budget_in_the_report(self):
        engine = PhilterDiffuse(collection_name="claims")
        engine.privatize_counts({}, scale=1.0)
        report = main.build_audit_report(engine, {}, 0.0, 1.0, 10.0)
        self.assertIsNone(report["budget"])
        self.assertIsNone(report["risk"])

    def test_evaluation_gives_each_released_type_its_rates(self):
        evaluation = main.load_evaluation(self.write_evaluation(), ["ssn", "email-address", "person"])
        self.assertEqual(evaluation["types"], {
            "ssn": {"evaluated": True, "recall": 1.0, "precision": 0.9},
            "email-address": {"evaluated": True, "recall": 0.75, "precision": None},
            "person": {"evaluated": False},
        })
        self.assertEqual((evaluation["types_evaluated"], evaluation["types_released"]), (2, 3))
        self.assertAlmostEqual(evaluation["coverage"], 2 / 3)
        self.assertEqual(evaluation["documents"], 27)
        self.assertEqual(evaluation["evaluated_at"], "2026-11-20T10:00:00Z")

    def test_policy_hash_does_not_depend_on_key_order_or_spacing(self):
        first = main.load_evaluation(self.write_evaluation(_scope_report(policy={"a": 1, "b": [1, 2]})), ["ssn"])
        second = main.load_evaluation(self.write_evaluation('{"policy": {"b": [1,2], "a": 1}, "entity_metrics": {}}'), ["ssn"])
        self.assertEqual(first["policy_sha256"], second["policy_sha256"])
        self.assertEqual(first["policy_sha256"], hashlib.sha256(b'{"a":1,"b":[1,2]}').hexdigest())
        self.assertIsNone(main.load_evaluation(self.write_evaluation(_scope_report(policy=None)), ["ssn"])["policy_sha256"])

    def test_report_and_printed_table_carry_the_evaluation(self):
        self.write_evaluation()
        code, out, err = self.release("--report", "audit.json", "--evaluation", "report.json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_report()["evaluation"]["types"]["person"], {"evaluated": False})
        self.assertIn("Policy Evaluation:    2 of 3 types, 27 labelled documents", out)
        self.assertRegex(out, r"ssn\s+\|\s+\S+\s+\| 1.00")
        self.assertRegex(out, r"person\s+\|\s+\S+\s+\| not evaluated")

    def test_a_bad_evaluation_stops_the_run_before_any_budget_is_spent(self):
        for name, report in (("not JSON", "{"),
                             ("not a Philter Scope report", '{"total_documents": 3}'),
                             ("recall out of range", json.dumps(_scope_report(entity_metrics={"SSN": 1.5}))),
                             ("recall missing", json.dumps(_scope_report(entity_stats={"SSN": {"precision": 1.0}})))):
            with self.subTest(name):
                self.write_evaluation(report)
                code, _, _ = self.release("--evaluation", "report.json", "--report", "audit.json")
                self.assertEqual(code, 1)
                self.assertFalse(os.path.exists("privacy_budget.json"))
                self.assertFalse(os.path.exists("out.csv"))
                self.assertFalse(os.path.exists("audit.json"))


if __name__ == "__main__":
    unittest.main()
