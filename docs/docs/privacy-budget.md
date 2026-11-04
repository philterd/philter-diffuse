# Privacy Budget Management

Differential privacy relies on the concept of a "privacy budget," typically denoted by the Greek letter epsilon ($\epsilon$). This budget represents the maximum cumulative privacy loss that an organization is willing to tolerate for a given dataset. It is measured per document: a person who appears in many documents loses up to epsilon times that number.

## The Core Concept: Epsilon ($\epsilon$)

In Philter Diffuse, $\epsilon$ is a measure of the information leak allowed for each query. 

*   **Calculation**: For the Discrete Laplace mechanism used here, epsilon for a single query is calculated as:
    $$\epsilon = \frac{\Delta f}{\text{scale}}$$
    Where $\Delta f$ is the **sensitivity** and **scale** is the noise parameter provided by the user via `--scale`. One document adds at most one to each PII type's count, but it can add to every type at once, so the sensitivity of a query is the number of PII types in `--pii-types`. The charge depends on that list, not on which types the data holds. OpenDP computes epsilon from it.
*   **Default**: With the default scale of `2.0`, a query releasing 4 PII types consumes $\epsilon = 4/2 = 2.0$. To spend less per query, raise `--scale`: epsilon falls in proportion.

### Why do we need a budget?
Every time a privatized query is released, a small amount of information about the underlying data is leaked. If an attacker can query the same dataset multiple times with different noise, they could potentially average out the noise to reconstruct the original values. A privacy budget places a hard limit on this cumulative leakage.

## Persistent Tracking

Philter Diffuse automatically tracks the cumulative epsilon spent for each unique data source (a MongoDB collection, a JSON file, or a Philter deployment read through its API). This tracking is **persistent**, meaning it survives between different executions of the tool.

*   **MongoDB Mode**: The spent budget is stored in a collection named `privacy_metadata` within the same database as the data.
*   **JSON Mode**: The spent budget is stored in the budget file, under the `--source` name. A renamed or copied input file keeps its budget, and a new `--source` name starts a new one, so use one name per dataset.
*   **Philter API Mode** (`--philter-url`): The spent budget is stored in the budget file, under a key naming the Philter URL, such as `pii_count_aggregates@https://localhost:8080`. Nothing is written to Philter or its database. Every read from that deployment draws on the one budget, whatever `--context`, `--from`, or `--to` it uses, so overlapping reports cannot each spend a fresh budget. If Philter reports that count recording is off, no budget is spent. Moving from `--mongo-uri --aggregates` to `--philter-url` starts a new budget entry: the epsilon already spent is recorded in MongoDB's `privacy_metadata` collection (under the `pii_counts` collection name), not in the budget file. When you move, copy the spent epsilon to the new entry so earlier releases still count against the ceiling.

The budget file is `~/.philter-diffuse/privacy_budget.json`, so it does not depend on the working directory. To keep it elsewhere, such as on storage shared by several machines, set `--budget-file` or the `PHILTER_DIFFUSE_BUDGET_FILE` environment variable, and use the same file for every run against a source. A budget file that cannot be read stops the run rather than starting again from zero, so back it up.

The budget is charged before any noise is sampled, as one step with the ceiling check, so runs at the same time cannot each spend the same balance. On Windows, concurrent runs against one budget file are not serialized: run them one at a time.

## The Budget Ceiling

The `--budget-ceiling` parameter (default: `10.0`) defines the maximum allowed cumulative epsilon. 

### What happens when the ceiling is reached?
If a requested query would cause the total spent epsilon to exceed the ceiling, Philter Diffuse refuses it:

1.  **Error**: The run exits with code `1`, and standard error gives the cost of the release and the amount already spent.
2.  **No Output**: No counts are released. Any existing `--output` file is left unchanged, and no report is printed.
3.  **No Budget Consumption**: Nothing is deducted from the remaining budget.

## Interpreting Protection Strength

To assist non-technical auditors, Philter Diffuse provides a qualitative interpretation of the privacy loss of each query (not the cumulative total, which is in the budget store):

| Query Epsilon ($\epsilon$) | Protection Strength | Description |
| :--- | :--- | :--- |
| $\epsilon \leq 0.1$ | **EXTREME** | Highest level of protection; suitable for extremely sensitive data. |
| $0.1 < \epsilon \leq 1.0$ | **HIGH** | Strong protection; standard for most production use cases. |
| $1.0 < \epsilon \leq 5.0$ | **MODERATE** | Balanced protection and utility. |
| $\epsilon > 5.0$ | **LOW** | Useful for testing or less sensitive data; more can be inferred about any one document. |

## Managing the Budget

### Resetting the Budget
If you have refreshed your underlying dataset (e.g., a new month of data) and wish to reset the budget:
*   **MongoDB**: Delete the document for that collection in the `privacy_metadata` collection.
*   **JSON or Philter API**: Edit the budget file and remove the entry for your `--source` name or Philter URL, or delete the file entirely.

### Adjusting the Ceiling
You can increase the ceiling by passing a higher value to the `--budget-ceiling` flag during execution. However, this should only be done after careful consideration of the privacy implications.
