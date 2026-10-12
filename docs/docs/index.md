# Philter Diffuse

Philter Diffuse provides mathematical guarantees for PII count aggregations using [OpenDP](https://github.com/opendp/opendp). It is designed for organizations that need to share or publish aggregate statistics about personally identifiable information (PII) without compromising the privacy of individuals represented in the data.

## Overview

In many data analysis scenarios, simply removing direct identifiers (like names) is insufficient to protect privacy. Aggregate counts can still leak information if an attacker has background knowledge or if the counts are very small. Philter Diffuse addresses this by applying **Differential Privacy**, a rigorous mathematical framework for privacy protection.

It uses the **Discrete Laplace mechanism** to privatize PII counts. This mechanism adds carefully calibrated random noise to the raw counts, ensuring that the presence or absence of any single individual in the dataset has a limited impact on the released results.

## Integration with the Philter Ecosystem

Philter Diffuse is a key component of the Philterd privacy suite, designed to work seamlessly with:

*   **[Philter](https://www.github.com/philterd/philter)**: A PII detection and redaction engine. Philter identifies PII in unstructured text.
*   **[Phileas](https://www.github.com/philterd/phileas)**: A high-performance PII redaction service.

Typically, Philter or Phileas processes text and generates counts of PII entities found. Philter Diffuse then takes these raw counts and applies differential privacy before they are used for reporting or analytics.

## Get Started

New to Philter Diffuse? The [Quickstart](quickstart.md) takes you from a clean checkout to a privatized CSV and an audit report in about five minutes, no database required. For the current release state and maturity, see [Project Status](status.md).

## Key Features

*   **Robust Differential Privacy**: Implements the Discrete Laplace mechanism from the OpenDP library, providing formally provable privacy guarantees.
*   **Persistent Privacy Budget Management**: Tracks the cumulative "epsilon" (privacy loss) across multiple queries. This prevents "privacy exhaustion," where repeated queries on the same data could eventually reveal individual information.
*   **Flexible Data Ingestion**:
    *   **MongoDB Integration**: Directly connect to MongoDB to fetch and privatize counts stored in collections.
    *   **JSON Support**: Process PII counts from simple local JSON files, making it easy to integrate into existing pipelines.
*   **Compliance-Ready Audit Reports**: Generates detailed reports for every execution, including qualitative interpretations of the privacy strength (e.g., "EXTREME", "HIGH") to help compliance officers and auditors.
*   **Safe Output Formatting**:
    *   **CSV Export**: Results are exported to standard CSV format for easy consumption by downstream tools.
    *   **Thresholding**: Automatically suppresses counts below a user-defined threshold (e.g., showing "None" for counts less than 5) to provide an additional layer of protection against re-identification.
*   **Reads Philter Telemetry Directly**: Privatizes Philter's aggregated, document-presence PII counts with `--aggregates`, read through Philter's API with an API key (`--philter-url`, Philter 4.0 or later) or from its `pii_count_aggregates` collection (deprecated), so you can run analytics on your own redaction telemetry.
*   **Dockerized Deployment**: Easily deployable via Docker for consistent and isolated execution environments.
