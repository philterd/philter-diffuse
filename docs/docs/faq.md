# Frequently Asked Questions (FAQ)

This page addresses common questions and concerns regarding Philter Diffuse, its implementation of differential privacy, and its integration into your data pipelines.

## General Questions

### What is Philter Diffuse?
Philter Diffuse is a specialized utility designed to add an extra layer of privacy to PII (Personally Identifiable Information) count aggregations. By using the Discrete Laplace mechanism provided by the OpenDP library, it injects mathematical noise into raw counts, bounding how much the aggregate output reveals about any one document.

### When would I use Philter Diffuse?
You would use Philter Diffuse when you need to report or share statistics based on sensitive PII (Personally Identifiable Information) but need to limit what those statistics reveal about any one document in the dataset. This is common in healthcare, financial reporting, and academic research where aggregate data must be publicly released or shared with third parties.

### How does it differ from "standard" anonymization?
Traditional anonymization (like k-anonymity) often fails against linkage attacks or background knowledge. Differential privacy provides a formal bound on how much the presence or absence of a single document can change the outcome of the analysis.

### Does it protect people or documents?
Documents. The bound applies to one document: one redaction in Philter, or one record counted by the source. A person who appears in many documents can move a count by as many documents as they appear in, and their protection weakens in proportion: epsilon times that number. Philter's counts do not identify people, so Philter Diffuse cannot limit how much one person contributes. Account for this when you choose `--scale` and `--budget-ceiling`, particularly where one person can appear in many documents.

### Why must I list the PII types?
A row for each type found would show that at least one document held that type, even with its count masked as `None`. With `--pii-types`, the rows are fixed before the data is read. See [Choosing the PII types](usage.md#choosing-the-pii-types).

### Is commercial support available?
Yes. Commercial support for Philter Diffuse is available through [Philterd, LLC](https://www.philterd.ai). We offer enterprise-grade service level agreements (SLAs), custom integration assistance, and priority bug fixes for our entire ecosystem of PII de-identification tools.

### Is Philter Diffuse part of a larger ecosystem?
Yes. Philter Diffuse is designed to complement [Philter](https://www.github.com/philterd/philter) (the PII de-identification engine) and [Phileas](https://www.github.com/philterd/phileas) (the PII redaction library underneath Philter). While Philter removes PII from text, Philter Diffuse ensures that the *statistics* derived from that PII remain private.

---

## Technical & Mathematical Questions

### What is the "Epsilon" ($\epsilon$) value?
Epsilon is a measure of "privacy loss." A smaller epsilon (e.g., 0.1) means more privacy but less accuracy because more noise is added. A larger epsilon (e.g., 5.0) means less privacy but higher accuracy. Philter Diffuse calculates epsilon as $n / \text{scale}$, where $n$ is the number of PII types in `--pii-types`.

### Why are some counts returned as "None"?
When you set a `--threshold` (e.g., `--threshold 10`), any privatized count that falls below that value is masked as `None`. This prevents the leakage of information from very small cohorts, which are often the most vulnerable to re-identification.

### Can the privatized counts be negative?
Mathematically, Laplace noise can be negative and could result in a negative count. However, Philter Diffuse automatically clips all privatized counts to a minimum of 0 to ensure the output is realistic for count-based data.

### What is "Sensitivity"?
Sensitivity is how much adding or removing one document can change the released counts. Philter Diffuse counts documents, so one document changes any one count by at most 1. A document can contain several PII types, though, and so change several counts at once: the sensitivity of a query is the number of PII types in `--pii-types`.

---

## Privacy Budget Management

### Why do I need a "Budget Ceiling"?
If you run the same query multiple times on the same data, an attacker could potentially average the results to filter out the noise and find the true value. The privacy budget (managed via `--budget-ceiling`) tracks the cumulative epsilon spent and stops providing accurate results once the limit is reached to prevent this "averaging attack."

### Where is the budget stored?
*   **MongoDB Mode**: The budget is stored in a collection named `privacy_metadata` within your database.
*   **Philter API Mode** (`--philter-url`): The budget is stored in the budget file, keyed by the Philter URL. Nothing is written to Philter's database.
*   **JSON Mode**: The budget is stored in the budget file, keyed by the `--source` name.

The budget file is `~/.philter-diffuse/privacy_budget.json`, unless `--budget-file` or the `PHILTER_DIFFUSE_BUDGET_FILE` environment variable names another.

### How can I reset the privacy budget?
In a development or testing environment, reset the budget as follows. In production, see [Resetting the Budget](privacy-budget.md#resetting-the-budget) first.
*   In MongoDB: Drop the `privacy_metadata` collection or delete the specific document for your collection.
*   In JSON or Philter API mode: Remove the entry from the budget file, or delete the file.
*   **Note**: In production, resetting the budget should be done with extreme caution as it potentially invalidates the privacy bound on the underlying documents.

---

## Troubleshooting

### Why did my run stop with a budget error?
The release would have taken cumulative privacy loss past the `--budget-ceiling`. To protect the data, Philter Diffuse exits with an error (exit code `1`), writes no output, leaves any existing `--output` file unchanged, and spends no budget. The error gives the cost of the release and the amount already spent.

### My MongoDB connection is failing. What should I check?
Ensure that your `--mongo-uri` is correct and follows the format `mongodb://[user:pass@]host:port/database`. Also, verify that the MongoDB instance is accessible from the environment where Philter Diffuse is running (check firewalls and network bindings).

### Can I use Philter Diffuse with other databases besides MongoDB?
Currently, Philter Diffuse has native support for MongoDB, local JSON files, and Philter's PII Counts API (`--philter-url`, Philter 4.0 or later). For other databases, we recommend exporting your PII counts to a JSON format and using the `--input` flag.
