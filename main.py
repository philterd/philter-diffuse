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

import contextlib
import hashlib
import math
import json
import argparse
import csv
import os
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
import opendp.prelude as dp
from pymongo import MongoClient, ReturnDocument

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows has no flock, so concurrent runs there are not serialized.

# Project version. Philter Diffuse is early-access (0.x): the differential-privacy
# mechanism and CLI are usable and tested, but the interface may still change
# before a 1.0 release. See docs/docs/status.md for the maturity checklist.
__version__ = "0.1.0"

# Enable OpenDP features for 2026 stability
dp.enable_features("contrib")

# The environment variable holding the Philter API key. It is read from the environment rather
# than a command-line argument so it does not appear in shell history or process listings.
PHILTER_API_KEY_ENV = "PHILTER_API_KEY"

# Where the budget lives unless --budget-file or this environment variable says otherwise. It is
# not relative to the working directory: a new directory must not mean a fresh budget.
BUDGET_FILE_ENV = "PHILTER_DIFFUSE_BUDGET_FILE"
DEFAULT_BUDGET_FILE = os.path.join("~", ".philter-diffuse", "privacy_budget.json")

# The largest count OpenDP's 64-bit integers hold.
MAX_COUNT = 2**63 - 1

# A release that leaves a source, or a day, past this share of the budget ceiling prints a warning.
DEFAULT_WARNING_SHARE = 0.8

# The MongoDB collection that holds the release log, next to the privacy_metadata collection.
RELEASE_LOG_COLLECTION = "privacy_releases"

# The last day a YYYY-MM-DD date can name.
LAST_DAY = date.max.isoformat()


class PhilterApiError(Exception):
    """
    Philter could not be reached, or refused the request. The message is written for a person
    and never contains the API key.
    """


class CountRecordingDisabled(Exception):
    """
    Philter reports that PII count recording (the diffuseCountsEnabled admin setting) is off.
    """


class BudgetExceeded(Exception):
    """
    A release would pass the budget ceiling, so nothing was released and nothing was charged.
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
                # Entries are told apart by id: two users' contexts of one name on one day are
                # separate entries, and another user's may come without its context name.
                key = entry.get("id")
                if not isinstance(key, str) or not key:
                    raise PhilterApiError(f"An entry from {self.base_url}/api/pii-counts has no id. Philter Diffuse needs a Philter release that returns one.")
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
            elif error.code == 400 and reason is None and self.base_url.startswith("http://"):
                # Philter's HTTPS port answers a plain HTTP request with a bare 400.
                message += " Philter serves HTTPS by default. Check whether --philter-url should start with https://."
            elif reason in self.REASON_HINTS:
                message += f" {self.REASON_HINTS[reason]}"
        if request_id:
            message += f" (Philter request ID: {request_id})"
        return message


def _daily_ledger(entry):
    """
    Reads a budget file entry kept per UTC day: the epsilon spent on every day, and the window and
    epsilon of each release charged since.
    """
    if entry is None:
        return {"spent_on_every_day": 0.0, "releases": []}
    if isinstance(entry, dict):
        return entry
    # A 0.1.0 entry is one running total with no days. Counting it as spent on every day means
    # upgrading cannot give a release more budget than it had before.
    return {"spent_on_every_day": entry, "releases": []}


def _busiest_day(ledger, first_day, last_day):
    """
    Returns the day from first_day to last_day, both inclusive, with the most epsilon spent, and that
    amount. What a day has spent only rises on the first day of a release's window, so those days and
    first_day are the only ones to check. Days are YYYY-MM-DD, so they compare as strings.
    """
    def spent_on(day):
        return ledger["spent_on_every_day"] + sum(
            release["epsilon"] for release in ledger["releases"] if release["from"] <= day <= release["to"])

    days = [first_day] + sorted(release["from"] for release in ledger["releases"]
                                if first_day < release["from"] <= last_day)
    return max(((day, spent_on(day)) for day in days), key=lambda day_spent: day_spent[1])


def _spent_by_day(ledger):
    """
    Returns (first_day, last_day, spent) for each run of days that has spent more than the amount
    spent on every day, in order. What a day has spent changes only on the first day of a release's window and on the day
    after its last, so the days between two of those have all spent the same.
    """
    def shift(day, days):
        return (date.fromisoformat(day) + timedelta(days=days)).isoformat()

    starts = sorted({release["from"] for release in ledger["releases"]}
                    | {shift(release["to"], 1) for release in ledger["releases"] if release["to"] < LAST_DAY})
    runs = []
    for i, start in enumerate(starts):
        end = shift(starts[i + 1], -1) if i + 1 < len(starts) else LAST_DAY
        spent = _busiest_day(ledger, start, start)[1]
        if runs and math.isclose(runs[-1][2], spent, abs_tol=1e-12):
            runs[-1] = (runs[-1][0], end, runs[-1][2])
        else:
            runs.append((start, end, spent))
    return [run for run in runs if not math.isclose(run[2], ledger["spent_on_every_day"], abs_tol=1e-12)]


def _replace_file(path, content, mode_from=None):
    """
    Replaces the file in one step, so a crash cannot leave it truncated. A file that already exists
    keeps its access; a new one takes the access of mode_from when that is given.
    """
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    try:
        # mkstemp makes the file private; keep whatever access the file already had.
        if os.path.exists(path):
            shutil.copymode(path, temporary)
        elif mode_from and os.path.exists(mode_from):
            shutil.copymode(mode_from, temporary)
        with os.fdopen(descriptor, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


class PhilterDiffuse:
    """
    Differential Privacy Engine for MongoDB.
    Provides mathematical guarantees for PII count aggregations.
    """

    def __init__(self, mongo_uri=None, collection_name=None, budget_file=None):
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
            self.release_log_collection = self.db[RELEASE_LOG_COLLECTION]
        else:
            self.client = None
            self.collection_name = collection_name or "json_file" # Default label for JSON mode
            self.budget_file = os.path.expanduser(budget_file or os.environ.get(BUDGET_FILE_ENV) or DEFAULT_BUDGET_FILE)
            # The release log sits next to the budget file: privacy_budget.json logs to
            # privacy_budget.releases.jsonl.
            self.release_log = f"{os.path.splitext(self.budget_file)[0]}.releases.jsonl"
        self.sensitivity = 1  # How much one document can change any one count
        # (spent, day) after the last release: the epsilon the source has spent, or with a window the
        # epsilon spent on the window's busiest day, which is then named.
        self.spent_after_release = None

    def _get_spent_epsilon(self):
        """
        Retrieves the total epsilon spent for the current collection.
        """
        if self.client:
            record = self.metadata_collection.find_one({"collection": self.collection_name})
            return record.get("spent_epsilon", 0.0) if record else 0.0
        return self._read_budget_file().get(self.collection_name, 0.0)

    def budget_status(self, budget_ceiling):
        """
        Returns the lines --budget-status prints: the spent and remaining epsilon of every source in
        the budget store, per UTC day for a source kept by day. It only reads: nothing is created,
        locked, or changed, and no counts are read.
        """
        def amounts(spent):
            return f"spent {spent:g}, remaining {max(0.0, budget_ceiling - spent):g}"

        if self.client:
            # The database name only: the URI can hold a password.
            where = f"the privacy_metadata collection of MongoDB database {self.db.name}"
            entries = {record["collection"]: record.get("spent_epsilon", 0.0)
                       for record in self.metadata_collection.find()}
        else:
            where = self.budget_file
            entries = self._read_budget_file()
        if not entries:
            return [f"No privacy budget has been spent in {where}."]

        lines = [f"Privacy budget in {where}, against a ceiling of {budget_ceiling:g}:"]
        for source in sorted(entries):
            entry = entries[source]
            if not isinstance(entry, dict):
                lines.append(f"{source}: {amounts(entry)}")
                continue
            ledger = _daily_ledger(entry)
            lines.append(f"{source}, per UTC day:")
            for first_day, last_day, spent in _spent_by_day(ledger):
                days = first_day if first_day == last_day else f"{first_day} to {last_day}"
                lines.append(f"  {days}: {amounts(spent)}")
            lines.append(f"  every other day: {amounts(ledger['spent_on_every_day'])}")
        return lines

    def _read_budget_file(self):
        if not os.path.exists(self.budget_file):
            return {}
        try:
            with open(self.budget_file, 'r') as f:
                return json.load(f)
        except ValueError:
            # Starting from zero here would discard every earlier charge.
            raise ValueError(f"The privacy budget file {self.budget_file} is not valid JSON. Restore it from a backup.") from None

    def _write_budget_file(self, budget_data):
        """
        Replaces the file in one step, so a crash cannot leave a truncated budget behind.
        """
        _replace_file(self.budget_file, json.dumps(budget_data))

    def _log_release(self, release):
        """
        Adds one entry to the release log. It is called under the budget lock, right after the charge
        is written, so concurrent runs cannot drop each other's entries. The log is replaced in one step
        like the budget, so a crash leaves it as it was, and a new log takes the budget file's access.
        """
        log = ""
        if os.path.exists(self.release_log):
            with open(self.release_log, 'r') as f:
                log = f.read()
            if log and not log.endswith("\n"):
                log += "\n"
        _replace_file(self.release_log, log + json.dumps(release) + "\n", mode_from=self.budget_file)

    @contextlib.contextmanager
    def _budget_lock(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.budget_file)), exist_ok=True)
        with open(f"{self.budget_file}.lock", 'w') as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _charge(self, epsilon, budget_ceiling, window=None, release=None):
        """
        Adds epsilon to the amount spent, as one step with the ceiling check so that concurrent runs
        cannot each pass the check on the same balance. Raises BudgetExceeded without charging when
        the ceiling would be passed. Returns (spent, day): what the source has spent after the charge,
        or with a window what the window's busiest day has spent, and that day.

        With a window, a (from, to) pair of UTC days, the budget file keeps the amount spent per day:
        the release is charged to every day in the window, and the ceiling applies to each day. A
        document is recorded under one day, so only the releases whose windows include that day add
        to its privacy loss.

        With release, the log entry for this release, the entry is added to the release log right
        after the charge is written. A refused release adds nothing.
        """
        if self.client:
            self.metadata_collection.create_index("collection", unique=True)
            self.metadata_collection.update_one({"collection": self.collection_name},
                                                {"$setOnInsert": {"spent_epsilon": 0.0}}, upsert=True)
            charged = self.metadata_collection.find_one_and_update(
                {"collection": self.collection_name, "spent_epsilon": {"$lte": budget_ceiling - epsilon}},
                {"$inc": {"spent_epsilon": epsilon}}, return_document=ReturnDocument.AFTER)
            if charged is not None:
                if release is not None:
                    self.release_log_collection.insert_one(dict(release))
                return charged["spent_epsilon"], None
            spent = self._get_spent_epsilon()
        else:
            with self._budget_lock():
                budget_data = self._read_budget_file()
                if window:
                    ledger = _daily_ledger(budget_data.get(self.collection_name))
                    day, spent = _busiest_day(ledger, *window)
                    if spent + epsilon <= budget_ceiling:
                        ledger["releases"].append({"from": window[0], "to": window[1], "epsilon": epsilon})
                        budget_data[self.collection_name] = ledger
                        self._write_budget_file(budget_data)
                        if release is not None:
                            self._log_release(release)
                        # Every day in the window was charged the same, so the busiest day stays so.
                        return spent + epsilon, day
                    raise BudgetExceeded(f"This release costs epsilon {epsilon:g}, and {day} has spent {spent:g} "
                                         f"of its budget ceiling of {budget_ceiling:g} at {self.collection_name}. "
                                         f"Nothing was released and no budget was spent. Raise --scale or list "
                                         f"fewer types to spend less, or release days with budget left.")
                spent = budget_data.get(self.collection_name, 0.0)
                if spent + epsilon <= budget_ceiling:
                    budget_data[self.collection_name] = spent + epsilon
                    self._write_budget_file(budget_data)
                    if release is not None:
                        self._log_release(release)
                    return spent + epsilon, None
        raise BudgetExceeded(f"This release costs epsilon {epsilon:g}, and {self.collection_name} has spent "
                             f"{spent:g} of its budget ceiling of {budget_ceiling:g}. Nothing was released and "
                             f"no budget was spent. Raise --scale or list fewer types to spend less.")

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
        Loads raw counts from a JSON file. Raises ValueError unless it is a flat object of whole,
        non-negative counts: truncating or clamping a bad count would release a wrong answer.
        """
        with open(file_path, 'r') as f:
            try:
                raw_counts = json.load(f)
            except ValueError:
                raise ValueError(f"{file_path} is not valid JSON.") from None
        if not isinstance(raw_counts, dict):
            raise ValueError(f"{file_path} must hold a JSON object of PII types and counts, such as {{\"ssn\": 10}}.")
        for pii_type, count in raw_counts.items():
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_COUNT:
                raise ValueError(f"The count for {pii_type} in {file_path} must be a whole number of 0 or more.")
        return raw_counts

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

    def privatize_counts(self, raw_counts, scale=2.0, budget_ceiling=10.0, window=None):
        """
        Applies Discrete Laplace noise to counts and returns (private_counts, epsilon). Raises
        BudgetExceeded, releasing nothing, when the release would pass the persistent budget ceiling.
        With a window, a (from, to) pair of UTC days, the ceiling applies to each day in it.
        """
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("Scale must be a positive number.")

        keys = list(raw_counts)
        if not keys:
            return {}, 0.0

        # One document adds at most one to each type's count, but it can add to every type at once,
        # so the counts released together have an L1 sensitivity of one per type. Charging a single
        # count's epsilon would understate the loss by up to the number of types. The keys must be
        # the declared types (see restrict_to_declared_types), or the charge depends on the data.
        measurement = dp.m.make_laplace(
            input_domain=dp.vector_domain(dp.atom_domain(T="i64")),
            input_metric=dp.l1_distance(T="i64"),
            scale=float(scale)
        )
        epsilon = measurement.map(d_in=self.sensitivity * len(keys))

        # The log entry names what was released and what it cost, never a count, raw or private.
        release = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "source": self.collection_name, "epsilon": epsilon, "scale": float(scale), "pii_types": keys}
        if window:
            release["from"], release["to"] = window

        # Charge before sampling: a failure after this overcharges, while the reverse could release
        # counts that were never paid for.
        self.spent_after_release = self._charge(epsilon, budget_ceiling, window, release)

        noisy = measurement([int(raw_counts[k]) for k in keys])
        return {k: max(0, v) for k, v in zip(keys, noisy)}, epsilon

    def generate_audit_report(self, private, epsilon, csv_file=None, threshold=0, spent=None,
                              budget_ceiling=None, day=None, evaluation=None):
        """
        Prints the audit report for a release. It holds no raw counts: standard output ends up in
        logs, and exact counts there would undo the noise added to the release. With spent and
        budget_ceiling, it gives the budget spent and remaining after the release, on the given day
        for a budget kept per day, and the membership risk for what was spent. With evaluation, what
        load_evaluation read, it adds the policy's recall for each type.
        """
        print("\n" + "="*40)
        print("PRIVACY AUDIT REPORT")
        print("="*40)
        print(f"Privacy Loss (Epsilon): {epsilon:.4f}")
        print(f"Protection Strength:   {self._interpret_epsilon(epsilon)}")
        print(f"Influence Limit:      e^{epsilon:.2f} ({math.exp(epsilon):.2f}x)")
        if spent is not None and budget_ceiling is not None:
            on_day = f" on {day}" if day else ""
            print(f"Budget Spent:         {spent:.4f} of {budget_ceiling:.4f}{on_day}"
                  + (" (the busiest day from --from to --to)" if day else ""))
            print(f"Budget Remaining:     {max(0.0, budget_ceiling - spent):.4f}{on_day}")
            print(f"Membership Risk:      {membership_risk(spent):.1%} at most, from even odds{on_day}")
        if evaluation:
            print(f"Policy Evaluation:    {evaluation['types_evaluated']} of {evaluation['types_released']} types, "
                  f"{evaluation['documents']} labelled documents")
        if csv_file:
            print(f"Output File:          {csv_file}")
        if threshold > 0:
            print(f"Count Threshold:      {threshold}")
        print("-" * 40)
        if evaluation:
            print(f"{'PII Type':<15} | {'Private':<8} | Recall")
        else:
            print(f"{'PII Type':<15} | {'Private':<8}")
        print("-" * 40)
        for key in private:
            display_count = _displayed_count(private[key], threshold)
            if evaluation:
                measured = evaluation["types"][key]
                recall = f"{measured['recall']:.2f}" if measured["evaluated"] else "not evaluated"
                print(f"{key:<15} | {display_count:<8} | {recall}")
            else:
                print(f"{key:<15} | {display_count:<8}")
        
        if csv_file:
            print("-" * 40)
            print(f"Results successfully written to {csv_file}")
            
        print("="*40 + "\n")

    def _interpret_epsilon(self, epsilon):
        if epsilon <= 0.1: return "EXTREME"
        if epsilon <= 1.0: return "HIGH"
        if epsilon <= 5.0: return "MODERATE"
        return "LOW (Testing Only)"

def _released_count(count, threshold):
    """A privatized count as it is released: never negative, and None below the threshold."""
    count = max(0, count)
    return None if count < threshold else count


def _displayed_count(count, threshold):
    released = _released_count(count, threshold)
    return "None" if released is None else released


def membership_risk(epsilon):
    """
    The most sure an attacker can become that one document is in the counts, if it was a coin toss
    to them before they saw the releases that spent epsilon: e^epsilon / (1 + e^epsilon). It follows
    from the bound differential privacy puts on how much one document changes the odds of any output.
    """
    return 1.0 / (1.0 + math.exp(-epsilon))


def _entity_key(name):
    # Philter Scope labels a type EMAIL_ADDRESS where Philter's counts say email-address.
    return str(name).strip().lower().replace("_", "-").replace(" ", "-")


def _rate(value, entity, field, file_path):
    if value is None and field == "precision":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"The {field} for {entity} in {file_path} must be a number from 0 to 1.")
    return float(value)


def load_evaluation(file_path, pii_types):
    """
    Reads a Philter Scope report.json and returns the evaluation section of the audit report: when the
    evaluation ran, how many documents it scored, a hash of the policy it evaluated, and the recall and
    precision of each type in pii_types. A type the evaluation has no entity for is marked as not
    evaluated rather than left out. Raises ValueError if the file is not a Philter Scope report.
    """
    with open(file_path, encoding="utf-8") as f:
        try:
            report = json.load(f)
        except ValueError:
            raise ValueError(f"{file_path} is not valid JSON.") from None
    stats = report.get("entity_stats") if isinstance(report, dict) else None
    recalls = report.get("entity_metrics") if isinstance(report, dict) else None
    if not isinstance(stats, dict) and not isinstance(recalls, dict):
        raise ValueError(f"{file_path} is not a Philter Scope report: it has neither entity_stats nor entity_metrics.")

    # entity_stats has both rates; reports written before it existed only have the recall.
    measured = {}
    for entity, recall in (recalls or {}).items():
        measured[_entity_key(entity)] = {"recall": _rate(recall, entity, "recall", file_path), "precision": None}
    for entity, stat in (stats or {}).items():
        if not isinstance(stat, dict):
            raise ValueError(f"The entity_stats entry for {entity} in {file_path} must be an object.")
        measured[_entity_key(entity)] = {"recall": _rate(stat.get("recall"), entity, "recall", file_path),
                                         "precision": _rate(stat.get("precision"), entity, "precision", file_path)}

    types = {}
    for pii_type in pii_types:
        rates = measured.get(_entity_key(pii_type))
        types[pii_type] = {"evaluated": True, **rates} if rates else {"evaluated": False}
    evaluated = sum(1 for rates in types.values() if rates["evaluated"])

    # Philter's counts don't say which policy produced them. The hash lets a reader compare the
    # evaluated policy with the deployed one.
    policy = report.get("policy")
    policy_sha256 = None
    if policy:
        canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        policy_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return {
        "evaluated_at": report.get("timestamp"),
        "documents": report.get("total_documents"),
        "policy_sha256": policy_sha256,
        "types_released": len(types),
        "types_evaluated": evaluated,
        "coverage": evaluated / len(types) if types else 0.0,
        "types": types,
    }


AUDIT_REPORT_LIMITS = [
    "Epsilon bounds what one document can change. A person who appears in several documents is protected less, in proportion.",
    "membership_risk starts the attacker at even odds. One who already suspects a document is there ends up more sure, though the releases still change the odds by at most e^epsilon.",
    "The evaluation measures the policy on a labelled sample, not on the documents in this release.",
    "Philter's counts do not record which policy produced them. Compare policy_sha256 with a hash of the deployed policy.",
]


def build_audit_report(engine, private, epsilon, scale, budget_ceiling, threshold=0, window=None, evaluation=None):
    """
    Returns the audit report for a release as a dict for --report. Like the printed report it holds no
    raw counts, only the privatized counts that are already in the CSV.
    """
    report = {
        "tool": "Philter Diffuse",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": engine.collection_name,
        "window": {"from": window[0], "to": window[1]} if window else None,
        "mechanism": {"name": "Discrete Laplace", "scale": scale, "types": len(private)},
        "epsilon": epsilon,
        "budget": None,
        "risk": None,
        "threshold": threshold,
        "counts": {key: _released_count(value, threshold) for key, value in private.items()},
        "evaluation": evaluation,
        "limits": AUDIT_REPORT_LIMITS,
    }
    if engine.spent_after_release:
        # With a window the busiest day is the bound: each document is recorded under one day, and
        # every release that covers that day adds to what its documents have lost.
        spent, day = engine.spent_after_release
        report["budget"] = {"spent": spent, "ceiling": budget_ceiling, "remaining": max(0.0, budget_ceiling - spent),
                            "day": day}
        report["risk"] = {"epsilon": spent, "membership_risk": membership_risk(spent)}
    return report


def write_audit_report(report, file_path):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


def restrict_to_declared_types(raw_counts, pii_types):
    """
    Returns (counts, left_out): a count for exactly the declared types, in the declared order, and
    how many other types the source held. A declared type the source lacks counts as 0.

    The released rows must not depend on the data. A row for each type found would show that at
    least one document held that type, however much noise its count received, and no epsilon
    bounds that disclosure.
    """
    counts = {pii_type: raw_counts.get(pii_type, 0) for pii_type in pii_types}
    return counts, len(set(raw_counts) - set(pii_types))


def _pii_types(value):
    """
    An argparse type for a comma-separated list of PII types. Repeats are dropped, since they would
    otherwise be charged for a row that is released once.
    """
    pii_types = list(dict.fromkeys(name.strip() for name in value.split(",")))
    if not all(pii_types):
        raise argparse.ArgumentTypeError(f"'{value}' is not a comma-separated list of PII types, such as ssn,email-address.")
    return pii_types


def _scale(value):
    """
    An argparse type for a positive, finite number.
    """
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"'{value}' is not a positive number.")
    return number


def _ceiling(value):
    """
    An argparse type for a finite number of 0 or more.
    """
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number of 0 or more.")
    return number


def _share(value):
    """
    An argparse type for a share from 0 to 1.
    """
    number = float(value)
    if not 0 <= number <= 1:
        raise argparse.ArgumentTypeError(f"'{value}' is not a share from 0 to 1, such as 0.8.")
    return number


def _threshold(value):
    """
    An argparse type for a whole number of 0 or more.
    """
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"'{value}' is not a whole number of 0 or more.")
    return number


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
    parser.add_argument("--source", type=str, default=None, metavar="NAME", help="With --input, a name for the dataset the counts come from. The privacy budget is tracked under this name, so keep it the same for every release from that dataset.")
    parser.add_argument("--mongo-uri", type=str, default=None, help="MongoDB connection URI (format: mongodb://host:port/database).")
    parser.add_argument("--pii-types", type=_pii_types, default=None, metavar="TYPE[,TYPE...]", help="Required for a release. The PII types to release, comma-separated, such as ssn,email-address. Output has one row per type whether or not the source holds it, and types not listed are left out. Each type costs 1/scale of epsilon. Choose the list without looking at the counts.")
    parser.add_argument("--scale", type=_scale, default=None, help="Scale for Laplace noise (higher = more privacy, less accuracy). Defaults to 2.0.")
    parser.add_argument("--output", type=str, default=None, help="Required for a release. Path to write the privatized PII counts to a CSV file.")
    parser.add_argument("--threshold", type=_threshold, default=None, help="Counts below this threshold will be output as 'None'.")
    parser.add_argument("--budget-ceiling", type=_ceiling, default=10.0, help="The maximum total epsilon allowed per source, and with --philter-url per UTC day.")
    parser.add_argument("--budget-warning-share", type=_share, default=None, metavar="SHARE", help=f"Print a warning on standard error when a release leaves a source, or with --philter-url a day, past this share of --budget-ceiling, from 0 to 1. Defaults to {DEFAULT_WARNING_SHARE}.")
    parser.add_argument("--budget-file", type=str, default=None, metavar="PATH", help=f"Where to keep the privacy budget, for sources other than MongoDB. Defaults to the {BUDGET_FILE_ENV} environment variable, then {DEFAULT_BUDGET_FILE}.")
    parser.add_argument("--budget-status", action="store_true", help="Print the epsilon spent and remaining for each source in the budget file, or with --mongo-uri in MongoDB, against --budget-ceiling, and exit. It reads no counts, makes no request to Philter, and spends nothing.")
    parser.add_argument("--aggregates", action="store_true", help="Read Philter's aggregated, document-presence PII counts instead of counting documents per field. With --philter-url they are read through Philter's API; otherwise from the pii_count_aggregates collection (deprecated).")
    parser.add_argument("--context", type=str, default=None, help="When reading aggregates, restrict to a single Philter context.")
    parser.add_argument("--philter-url", type=str, default=None, metavar="URL", help=f"Read the aggregates through Philter's API (Philter 4.0 or later) at this URL, such as https://localhost:8080, instead of from MongoDB. Implies --aggregates. The API key is read from the {PHILTER_API_KEY_ENV} environment variable.")
    parser.add_argument("--philter-ca-cert", type=str, default=None, metavar="PEM_FILE", help="With --philter-url, a PEM file holding a certificate to trust in addition to the system's certificate authorities, such as Philter's self-signed certificate.")
    parser.add_argument("--from", dest="date_from", type=_day, default=None, metavar="YYYY-MM-DD", help="Required with --philter-url. Read only counts for this UTC day or later. The release is charged to the privacy budget of every day from --from to --to.")
    parser.add_argument("--to", dest="date_to", type=_day, default=None, metavar="YYYY-MM-DD", help="Required with --philter-url. Read only counts for this UTC day or earlier.")
    parser.add_argument("--report", type=str, default=None, metavar="PATH", help="Also write the audit report as JSON to this path: epsilon, the budget spent with this release, the membership risk, the privatized counts, and with --evaluation the policy's recall. Like standard output, it holds no raw counts.")
    parser.add_argument("--evaluation", type=str, default=None, metavar="PATH", help="A Philter Scope report.json for the policy that produced the counts. Its recall and precision for each type in --pii-types are added to the audit report. It does not change the noise or the budget.")

    args = parser.parse_args(argv)

    if args.budget_status:
        for flag, value in (("--input", args.input), ("--source", args.source), ("--philter-url", args.philter_url),
                            ("--philter-ca-cert", args.philter_ca_cert), ("--pii-types", args.pii_types),
                            ("--output", args.output), ("--scale", args.scale), ("--threshold", args.threshold),
                            ("--budget-warning-share", args.budget_warning_share), ("--aggregates", args.aggregates),
                            ("--context", args.context), ("--from", args.date_from), ("--to", args.date_to),
                            ("--report", args.report), ("--evaluation", args.evaluation)):
            if value is not None and value is not False:
                parser.error(f"--budget-status only reads the budget and cannot be used with {flag}.")
        if args.budget_file and args.mongo_uri:
            parser.error("--budget-file cannot be used with --mongo-uri, which keeps the budget in MongoDB.")
        try:
            engine = PhilterDiffuse(args.mongo_uri, budget_file=args.budget_file)
            for line in engine.budget_status(args.budget_ceiling):
                print(line)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    missing = [flag for flag, value in (("--pii-types", args.pii_types), ("--output", args.output)) if value is None]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    if args.scale is None:
        args.scale = 2.0
    if args.threshold is None:
        args.threshold = 0
    if args.budget_warning_share is None:
        args.budget_warning_share = DEFAULT_WARNING_SHARE

    if sum(1 for source in (args.input, args.philter_url, args.mongo_uri) if source) > 1:
        parser.error("--input, --philter-url, and --mongo-uri cannot be used together.")
    if args.philter_url:
        args.aggregates = True
    else:
        if args.date_from or args.date_to:
            parser.error("--from and --to require --philter-url.")
        if args.philter_ca_cert:
            parser.error("--philter-ca-cert requires --philter-url.")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--from must not be after --to.")
    if args.aggregates and args.input:
        parser.error("--aggregates requires --philter-url or --mongo-uri.")
    if args.context is not None and not args.aggregates:
        parser.error("--context requires --philter-url, or --mongo-uri with --aggregates.")
    if args.input and not args.source:
        parser.error("--input requires --source, the name the privacy budget is tracked under.")
    if args.source and not args.input:
        parser.error("--source requires --input.")
    if args.budget_file and args.mongo_uri:
        parser.error("--budget-file cannot be used with --mongo-uri, which keeps the budget in MongoDB.")
    if not (args.input or args.philter_url or args.mongo_uri):
        parser.error("One of --input, --philter-url, or --mongo-uri is required.")
    if args.philter_url and not (args.date_from and args.date_to):
        parser.error("--philter-url requires --from and --to. The release is charged to the privacy budget of "
                     "each UTC day from one to the other.")

    try:
        if args.input:
            # The budget is tracked under a declared name, so copying or renaming the file does not
            # start a fresh budget.
            engine = PhilterDiffuse(collection_name=args.source, budget_file=args.budget_file)
            print(f"Loading counts from {args.input}...")
            raw_data = engine.load_counts_from_json(args.input)
        elif args.philter_url:
            # Read through Philter's API. No MongoDB connection is made: the budget is tracked per
            # UTC day in the local budget file, keyed by the Philter deployment the counts came from.
            try:
                client = PhilterClient(args.philter_url, os.environ.get(PHILTER_API_KEY_ENV),
                                       ca_cert=args.philter_ca_cert)
                engine = PhilterDiffuse(collection_name=f"pii_count_aggregates@{client.base_url}", budget_file=args.budget_file)
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
                    raw_data = engine.fetch_pii_counts(args.pii_types)
            except Exception as e:
                print(f"Error: MongoDB connection failed: {e}")
                sys.exit(1)

        raw_data, left_out = restrict_to_declared_types(raw_data, args.pii_types)
        if left_out:
            # A count, not names: this output may be kept where the names would disclose something.
            print(f"WARNING: {left_out} PII type(s) in the source are not in --pii-types and were left out of this release.", file=sys.stderr)

        # Read before anything is charged, so a bad file costs no budget.
        evaluation = load_evaluation(args.evaluation, args.pii_types) if args.evaluation else None

        # 2. Privatize
        window = (args.date_from, args.date_to) if args.philter_url else None
        private_data, spent_epsilon = engine.privatize_counts(raw_data, scale=args.scale,
                                                              budget_ceiling=args.budget_ceiling, window=window)
        spent, day = engine.spent_after_release

        # 3. Output to CSV
        engine.write_counts_to_csv(private_data, args.output, threshold=args.threshold)
        
        # 4. Report
        if args.report:
            write_audit_report(build_audit_report(engine, private_data, spent_epsilon, args.scale, args.budget_ceiling,
                                                  threshold=args.threshold, window=window, evaluation=evaluation),
                               args.report)
        engine.generate_audit_report(private_data, spent_epsilon, csv_file=args.output, threshold=args.threshold,
                                     spent=spent, budget_ceiling=args.budget_ceiling, day=day, evaluation=evaluation)
        if args.report:
            print(f"Audit report written to {args.report}")
        if spent > args.budget_warning_share * args.budget_ceiling:
            spender = f"{day} has spent {spent:g} of its budget ceiling of {args.budget_ceiling:g} at {engine.collection_name}" \
                if day else f"{engine.collection_name} has spent {spent:g} of its budget ceiling of {args.budget_ceiling:g}"
            print(f"WARNING: {spender}, past the warning share of {args.budget_warning_share * 100:g}%.", file=sys.stderr)

    except SystemExit:
        # Propagate the exit status so a failed run does not look like a successful one.
        raise
    except BudgetExceeded as e:
        # Exiting with an error and writing nothing keeps a refusal from reading as a count of zero.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
