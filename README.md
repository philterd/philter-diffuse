# Philter Diffuse

Philter Diffuse provides mathematical guarantees for PII count aggregations using [OpenDP](https://github.com/opendp/opendp).

It uses the Discrete Laplace mechanism to privatize PII counts and generates compliance-ready reports with epsilon (privacy loss) interpretation. The tool also features persistent privacy budget tracking to prevent privacy loss exhaustion over time.

Designed to be used with [Phileas](https://www.github.com/philterd/phileas) and [Philter](https://www.github.com/philterd/philter).

View the documentation at https://philterd.github.io/philterdiffuse. New users should start with the [Quickstart](https://philterd.github.io/philterdiffuse/quickstart/).

## Project Status

Philter Diffuse is early-access software at version `0.1.0`. The differential-privacy mechanism, budget accounting, and CLI are usable and tested, but as a `0.x` release the interface may change before a stable `1.0`. See [Project Status](https://philterd.github.io/philterdiffuse/status/) for the maturity checklist and roadmap. Check your checkout with `python main.py --version`.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

1. **Option A: MongoDB**
   Ensure MongoDB is running and run:
   ```bash
   python main.py --mongo-uri "mongodb://localhost:27017/analytics_db" --output privatized_counts.csv
   ```

2. **Option B: JSON file**
   Provide a JSON document with PII entity counts:
   ```bash
   python main.py --input pii_counts.json --output privatized_counts.csv
   ```
   Sample `pii_counts.json`:
   ```json
   {
     "ssn": 10,
     "age": 45,
     "zipcode": 12,
     "creditcard": 5
   }
   ```

3. **Export to CSV**
   You can export privatized counts to a CSV file:
   ```bash
   python main.py --input pii_counts.json --output privatized_counts.csv
   ```

4. **Using Docker**
   You can also run the tool using Docker:
   ```bash
   ./run-docker.sh
   ```
   This script builds the image and runs a sample privatization task using volume mounts.

## Privacy Budget Management

Philter Diffuse tracks the total epsilon spent per collection (or JSON source) in a persistent metadata store. When using MongoDB, it is stored in the `privacy_metadata` collection. Otherwise (JSON input, or reading counts through Philter's API), it is stored in a local file named `privacy_budget.json`.

If the `--budget-ceiling` is reached, the tool will notify the user, apply infinite noise (effectively zeroing out or thresholding the results), and refuse to release further accurate information to protect privacy.

## CLI Options

| Option | Description | Required? | Default |
|--------|-------------|-----------|---------|
| `--input` | Path to a JSON document containing PII entity counts. | Optional | None |
| `--mongo-uri` | MongoDB connection URI (format: `mongodb://host:port/database`). | Optional | `mongodb://localhost:27017/philter` |
| `--scale` | Scale for Laplace noise (higher = more privacy). | Optional | `2.0` |
| `--output` | Path to write the privatized PII counts to a CSV file. | **Required** | None |
| `--threshold` | Counts below this threshold will be output as `None`. | Optional | `0` |
| `--budget-ceiling` | The maximum total epsilon allowed per collection. | Optional | `10.0` |
| `--aggregates` | Read [Philter's](https://www.github.com/philterd/philter) aggregated, document-presence PII counts (summing the count buckets that Philter records per user's `(context, day)`) instead of counting documents per field. With `--philter-url` they are read through Philter's API; otherwise from the `pii_count_aggregates` collection in MongoDB (deprecated). | Optional | off |
| `--context` | When using `--aggregates`, restrict to Philter contexts of this name. Through Philter's API, contexts of that name belong to the API key's user unless Philter's `ADMIN_CROSS_USER_ACCESS_ENABLED` is on, when they include every user's. | Optional | all contexts |
| `--philter-url` | Read the aggregates through Philter's API (Philter 4.0 or later) at this URL, such as `https://localhost:8080`. Implies `--aggregates`. The API key is read from the `PHILTER_API_KEY` environment variable. | Optional | None |
| `--philter-ca-cert` | With `--philter-url`, a PEM file holding a certificate to trust in addition to the system's certificate authorities, such as Philter's self-signed certificate. | Optional | None |
| `--from` | With `--philter-url`, read only counts for this UTC day (`YYYY-MM-DD`) or later. | Optional | None |
| `--to` | With `--philter-url`, read only counts for this UTC day (`YYYY-MM-DD`) or earlier. | Optional | None |

### Reading Philter's PII counts

When Philter is configured to record PII count statistics (its `diffuseCountsEnabled` admin setting, off by default), it records document-presence counts, one entry per user's `(context, day)`. These are document-presence counts (each redaction contributes at most one per type), which matches the differential-privacy `sensitivity = 1` assumption.

#### Through Philter's API (recommended, Philter 4.0 or later)

Philter 4.0 serves the counts at `GET /api/pii-counts`, so Philter Diffuse needs only an API key, not access to Philter's database. Put the key in the `PHILTER_API_KEY` environment variable (it is never taken as a command-line argument, so it stays out of shell history and process listings, and it is never printed) and pass Philter's URL:

```bash
export PHILTER_API_KEY=<api key>
python main.py --philter-url https://localhost:8080 --aggregates --output privatized_counts.csv
```

* The key must hold the `pii-counts:read` scope, and its user must be a Philter **administrator**, because the counts cover the whole deployment. Philter records each read in its audit log.
* Narrow the counts with `--context <name>` and with `--from` and `--to` (UTC days, `YYYY-MM-DD`, both inclusive). Philter applies these filters, and Philter Diffuse reads every page of the result. Philter keeps each user's counts apart; without `--context`, Philter Diffuse sums every user's. Through Philter's API, contexts of that name belong to the API key's user unless Philter's `ADMIN_CROSS_USER_ACCESS_ENABLED` is on, when they include every user's.
* If Philter reports that count recording is turned off, Philter Diffuse says so and exits without writing privatized output or spending privacy budget, rather than privatizing an empty or partial result as if no PII had been found.
* If Philter refuses the request (missing scope, not an administrator, invalid key) or cannot be reached, Philter Diffuse prints the reason and Philter's request ID when one is returned, so the request can be found in Philter's audit log.
* Certificates are verified. Philter's Docker image generates a self-signed certificate; to trust it, export it and pass it with `--philter-ca-cert philter.pem` rather than turning verification off. Redirects are not followed, so the API key is only ever sent to the URL you give.
* The privacy budget for API reads is tracked in the local `privacy_budget.json`, keyed by the Philter URL (`pii_count_aggregates@https://localhost:8080`). No MongoDB connection is made. Moving from the MongoDB path starts a new budget entry: copy the epsilon already spent (in MongoDB's `privacy_metadata` collection) into it so earlier releases still count against the ceiling.

#### From MongoDB (deprecated)

Philter Diffuse can still read the `pii_count_aggregates` collection directly from Philter's MongoDB:

```bash
python main.py --mongo-uri "mongodb://localhost:27017/philter" --aggregates --output privatized_counts.csv
```

This path is deprecated and prints a warning naming `--philter-url`. It depends on Philter's internal storage layout and on MongoDB storage, and it will be removed in a future release. Move to `--philter-url` once you run Philter 4.0 or later.

## Community

Users and developers are invited to join the [Philterd Slack workspace](https://philterd.ai/slack/) to ask questions, share feedback, and discuss Philter Diffuse and the rest of the Philterd toolkit.

## License

Copyright 2026 Philterd, LLC. "Philterd", "Phileas", and "Philter" are registered trademarks of Philterd, LLC.

This project is licensed under the Apache License, Version 2.0. See the `LICENSE` file for details.
