# Privacy Budget Management

Differential privacy relies on the concept of a "privacy budget," typically denoted by the Greek letter epsilon ($\epsilon$). This budget represents the maximum cumulative privacy loss that an organization is willing to tolerate for a given dataset.

## The Core Concept: Epsilon ($\epsilon$)

In Philter Diffuse, $\epsilon$ is a measure of the information leak allowed for each query. 

*   **Calculation**: For the Discrete Laplace mechanism used here, epsilon for a single query is calculated as:
    $$\epsilon = \frac{\Delta f}{\text{scale}}$$
    Where $\Delta f$ is the **sensitivity** and **scale** is the noise parameter provided by the user via `--scale`. One document adds at most one to each PII type's count, but it can add to every type at once, so the sensitivity of a query is the number of PII types it releases. OpenDP computes epsilon from it.
*   **Default**: With the default scale of `2.0`, a query releasing 4 PII types consumes $\epsilon = 4/2 = 2.0$. To spend less per query, raise `--scale`: epsilon falls in proportion.

### Why do we need a budget?
Every time a privatized query is released, a small amount of information about the underlying data is leaked. If an attacker can query the same dataset multiple times with different noise, they could potentially average out the noise to reconstruct the original values. A privacy budget places a hard limit on this cumulative leakage.

## Persistent Tracking

Philter Diffuse automatically tracks the cumulative epsilon spent for each unique data source (a MongoDB collection, a JSON file, or a Philter deployment read through its API). This tracking is **persistent**, meaning it survives between different executions of the tool.

*   **MongoDB Mode**: The spent budget is stored in a collection named `privacy_metadata` within the same database as the data.
*   **JSON Mode**: The spent budget is stored in a local file named `privacy_budget.json` in the working directory.
*   **Philter API Mode** (`--philter-url`): The spent budget is stored in the same local `privacy_budget.json`, under a key naming the Philter URL, such as `pii_count_aggregates@https://localhost:8080`. Nothing is written to Philter or its database. Every read from that deployment draws on the one budget, whatever `--context`, `--from`, or `--to` it uses, so overlapping reports cannot each spend a fresh budget. If Philter reports that count recording is off, no budget is spent. Moving from `--mongo-uri --aggregates` to `--philter-url` starts a new budget entry: the epsilon already spent is recorded in MongoDB's `privacy_metadata` collection (under the `pii_counts` collection name), not in `privacy_budget.json`. When you move, copy the spent epsilon to the new entry so earlier releases still count against the ceiling.

## The Budget Ceiling

The `--budget-ceiling` parameter (default: `10.0`) defines the maximum allowed cumulative epsilon. 

### What happens when the ceiling is reached?
If a requested query would cause the total spent epsilon to exceed the ceiling, Philter Diffuse takes the following actions to protect the data:

1.  **Warning**: A prominent warning is printed to the console and included in the audit report.
2.  **Infinite Noise**: The scale is effectively increased to infinity.
3.  **Result Suppression**: All privatized counts are returned as `0` (or masked if thresholding is active). 
4.  **No Budget Consumption**: Since the released result contains no useful information (pure noise), no additional epsilon is deducted from the remaining budget.

## Interpreting Protection Strength

To assist non-technical auditors, Philter Diffuse provides a qualitative interpretation of the cumulative privacy loss:

| Cumulative Epsilon ($\epsilon$) | Protection Strength | Description |
| :--- | :--- | :--- |
| $\epsilon \leq 0.1$ | **EXTREME** | Highest level of protection; suitable for extremely sensitive personal data. |
| $0.1 < \epsilon \leq 1.0$ | **HIGH** | Strong protection; standard for most production use cases. |
| $1.0 < \epsilon \leq 5.0$ | **MODERATE** | Balanced protection and utility. |
| $\epsilon > 5.0$ | **LOW** | Useful for testing or less sensitive data; risk of re-identification increases. |

## Managing the Budget

### Resetting the Budget
If you have refreshed your underlying dataset (e.g., a new month of data) and wish to reset the budget:
*   **MongoDB**: Delete the document for that collection in the `privacy_metadata` collection.
*   **JSON or Philter API**: Edit `privacy_budget.json` and remove the entry for your file or Philter URL, or delete the file entirely.

### Adjusting the Ceiling
You can increase the ceiling by passing a higher value to the `--budget-ceiling` flag during execution. However, this should only be done after careful consideration of the privacy implications.
