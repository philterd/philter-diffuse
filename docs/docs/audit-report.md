# Audit Report

Every release prints an audit report to standard output. With `--report PATH` the same report is also written as JSON, to keep next to the CSV when a release is signed off. Like standard output, the JSON holds no raw counts.

```shell
python main.py --philter-url https://localhost:8080 --from 2026-09-01 --to 2026-09-30 \
  --pii-types ssn,email-address,person --output release.csv \
  --report release-audit.json --evaluation philter-scope/report.json
```

## Contents

| Field | Meaning |
| :--- | :--- |
| `version`, `generated_at` | The Philter Diffuse version and when the release ran (UTC). |
| `source` | The name the privacy budget is tracked under: `--source`, the MongoDB collection, or the Philter URL. |
| `window` | With `--philter-url`, the `from` and `to` UTC days the counts cover. `null` otherwise. |
| `mechanism` | `Discrete Laplace`, its `scale`, and the number of `types` released. |
| `epsilon` | What this release spent: the number of types divided by the scale. |
| `budget` | After this release: the epsilon `spent`, the `ceiling`, and what `remaining`. With a window, `day` is the busiest day in it, and `spent` is what that day has spent. |
| `risk` | The `epsilon` in `budget.spent` and its `membership_risk`. See below. |
| `threshold` | `--threshold`. Counts below it are released as `null` (`None` in the CSV). |
| `counts` | The privatized counts, as in the CSV. |
| `evaluation` | With `--evaluation`, the policy's measured recall and precision. `null` otherwise. |
| `limits` | What the numbers above don't cover, in plain sentences. |

## Membership risk

Differential privacy bounds how much one document can change the probability of any output: by a factor of at most `e^ε`. So an attacker who thought it even odds that a given document is in the counts can be at most `e^ε / (1 + e^ε)` sure after seeing them. That is the `membership_risk`.

It is computed for the epsilon the budget has spent, not just this release, because releases charged to the same budget add up. With `--philter-url` that is the busiest day in the window, since each document is recorded under one day.

| Epsilon spent | Membership risk |
| :--- | :--- |
| 0.5 | 62.2% |
| 1 | 73.1% |
| 2 | 88.1% |
| 5 | 99.3% |

The bound is per document. A person who appears in three documents can be up to three times as exposed, so read the risk for `3 × ε` for them.

## Policy evaluation

Philter only counts what its policy detects, so a type with low recall is undercounted before any noise is added. `--evaluation` reads the `report.json` that [Philter Scope](https://github.com/philterd/philter-scope) writes when it scores a policy on a labelled set, and adds:

- `evaluated_at` and `documents`: when the evaluation ran and how many documents it scored.
- `policy_sha256`: a SHA-256 of the evaluated policy, as compact JSON with sorted keys.
- `types`: for each released type, `recall` and `precision`, or `"evaluated": false` if the evaluation had no entity for it. Names match without regard to case, `_`, or `-`, so `EMAIL_ADDRESS` matches `email-address`. Types named differently in the two tools show as not evaluated (philterd/philter-scope#18).
- `types_evaluated`, `types_released`, and `coverage`, the share of released types that were evaluated.

Philter's counts don't record which policy produced them, so the tool can't check that the evaluated policy is the one that ran. To compare, hash the deployed policy the same way:

```shell
python -c "import hashlib,json,sys; print(hashlib.sha256(json.dumps(json.load(open(sys.argv[1])), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest())" policy.json
```

The evaluation never changes the noise, the scale, or the budget. A file that isn't a Philter Scope report stops the run before anything is charged.

## Using the report as evidence for an Expert Determination

Under HIPAA's Expert Determination method (45 CFR 164.514(b)(1)), a qualified expert determines that the risk that released information could identify a person is very small, and documents the methods and results that justify it. The audit report doesn't make that determination. It records what the expert needs for each release:

- the method and its parameters (`mechanism`, `epsilon`),
- the privacy loss accumulated on the same budget and the resulting `membership_risk`,
- the suppression `threshold`,
- how well the policy finds each released type (`evaluation`), and which policy was measured.

Keep, for each release: the CSV, the `--report` JSON, the Philter Scope `report.json` it cites, and the policy that was deployed.

The report's contents were chosen with NIST SP 800-188 (*De-Identifying Government Datasets: Techniques and Governance*) and ISO/IEC 27559 (*Privacy enhancing data de-identification framework*) as design references: both ask for the technique, its parameters, and the re-identification risk to be documented. Using them as references is not a claim that a release conforms to either.

## What the report does not measure

- **k-anonymity and l-diversity.** Both describe groups of records that share quasi-identifiers in a record-level table. A release here is a handful of aggregate counts with no records to group, so neither applies; epsilon bounds the risk for this kind of release instead. `--threshold` suppresses small counts, but it is applied to the noisy counts, so it is not a k-anonymity guarantee.
- **The documents in this release.** The evaluation measures the policy on a labelled sample.
- **Which policy produced Philter's counts.** Compare `policy_sha256` with the deployed policy.
