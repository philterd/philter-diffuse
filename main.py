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

import math
import json
import argparse
import csv
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
import opendp.prelude as dp
from pymongo import MongoClient

# Project version. Philter Diffuse is early-access (0.x): the differential-privacy
# mechanism and CLI are usable and tested, but the interface may still change
# before a 1.0 release. See docs/docs/status.md for the maturity checklist.
__version__ = "0.1.0"

# Enable OpenDP features for 2026 stability
dp.enable_features("contrib")

# The environment variable holding the Philter API key. It is read from the environment rather
# than a command-line argument so it does not appear in shell history or process listings.
PHILTER_API_KEY_ENV = "PHILTER_API_KEY"


class PhilterApiError(Exception):
    """
    Philter could not be reached, or refused the request. The message is written for a person
    and never contains the API key.
    """


class CountRecordingDisabled(Exception):
    """
    Philter reports that PII count recording (the diffuseCountsEnabled admin setting) is off.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    Refuses redirects so the API key in the Authorization header is never sent to another address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PhilterClient:
    """
    Reads the PII counts Philter records for Philter Diffuse through ``GET /api/pii-counts``
    (Philter 4.0 and later), so no access to Philter's database is needed.
    """

    # The largest page the endpoint returns.
    PAGE_SIZE = 100

    # Hints for the refusals a reader of the PII counts is most likely to hit, keyed by Philter's
    # stable ``reason`` code.
    REASON_HINTS = {
        "invalid_credentials": f"Check that {PHILTER_API_KEY_ENV} holds a valid Philter API key.",
        "session_expired": f"The key in {PHILTER_API_KEY_ENV} has expired. Use a long-lived API key.",
        "user_deactivated": "The API key's user is deactivated.",
        "missing_scope": "The API key needs the pii-counts:read scope.",
        "admin_required": "The API key's user must be a Philter administrator.",
    }

    def __init__(self, base_url, api_key, ca_cert=None, timeout=30):
        api_key = (api_key or "").strip()
        if not api_key:
            raise PhilterApiError(f"No Philter API key. Set the {PHILTER_API_KEY_ENV} environment variable.")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in api_key):
            # Checked here because http.client's own refusal quotes the whole Authorization header.
            raise PhilterApiError(f"{PHILTER_API_KEY_ENV} contains a control character, such as a line break. Set it to the API key alone.")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PhilterApiError(f"The Philter URL must start with https:// or http://, such as https://localhost:8080 (got {base_url}).")
        # Lowercase the scheme and host so the budget, which is keyed by this URL, is not split by
        # how the address is typed.
        self.base_url = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(),
                                        query="", fragment="").geturl().rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        handlers = [_NoRedirect()]
        if parsed.scheme == "https":
            # Verification stays on. A CA certificate (or Philter's self-signed certificate itself)
            # can be trusted in addition to the system's certificate authorities.
            context = ssl.create_default_context()
            if ca_cert:
                try:
                    context.load_verify_locations(cafile=ca_cert)
                except (OSError, ssl.SSLError) as e:
                    raise PhilterApiError(f"Could not load the certificate in {ca_cert}: {e}.") from None
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def fetch_pii_count_aggregates(self, context=None, date_from=None, date_to=None):
        """
        Sums the counts Philter returns per (context, day) into a flat {pii_type: total} map, reading
        every page. Filters are applied by Philter. Raises CountRecordingDisabled when Philter says
        it is not recording counts, so an empty result is never mistaken for no PII.
        """
        params = {"limit": self.PAGE_SIZE}
        if context is not None:
            params["context"] = context
        if date_from is not None:
            params["from"] = date_from
        if date_to is not None:
            params["to"] = date_to

        totals = {}
        seen = set()
        offset = 0
        while True:
            page = self._get("/api/pii-counts", dict(params, offset=offset))
            if not isinstance(page, dict) or not isinstance(page.get("recordingEnabled"), bool) \
                    or not isinstance(page.get("counts"), list):
                raise PhilterApiError(f"The response from {self.base_url}/api/pii-counts is not Philter's PII counts listing. Check that --philter-url points at Philter 4.0 or later.")
            if not page["recordingEnabled"]:
                raise CountRecordingDisabled(
                    "Philter reports that PII count recording is turned off (the diffuseCountsEnabled "
                    "admin setting), so its counts do not reflect the PII it has found. An administrator "
                    "can turn it on with Philter's Settings API.")
            entries = page["counts"]
            for entry in entries:
                # The listing is newest first, so a day bucket created while paging shifts the
                # entries down by one and the last entry of a page comes back on the next. Counting
                # it twice would let one redaction add two to a type, breaking sensitivity = 1.
                key = (entry.get("context"), entry.get("day"))
                if key in seen:
                    continue
                seen.add(key)
                for pii_type, count in (entry.get("counts") or {}).items():
                    totals[pii_type] = totals.get(pii_type, 0) + int(count)
            offset += len(entries)
            if not entries or offset >= int(page.get("total", 0)):
                return totals

    def _get(self, path, params):
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            raise PhilterApiError(self._describe_refusal(e)) from None
        except urllib.error.URLError as e:
            message = f"Could not reach Philter at {self.base_url}: {e.reason}."
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                message += " To trust Philter's certificate, pass it (or the CA that issued it) with --philter-ca-cert."
            raise PhilterApiError(message) from None
        except (TimeoutError, OSError) as e:
            raise PhilterApiError(f"Could not reach Philter at {self.base_url}: {e}.") from None

        try:
            return json.loads(body)
        except ValueError:
            raise PhilterApiError(f"Philter at {self.base_url} did not return JSON from {path}. Check that --philter-url points at Philter 4.0 or later.") from None

    def _describe_refusal(self, error):
        request_id = error.headers.get("X-Request-Id") if error.headers else None
        if 300 <= error.code < 400:
            message = (f"Philter at {self.base_url} answered with a redirect (HTTP {error.code}), which is not "
                       f"followed so the API key is not sent elsewhere. Point --philter-url at the address Philter serves.")
        else:
            message, reason = None, None
            try:
                body = json.loads(error.read())
                message, reason = body.get("message"), body.get("reason")
            except (ValueError, AttributeError):
                pass
            message = f"Philter refused the request (HTTP {error.code}" + (f", {reason})" if reason else ")") \
                + (f": {message}" if message else ".")
            if error.code == 404:
                message += f" {self.base_url} may not serve /api/pii-counts: reading counts through the API requires Philter 4.0 or later."
            elif reason in self.REASON_HINTS:
                message += f" {self.REASON_HINTS[reason]}"
        if request_id:
            message += f" (Philter request ID: {request_id})"
        return message


class PhilterDiffuse:
    """
    Differential Privacy Engine for MongoDB.
    Provides mathematical guarantees for PII count aggregations.
    """

    def __init__(self, mongo_uri=None, collection_name=None):
        if mongo_uri:
            self.client = MongoClient(mongo_uri)
            # MongoDB URI format: mongodb://host:port/database
            # We'll parse the database from the URI path
            import urllib.parse
            parsed = urllib.parse.urlparse(mongo_uri)
            db_name = parsed.path.strip('/')
            
            if not db_name:
                # Fallback to default if not specified in URI
                db_name = "philter"
            
            self.db = self.client[db_name]
            self.collection_name = collection_name or "pii_counts"
            self.collection = self.db[self.collection_name]
            self.metadata_collection = self.db["privacy_metadata"]
        else:
            self.client = None
            self.collection_name = collection_name or "json_file" # Default label for JSON mode
            self.budget_file = "privacy_budget.json"
        self.sensitivity = 1  # Standard for count-based individual records

    def _get_spent_epsilon(self):
        """
        Retrieves the total epsilon spent for the current collection.
        """
        if self.client:
            record = self.metadata_collection.find_one({"collection": self.collection_name})
            return record.get("spent_epsilon", 0.0) if record else 0.0
        else:
            if os.path.exists(self.budget_file):
                with open(self.budget_file, 'r') as f:
                    budget_data = json.load(f)
                    return budget_data.get(self.collection_name, 0.0)
            return 0.0

    def _update_spent_epsilon(self, new_total):
        """
        Updates the total epsilon spent for the current collection.
        """
        if self.client:
            self.metadata_collection.update_one(
                {"collection": self.collection_name},
                {"$set": {"spent_epsilon": new_total}},
                upsert=True
            )
        else:
            budget_data = {}
            if os.path.exists(self.budget_file):
                with open(self.budget_file, 'r') as f:
                    budget_data = json.load(f)
            budget_data[self.collection_name] = new_total
            with open(self.budget_file, 'w') as f:
                json.dump(budget_data, f)

    def fetch_pii_counts(self, pii_fields):
        """
        Fetches raw counts for specific PII types from MongoDB.
        In a production scenario, this replaces manual count logic.
        """
        if not self.client:
            raise ValueError("MongoDB client not initialized. Cannot fetch counts from database.")

        raw_counts = {}
        for field in pii_fields:
            # Simple count of documents where the PII field exists
            count = self.collection.count_documents({field: {"$exists": True}})
            raw_counts[field] = count
        return raw_counts

    def fetch_pii_count_aggregates(self, context=None):
        """
        Sums Philter's aggregated, document-presence PII counts from the
        ``pii_count_aggregates`` collection that Philter writes. Philter stores one document
        per (context, day) with a ``counts`` map of {pii_type: number-of-documents-containing-it}.
        This returns a flat {pii_type: total} map summed across the selected buckets.

        Because each redaction contributes at most one to any type's count (document presence),
        summing across buckets preserves the sensitivity=1 assumption that the differential-privacy
        guarantee relies on. Optionally restrict to a single ``context``.
        """
        if not self.client:
            raise ValueError("MongoDB client not initialized. Cannot fetch counts from database.")

        query = {}
        if context is not None:
            query["context"] = context

        totals = {}
        for doc in self.db["pii_count_aggregates"].find(query):
            counts = doc.get("counts") or {}
            for pii_type, count in counts.items():
                totals[pii_type] = totals.get(pii_type, 0) + int(count)
        return totals

    def load_counts_from_json(self, file_path):
        """
        Loads raw counts from a JSON file.
        """
        with open(file_path, 'r') as f:
            return json.load(f)

    def write_counts_to_csv(self, private_counts, file_path, threshold=0):
        """
        Writes privatized counts to a CSV file.
        """
        with open(file_path, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['PII Type', 'Private Count'])
            for key, value in private_counts.items():
                # Ensure counts don't display as negative
                safe_count = max(0, value)
                
                # Apply threshold
                display_count = "None" if safe_count < threshold else safe_count
                
                writer.writerow([key, display_count])

    def privatize_counts(self, raw_counts, scale=2.0, budget_ceiling=10.0):
        """
        Applies Discrete Laplace noise to counts and calculates Epsilon.
        Checks against a persistent budget ceiling.
        """
        # 1. Calculate Epsilon for this query
        if scale <= 0:
            raise ValueError("Scale must be a positive number.")
            
        epsilon_this_query = self.sensitivity / scale
        
        # 2. Check Budget
        spent_epsilon = self._get_spent_epsilon()
        budget_exhausted = False
        if (spent_epsilon + epsilon_this_query) > budget_ceiling:
            # Refuse to execute by using infinite noise
            # Alternatively, we could raise an error, but the prompt says 
            # "refuse to execute or automatically increase the noise (scale) to infinite levels"
            scale = float('inf')
            epsilon_this_query = 0.0
            budget_exhausted = True
            print(f"WARNING: Budget ceiling of {budget_ceiling} exceeded. Infinite noise applied.")

        # 3. Define the Measurement
        if math.isinf(scale):
            # If scale is infinite, all results are just noise, or we can just return 0 
            # but usually infinite noise means we don't return meaningful data.
            # OpenDP make_laplace might handle inf scale, but let's be safe.
            private_results = {k: 0 for k in raw_counts}
        else:
            measurement = dp.m.make_laplace(
                input_domain=dp.atom_domain(T=int),
                input_metric=dp.absolute_distance(T=int),
                scale=float(scale)
            )
            # 4. Privatize and ensure no negative numbers
            # Cast counts to int for OpenDP compatibility (i32)
            private_results = {k: max(0, measurement(int(v))) for k, v in raw_counts.items()}

        # 5. Update Budget if it was a valid query
        if not math.isinf(scale):
            self._update_spent_epsilon(spent_epsilon + epsilon_this_query)

        return private_results, epsilon_this_query, budget_exhausted

    def generate_audit_report(self, raw, private, epsilon, csv_file=None, threshold=0, budget_exhausted=False):
        """
        Outputs a compliance-ready report for auditors.
        """
        print("\n" + "="*40)
        print("PRIVACY AUDIT REPORT")
        print("="*40)
        if budget_exhausted:
            print("!!! WARNING: PRIVACY BUDGET EXHAUSTED !!!")
            print("!!! NO MORE ACCURATE QUERIES ALLOWED !!!")
            print("-" * 40)
        print(f"Privacy Loss (Epsilon): {epsilon:.4f}")
        print(f"Protection Strength:   {self._interpret_epsilon(epsilon)}")
        print(f"Influence Limit:      e^{epsilon:.2f} ({math.exp(epsilon):.2f}x)")
        if csv_file:
            print(f"Output File:          {csv_file}")
        if threshold > 0:
            print(f"Count Threshold:      {threshold}")
        print("-" * 40)
        print(f"{'PII Type':<15} | {'Raw':<8} | {'Private':<8}")
        print("-" * 40)
        for key in raw:
            # Ensure counts don't display as negative
            safe_count = max(0, private[key])
            
            # Apply threshold
            display_count = "None" if safe_count < threshold else safe_count
            
            print(f"{key:<15} | {raw[key]:<8} | {display_count:<8}")
        
        if csv_file:
            print("-" * 40)
            print(f"Results successfully written to {csv_file}")
            
        print("="*40 + "\n")

    def _interpret_epsilon(self, epsilon):
        if epsilon <= 0.1: return "EXTREME"
        if epsilon <= 1.0: return "HIGH"
        if epsilon <= 5.0: return "MODERATE"
        return "LOW (Testing Only)"

def _day(value):
    """
    An argparse type for a UTC day in Philter's YYYY-MM-DD format.
    """
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        day = None
    # strptime accepts 2026-9-1, which Philter refuses, so require the canonical form.
    if day is None or day.isoformat() != value:
        raise argparse.ArgumentTypeError(f"'{value}' is not a date in YYYY-MM-DD format, such as 2026-09-01.")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Philter Diffuse: Differential Privacy Engine.")
    parser.add_argument("--version", action="version", version=f"Philter Diffuse {__version__}")
    parser.add_argument("--input", type=str, help="Path to a JSON document containing PII entity counts.")
    parser.add_argument("--mongo-uri", type=str, default="mongodb://localhost:27017/philter", help="MongoDB connection URI (format: mongodb://host:port/database).")
    parser.add_argument("--scale", type=float, default=2.0, help="Scale for Laplace noise (higher = more privacy, less accuracy).")
    parser.add_argument("--output", type=str, required=True, help="Path to write the privatized PII counts to a CSV file.")
    parser.add_argument("--threshold", type=int, default=0, help="Counts below this threshold will be output as 'None'.")
    parser.add_argument("--budget-ceiling", type=float, default=10.0, help="The maximum total epsilon allowed per collection.")
    parser.add_argument("--aggregates", action="store_true", help="Read Philter's aggregated, document-presence PII counts instead of counting documents per field. With --philter-url they are read through Philter's API; otherwise from the pii_count_aggregates collection (deprecated).")
    parser.add_argument("--context", type=str, default=None, help="When reading aggregates, restrict to a single Philter context.")
    parser.add_argument("--philter-url", type=str, default=None, metavar="URL", help=f"Read the aggregates through Philter's API (Philter 4.0 or later) at this URL, such as https://localhost:8080, instead of from MongoDB. Implies --aggregates. The API key is read from the {PHILTER_API_KEY_ENV} environment variable.")
    parser.add_argument("--philter-ca-cert", type=str, default=None, metavar="PEM_FILE", help="With --philter-url, a PEM file holding a certificate to trust in addition to the system's certificate authorities, such as Philter's self-signed certificate.")
    parser.add_argument("--from", dest="date_from", type=_day, default=None, metavar="YYYY-MM-DD", help="With --philter-url, read only counts for this UTC day or later.")
    parser.add_argument("--to", dest="date_to", type=_day, default=None, metavar="YYYY-MM-DD", help="With --philter-url, read only counts for this UTC day or earlier.")

    args = parser.parse_args(argv)

    if args.philter_url:
        if args.input:
            parser.error("--input and --philter-url cannot be used together.")
        args.aggregates = True
    else:
        if args.date_from or args.date_to:
            parser.error("--from and --to require --philter-url.")
        if args.philter_ca_cert:
            parser.error("--philter-ca-cert requires --philter-url.")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--from must not be after --to.")

    FIELDS_TO_MONITOR = ["creditcard", "ssn", "age", "zipcode"]

    try:
        if args.input:
            # Initialize Engine without MongoDB for JSON mode
            # Use the JSON filename as the collection name for budget tracking
            json_filename = os.path.basename(args.input)
            engine = PhilterDiffuse(collection_name=json_filename)
            print(f"Loading counts from {args.input}...")
            raw_data = engine.load_counts_from_json(args.input)
        elif args.philter_url:
            # Read through Philter's API. No MongoDB connection is made: the budget is tracked in
            # the local budget file, keyed by the Philter deployment the counts came from.
            try:
                client = PhilterClient(args.philter_url, os.environ.get(PHILTER_API_KEY_ENV),
                                       ca_cert=args.philter_ca_cert)
                engine = PhilterDiffuse(collection_name=f"pii_count_aggregates@{client.base_url}")
                print(f"Reading PII counts from Philter at {client.base_url}...")
                raw_data = client.fetch_pii_count_aggregates(context=args.context, date_from=args.date_from,
                                                             date_to=args.date_to)
            except CountRecordingDisabled as e:
                print(f"Error: {e} No privatized output was written.", file=sys.stderr)
                sys.exit(1)
            except PhilterApiError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            # Initialize Engine with MongoDB
            try:
                engine = PhilterDiffuse(args.mongo_uri)
                # 1. Fetch raw data
                if args.aggregates:
                    print(f"WARNING: Reading pii_count_aggregates from MongoDB with --mongo-uri is deprecated and "
                          f"will be removed in a future release. Read the counts through Philter's API instead "
                          f"with --philter-url and the {PHILTER_API_KEY_ENV} environment variable (Philter 4.0 or later).",
                          file=sys.stderr)
                    raw_data = engine.fetch_pii_count_aggregates(context=args.context)
                else:
                    raw_data = engine.fetch_pii_counts(FIELDS_TO_MONITOR)
            except Exception as e:
                print(f"Error: MongoDB connection failed: {e}")
                sys.exit(1)

        # 2. Privatize
        private_data, spent_epsilon, exhausted = engine.privatize_counts(raw_data, scale=args.scale, budget_ceiling=args.budget_ceiling)

        # 3. Output to CSV
        engine.write_counts_to_csv(private_data, args.output, threshold=args.threshold)
        
        # 4. Report
        engine.generate_audit_report(raw_data, private_data, spent_epsilon, csv_file=args.output, threshold=args.threshold, budget_exhausted=exhausted)

    except SystemExit:
        # Propagate the exit status so a failed run does not look like a successful one.
        raise
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
