# Frequently Asked Questions (FAQ)

This page addresses common questions and concerns regarding Philter Diffuse, its implementation of differential privacy, and its integration into your data pipelines.

## General Questions

### What is Philter Diffuse?
Philter Diffuse is a specialized utility designed to add an extra layer of privacy to PII (Personally Identifiable Information) count aggregations. By using the Discrete Laplace mechanism provided by the OpenDP library, it injects mathematical noise into raw counts, ensuring that individual data points cannot be reconstructed from the aggregate output.

### When would I use Philter Diffuse?
You would use Philter Diffuse when you need to report or share statistics based on sensitive PII (Personally Identifiable Information) but must guarantee that those statistics cannot be used to re-identify any individual in the dataset. This is common in healthcare (HIPAA compliance), financial reporting, and academic research where aggregate data must be publicly released or shared with third parties.

### How does it differ from "standard" anonymization?
Traditional anonymization (like k-anonymity) often fails against linkage attacks or background knowledge. Differential privacy provides a rigorous mathematical guarantee that the presence or absence of a single individual in the dataset will not significantly change the outcome of the analysis.

### Is commercial support available?
Yes. Commercial support for Philter Diffuse is available through [Philterd, LLC](https://www.philterd.ai). We offer enterprise-grade service level agreements (SLAs), custom integration assistance, and priority bug fixes for our entire ecosystem of PII de-identification tools.

### Is Philter Diffuse part of a larger ecosystem?
Yes. Philter Diffuse is designed to complement [Philter](https://www.github.com/philterd/philter) (the PII de-identification engine) and [Phileas](https://www.github.com/philterd/phileas) (the enterprise-grade PII management platform). While Philter removes PII from text, Philter Diffuse ensures that the *statistics* derived from that PII remain private.

---

## Technical & Mathematical Questions

### What is the "Epsilon" ($\epsilon$) value?
Epsilon is a measure of "privacy loss." A smaller epsilon (e.g., 0.1) means more privacy but less accuracy because more noise is added. A larger epsilon (e.g., 5.0) means less privacy but higher accuracy. Philter Diffuse calculates epsilon as $n / \text{scale}$, where $n$ is the number of PII types released.

### Why are some counts returned as "None"?
When you set a `--threshold` (e.g., `--threshold 10`), any privatized count that falls below that value is masked as `None`. This prevents the leakage of information from very small cohorts, which are often the most vulnerable to re-identification.

### Can the privatized counts be negative?
Mathematically, Laplace noise can be negative and could result in a negative count. However, Philter Diffuse automatically clips all privatized counts to a minimum of 0 to ensure the output is realistic for count-based data.

### What is "Sensitivity"?
Sensitivity is how much adding or removing one document can change the released counts. Philter Diffuse counts documents, so one document changes any one count by at most 1. A document can contain several PII types, though, and so change several counts at once: the sensitivity of a query is the number of PII types it releases.

---

## Privacy Budget Management

### Why do I need a "Budget Ceiling"?
If you run the same query multiple times on the same data, an attacker could potentially average the results to filter out the noise and find the true value. The privacy budget (managed via `--budget-ceiling`) tracks the cumulative epsilon spent and stops providing accurate results once the limit is reached to prevent this "averaging attack."

### Where is the budget stored?
*   **MongoDB Mode**: The budget is stored in a collection named `privacy_metadata` within your database.
*   **Philter API Mode** (`--philter-url`): The budget is stored in the local `privacy_budget.json`, keyed by the Philter URL. Nothing is written to Philter's database.
*   **JSON Mode**: The budget is stored in a local file named `privacy_budget.json` in the working directory.

### How can I reset the privacy budget?
If you are in a development or testing environment and wish to reset the budget:
*   In MongoDB: Drop the `privacy_metadata` collection or delete the specific document for your collection.
*   In JSON mode: Delete the `privacy_budget.json` file.
*   **Note**: In production, resetting the budget should be done with extreme caution as it potentially violates the privacy guarantees for the underlying data subjects.

---

## Troubleshooting

### Why did I get "Infinite Noise" in my report?
This happens when your cumulative privacy loss has exceeded the `--budget-ceiling`. To protect the data, Philter Diffuse stops adding useful information and instead applies "infinite noise," effectively returning 0 for all counts and warning you of the exhaustion.

### My MongoDB connection is failing. What should I check?
Ensure that your `--mongo-uri` is correct and follows the format `mongodb://[user:pass@]host:port/database`. Also, verify that the MongoDB instance is accessible from the environment where Philter Diffuse is running (check firewalls and network bindings).

### Can I use Philter Diffuse with other databases besides MongoDB?
Currently, Philter Diffuse has native support for MongoDB, local JSON files, and Philter's PII Counts API (`--philter-url`, Philter 4.0 or later). For other databases, we recommend exporting your PII counts to a JSON format and using the `--input` flag.
