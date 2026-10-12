# Project Status

## Release state

Philter Diffuse is **early-access software at version `0.1.0`**.

The differential-privacy mechanism (Discrete Laplace via [OpenDP](https://github.com/opendp/opendp)), the privacy-budget accounting, and the command-line interface are usable and covered by tests. Because it is a `0.x` release, the command-line interface and output format may still change before a stable `1.0`. Pin to a specific commit or release if you depend on the current behavior.

Check the version of your checkout at any time:

```bash
python main.py --version
```

This is the authoritative status for the product. Marketing or product-page copy should defer to the version and checklist here rather than implying a stable release exists before one is tagged.

## Maturity checklist

The current state of the things a developer-facing tool needs before it is "done":

| Area | State | Notes |
| :--- | :--- | :--- |
| **Tests** | Done | `test_main.py` exercises privatization, budget tracking, thresholding, JSON/Mongo ingestion, the aggregates reader, and reading counts from Philter's API against a stubbed Philter; CI runs `pytest` on every push and pull request (`.github/workflows/tests.yml`). |
| **Examples** | Done | `example.sh` (local JSON run) and `run-docker.sh` (containerized run) are runnable end to end; the [Quickstart](quickstart.md) is a worked walkthrough. |
| **Docs** | Done | Install, usage, CLI options, privacy-budget management, quickstart, and FAQ are published to <https://philterd.github.io/philterdiffuse/> and rebuilt on every push (`.github/workflows/docs.yml`). |
| **Release artifact** | Not started | No git tag, GitHub release, or PyPI package yet. The tool runs from a checkout via `python main.py`. Tagging `0.1.0` and cutting a matching GitHub release is the next step toward `1.0`. |

When the checklist items change, update this table and the `__version__` in `main.py` together.

## Roadmap to 1.0

- Tag `0.1.0` and publish a GitHub release with notes so prospects see a concrete release state.
- Decide whether to package for PyPI (`pip install philterdiffuse`) or keep the checkout-and-run model, and document the decision here.
- Freeze the CLI flags and CSV schema, then promote to `1.0.0`.
