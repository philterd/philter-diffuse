# Usage

Philter Diffuse is a command-line tool designed to process PII counts and apply differential privacy. It reads counts from a MongoDB database, a local JSON file, or Philter's API.

## Input Modes

### 1. MongoDB Mode (Default)

In this mode, Philter Diffuse connects to a MongoDB instance, counts the documents in the `pii_counts` collection of the database named in the URI that have each of the monitored fields, and privatizes those counts. This mode is not tied to Philter; to read Philter's counts, see [Reading Philter's PII counts](#3-reading-philters-pii-counts-aggregates).

**Example Command:**
```bash
python main.py --mongo-uri "mongodb://localhost:27017/analytics_db" --output results.csv
```

*   **Behavior**: It connects to the `analytics_db` on `localhost`.
*   **Monitored Fields**: The tool counts documents containing each of these fields: `creditcard`, `ssn`, `age`, and `zipcode`. The fields and the collection are fixed.

### 2. JSON Mode

If you already have a JSON file containing the counts of various PII types, you can provide it directly using the `--input` flag. This is ideal for integration with existing logging or data pipeline tools.

**Example Command:**
```bash
python main.py --input data/pii_counts.json --output results.csv
```

**Expected JSON Format:**
The JSON file should be a simple flat object where keys represent the PII type and values are the integer counts.
```json
{
  "ssn": 105,
  "age": 450,
  "zipcode": 122,
  "creditcard": 58,
  "email": 210
}
```

## Command Line Options

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--input` | Path to a local JSON file containing PII counts. If omitted, MongoDB mode is used. | `None` |
| `--mongo-uri` | The connection string for your MongoDB instance. | `mongodb://localhost:27017/philter` |
| `--scale` | The **scale** parameter for the Laplace noise. A higher scale adds more noise (more privacy, less accuracy). | `2.0` |
| `--output` | **[Required]** The file path where the privatized CSV results will be saved. | `None` |
| `--threshold` | Results with a privatized count below this value will be masked as `None` in the output. | `0` |
| `--budget-ceiling` | The maximum cumulative privacy budget (epsilon) allowed for a single source. | `10.0` |
| `--aggregates` | Read Philter's aggregated, document-presence PII counts instead of counting documents per field. With `--philter-url` they are read through Philter's API; otherwise from the `pii_count_aggregates` collection in MongoDB (deprecated). | off |
| `--context` | When using `--aggregates`, restrict to Philter contexts of this name. Through Philter's API, contexts of that name belong to the API key's user unless Philter's `ADMIN_CROSS_USER_ACCESS_ENABLED` is on, when they include every user's. | all contexts |
| `--philter-url` | Read the aggregates through Philter's API (Philter 4.0 or later) at this URL, such as `https://localhost:8080`. Implies `--aggregates`. The API key is read from the `PHILTER_API_KEY` environment variable. | `None` |
| `--philter-ca-cert` | With `--philter-url`, a PEM file holding a certificate to trust in addition to the system's certificate authorities, such as Philter's self-signed certificate. | `None` |
| `--from` | With `--philter-url`, read only counts for this UTC day (`YYYY-MM-DD`) or later. | `None` |
| `--to` | With `--philter-url`, read only counts for this UTC day (`YYYY-MM-DD`) or earlier. | `None` |
| `--version` | Print the Philter Diffuse version and exit. | |

Run `python main.py --help` to see this list from the tool itself.

### 3. Reading Philter's PII counts (`--aggregates`)

When Philter is configured to record PII count statistics (its `diffuseCountsEnabled` admin setting, off by default), it records document-presence counts: one entry per user's `(context, day)`, each holding a map of `{pii_type: number-of-documents-containing-it}`. Instead of counting documents field by field, point Philter Diffuse at those counts directly. Philter Diffuse sums the entries into a single `{pii_type: total}` map and privatizes that. Because each redaction contributes at most one to any type's count (document presence), summing across entries keeps each count's sensitivity at 1. A document can contain several types, so a query's epsilon is the number of types released divided by `--scale`.

#### Through Philter's API (recommended)

Philter 4.0 and later serve the counts at `GET /api/pii-counts`. Reading them there needs only a Philter API key: no network access to or credentials for Philter's database, and no dependence on how Philter stores the counts.

```bash
export PHILTER_API_KEY=<api key>
python main.py --philter-url https://localhost:8080 --aggregates --output privatized_counts.csv
```

`--philter-url` implies `--aggregates`. Narrow the counts by context and by UTC day (both dates inclusive):

```bash
python main.py --philter-url https://localhost:8080 --aggregates \
  --context claims-intake --from 2026-09-01 --to 2026-09-30 --output privatized_counts.csv
```

Philter applies these filters itself, and Philter Diffuse reads every page of the result before summing.

**The API key.** Philter Diffuse reads the key only from the `PHILTER_API_KEY` environment variable, never from a command-line argument, so it does not appear in shell history or process listings. It never prints the key. Because the counts cover the whole Philter deployment, the key must hold the `pii-counts:read` scope **and** its user must be a Philter administrator. Philter records every read in its audit log.

**When recording is off.** If Philter reports that count recording is turned off, Philter Diffuse says so and exits with status `1` without writing privatized output or spending privacy budget. An empty result in that state does not mean Philter found no PII, so it is not privatized as if it did. An administrator turns recording on with Philter's Settings API (`{"diffuseCountsEnabled": true}`).

**When Philter refuses.** A refusal is reported with Philter's HTTP status and `reason` code, a hint for the common ones, and Philter's request ID (from its `X-Request-Id` header) so the request can be found in Philter's audit log:

```text
Error: Philter refused the request (HTTP 403, missing_scope): This API key does not have the 'pii-counts:read' scope. The API key needs the pii-counts:read scope. (Philter request ID: 01929f2e-6c3a-7b1d-9e4f-3a2b1c0d9e8f)
```

| `reason` | What to do |
| :--- | :--- |
| `invalid_credentials` | `PHILTER_API_KEY` is missing, mistyped, or revoked. |
| `missing_scope` | Give the key the `pii-counts:read` scope. |
| `admin_required` | Use a key whose user is a Philter administrator. |
| HTTP 404 | Philter is older than 4.0, or `--philter-url` points somewhere else. |
| HTTP 400 with no `reason` | With an `http://` URL, Philter is most likely serving HTTPS on that port. Use `https://`. |

**TLS.** Certificates are verified, and verification cannot be turned off. Philter's Docker image generates a self-signed certificate (with `localhost`, `philter`, and `127.0.0.1` in its subject alternative name) the first time it starts. To trust it, export it as PEM (the keystore password is `SSL_KEYSTORE_PASSWORD`, `philter` by default) and pass it with `--philter-ca-cert`:

```bash
docker cp <philter-container>:/opt/philter/ssl/philter.p12 .
openssl pkcs12 -in philter.p12 -clcerts -nokeys -passin pass:philter -out philter.pem
python main.py --philter-url https://localhost:8080 --philter-ca-cert philter.pem --output privatized_counts.csv
```

`--philter-ca-cert` adds to the system's certificate authorities, so it also takes a private CA's certificate. Where TLS is terminated in front of Philter and Philter serves plain HTTP, use an `http://` URL only over a private network: the API key travels in every request. Redirects are not followed, so the key is only ever sent to the URL you give.

**Privacy budget.** The budget for API reads is tracked in the local `privacy_budget.json`, keyed by the Philter URL, not in Philter's database. Moving from the MongoDB path starts a new budget entry, so carry the epsilon already spent over to it; see [Privacy Budget Management](privacy-budget.md).

#### From MongoDB (deprecated)

Philter Diffuse can still read the `pii_count_aggregates` collection from Philter's MongoDB:

```bash
python main.py --mongo-uri "mongodb://localhost:27017/philter" --aggregates --output privatized_counts.csv
```

Restrict to one Philter context with `--context`. This path is **deprecated**: it prints a warning naming `--philter-url`, and it will be removed in a future release. It needs network access and credentials for Philter's database, works only when Philter uses MongoDB storage, and depends on Philter's internal collection layout. Move to `--philter-url` once you run Philter 4.0 or later. `--from` and `--to` are only available with `--philter-url`.

---

## Output Formats

Philter Diffuse provides two forms of output for every execution:

### 1. Privatized CSV File
The file specified by the `--output` flag will contain a list of PII types and their corresponding privatized counts. For the JSON example above, run with `--threshold 60`:

```bash
python main.py --input data/pii_counts.json --output results.csv --threshold 60
```

**Example CSV Content:**
```csv
PII Type,Private Count
ssn,108
age,451
zipcode,122
creditcard,None
email,213
```
*Note: `creditcard` is "None" because its privatized count fell below the threshold of 60.*

### 2. Privacy Audit Report
A human-readable report is printed to the standard output (`stdout`) summarizing the execution and the privacy guarantees applied.

**Key Report Sections:**
*   **Privacy Loss (Epsilon)**: The specific amount of privacy budget consumed by this particular execution (the number of PII types released divided by `--scale`).
*   **Protection Strength**: A qualitative assessment of the privacy level (e.g., "HIGH" or "MODERATE").
*   **Influence Limit**: A mathematical multiplier representing the maximum change an individual record can have on the output probability.
*   **Raw vs. Private Table**: A side-by-side comparison for internal auditing. It includes the raw counts, so treat the report as confidential: it goes wherever standard output goes, such as container or CI logs. Share only the CSV.

## Practical Example: Increasing Privacy

If you are dealing with extremely sensitive data and want to increase the privacy protection, increase the `--scale` parameter:

```bash
python main.py --input pii_counts.json --output private.csv --scale 5.0 --threshold 10
```

In this example:
1.  **Scale 5.0**: Adds significantly more noise than the default (2.0), resulting in a lower epsilon: 0.2 per PII type, so 1.0 for the five types in the JSON example above.
2.  **Threshold 10**: Any PII count that, after adding noise, is less than 10 will be completely masked as `None`.
