# Changelog

All notable changes to Philter Diffuse are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Philter Diffuse follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). As a `0.x` project, its command-line interface and output format may change before `1.0.0`.

## Unreleased

### Privacy budget

- With `--philter-url`, the privacy budget is kept per UTC day. A release is charged to every day from `--from` to `--to`, and `--budget-ceiling` applies to each day, so releases over separate periods each have the full ceiling. Days that two releases share are charged by both. A refused release names the day and the epsilon it has spent. ([#1](https://github.com/philterd/philter-diffuse/issues/1))
- `--philter-url` now requires `--from` and `--to`.
- A budget file entry from `0.1.0` is read as already spent on every day, so upgrading gives no release more budget than it had before.
- `--budget-status` prints the epsilon spent and remaining for each source in the budget file, or with `--mongo-uri` in MongoDB, against `--budget-ceiling`, and per UTC day for a Philter deployment. It reads no counts, makes no request to Philter, needs no `--pii-types` or `--output`, and changes nothing. ([#2](https://github.com/philterd/philter-diffuse/issues/2))
- The audit report gives the budget spent and remaining after the release. With `--philter-url`, they are for the busiest day of the window, which the report names.
- A release that leaves its source, or with `--philter-url` a day, past a share of `--budget-ceiling` prints a warning on standard error. The share is 0.8 unless `--budget-warning-share` sets another.
- Each release adds one entry to a release log next to the budget: `privacy_budget.releases.jsonl` beside the budget file, or the `privacy_releases` collection in MongoDB. An entry gives the time, the source, the epsilon, the scale, the declared types, and the window when there is one, and never a count. It is written right after the charge, under the same lock, and a refused release adds none.

### Audit report

- `--report PATH` writes the audit report as JSON: epsilon, the scale and number of types, the budget spent with this release and what remains, a membership risk, the threshold, and the privatized counts that are in the CSV. Like standard output, it holds no raw counts. ([#7](https://github.com/philterd/philter-diffuse/issues/7))
- The membership risk is `e^ε / (1 + e^ε)` for the epsilon the source, or with `--philter-url` the busiest day in the window, has spent: the most sure an attacker who started at even odds can become that one document is in the counts. The printed report shows it after the budget.
- `--evaluation PATH` reads a Philter Scope `report.json` and adds when the evaluation ran, how many documents it scored, a SHA-256 of the policy it evaluated, and the recall and precision of each released type. A released type the evaluation didn't cover is marked as not evaluated. A file that isn't a Philter Scope report stops the run before any budget is spent.

## 0.1.0 - 2026-11-04

Initial release.

### Differential privacy

- PII counts are released with the Discrete Laplace mechanism from [OpenDP](https://github.com/opendp/opendp), with noise set by `--scale`. Counts are 64-bit, and negative results are clamped to 0.
- `--pii-types` declares the types to release. The output has exactly those rows whether or not the source holds each type, so the rows do not reveal which types the data holds. Types not listed are left out, with a warning on standard error that gives how many.
- Each release spends `n / scale` epsilon for `n` declared types, because one document can add to every count at once.
- The bound applies to one document. A person who appears in many documents is protected less, in proportion.
- `--threshold` masks a privatized count below the given value as `None`.

### Reading counts

- `--philter-url` reads the PII counts Philter records through `GET /api/pii-counts` (Philter 4.0 or later), every page, filtered by `--context`, `--from`, and `--to`. The API key is read from the `PHILTER_API_KEY` environment variable and is never printed. Certificates are verified, `--philter-ca-cert` adds a trusted certificate, and redirects are not followed. If Philter reports that recording is off, the run stops without output or charge.
- `--input` reads a JSON object of PII types and whole counts of 0 or more. Any other value stops the run.
- `--mongo-uri` counts the documents holding each declared field. With `--aggregates` it reads Philter's `pii_count_aggregates` collection; that path is deprecated in favor of `--philter-url` and will be removed.
- A run names exactly one source, and a flag that does not apply to that source is refused.

### Privacy budget

- Spent epsilon is tracked per source against `--budget-ceiling`, in MongoDB's `privacy_metadata` collection for MongoDB sources and otherwise in a budget file. The file is `~/.philter-diffuse/privacy_budget.json` unless `--budget-file` or `PHILTER_DIFFUSE_BUDGET_FILE` names another.
- JSON input is tracked under its `--source` name, so a renamed or copied file keeps its budget.
- The budget is charged before noise is sampled, as one step with the ceiling check, so concurrent runs cannot each spend the same balance. On Windows, concurrent runs against one budget file are not serialized.
- A release that would pass the ceiling exits with code `1`, writes no output, leaves any existing `--output` file unchanged, and spends no budget.

### Output

- Privatized counts are written to the CSV named by `--output`.
- An audit report on standard output gives the epsilon spent, a qualitative strength, and the private counts. It never prints raw counts.

### Docker

- A `Dockerfile` that runs as an unprivileged user (UID 1000), works in `/data`, and keeps the budget there. Mount `/data` so the budget outlives the container.
