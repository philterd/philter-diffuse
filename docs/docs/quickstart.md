# Quickstart

This walkthrough takes you from a clean checkout to a privatized CSV and a privacy audit report in about five minutes. It uses JSON mode, so you do not need MongoDB.

## 1. Install

```bash
git clone https://github.com/philterd/philterdiffuse.git
cd philterdiffuse
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Confirm the tool runs:

```bash
python main.py --version
```

## 2. Create some input counts

Philter Diffuse privatizes counts of PII entities. These usually come from your Philter or Phield telemetry, but for this quickstart create a small JSON file by hand. Save the following as `pii_counts.json`:

```json
{
  "ssn": 8,
  "creditcard": 15,
  "age": 120,
  "zipcode": 45,
  "date": 3
}
```

The keys are PII types and the values are raw counts (for example, 8 documents contained an SSN).

## 3. Run the privatization

```bash
python main.py \
  --input pii_counts.json \
  --output privatized_counts.csv \
  --scale 2.0 \
  --threshold 5
```

What each flag does:

- `--input` reads the raw counts from your JSON file.
- `--output` writes the differentially private counts to a CSV.
- `--scale 2.0` sets the noise level. Epsilon is `1 / scale`, so this query spends `0.5`.
- `--threshold 5` masks any privatized count below 5 as `None`, protecting small cohorts.

## 4. Read the audit report

The tool prints a privacy audit report to the console. Because the noise is random, your numbers will differ slightly each run:

```
========================================
PRIVACY AUDIT REPORT
========================================
Privacy Loss (Epsilon): 0.5000
Protection Strength:   HIGH
Influence Limit:      e^0.50 (1.65x)
Output File:          privatized_counts.csv
Count Threshold:      5
----------------------------------------
PII Type        | Raw      | Private
----------------------------------------
ssn             | 8        | 7
creditcard      | 15       | 16
age             | 120      | 118
zipcode         | 45       | 47
date            | 3        | None
----------------------------------------
```

- **Privacy Loss (Epsilon)** is the budget spent by this query.
- **Protection Strength** is a plain-language reading of epsilon (`HIGH` here).
- The **Raw vs. Private** table is for your eyes only. Only the private column is safe to share.
- `date` shows `None` because its privatized count fell below the threshold of 5.

## 5. Inspect the CSV

```bash
cat privatized_counts.csv
```

```csv
PII Type,Private Count
ssn,7
creditcard,16
age,118
zipcode,47
date,None
```

This CSV is the artifact you can hand to analysts, auditors, or a downstream dashboard.

## 6. Watch the privacy budget

Philter Diffuse remembers how much epsilon you have spent against each source. Run the same command again and look at the budget file it created:

```bash
cat privacy_budget.json
```

```json
{"pii_counts.json": 1.0}
```

Two runs at `--scale 2.0` have spent `0.5 + 0.5 = 1.0`. The default ceiling is `10.0`; once cumulative epsilon would cross it, Philter Diffuse stops releasing accurate numbers and applies infinite noise. See [Privacy Budget Management](privacy-budget.md) for how to manage and reset the budget.

## Next steps

- Read counts straight from MongoDB, or Philter's recorded PII counts through its API, in [Usage](usage.md).
- Understand epsilon, the ceiling, and budget exhaustion in [Privacy Budget Management](privacy-budget.md).
- Check the release state and maturity in [Project Status](status.md).
