# Installation

Philter Diffuse runs from a checkout of its repository, directly with Python or in Docker. It is not published as a package.

## Prerequisites

Before installing Philter Diffuse, ensure your system meets the following requirements:

*   **Python**: Version 3.12 is tested, in CI and in the Docker image. The current OpenDP release requires 3.10 or later.
*   **pip**: The standard Python package manager.
*   **MongoDB (Optional)**: If you plan to use MongoDB as a data source, ensure it's installed and accessible from your environment.
*   **Docker (Optional)**: If you prefer to run the application in a container.

## Direct Installation

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/philterd/philter-diffuse.git
    cd philter-diffuse
    ```

2.  **Create a Virtual Environment** (Recommended):
    It's best practice to use a virtual environment to manage dependencies and avoid conflicts with other projects.
    ```bash
    python -m venv venv
    source venv/bin/activate  # On macOS/Linux
    # venv\Scripts\activate     # On Windows
    ```

3.  **Install Dependencies**:
    The core dependencies for Philter Diffuse include `opendp`, `pymongo`, and other support libraries. Install them using the provided `requirements.txt`:
    ```bash
    pip install -r requirements.txt
    ```

## Docker Installation

Docker provides an isolated and consistent environment for running Philter Diffuse, eliminating potential "it works on my machine" issues.

### 1. Build the Docker Image

From the root of the project directory, build the image:

```bash
docker build -t philterdiffuse .
```

This will download the base Python image, copy the source code, and install all necessary dependencies inside the container.

### 2. Run the Container

You can run the container manually or use the provided `run-docker.sh` script, which automates common volume mounts for data processing.

**Example: Using the provided script**
```bash
./run-docker.sh
```

**Example: Running manually with volume mounts**
The image's entrypoint is `main.py`, so arguments after the image name are Philter Diffuse's options. It runs in `/data`: mount a directory there to hold the input, the output, and `privacy_budget.json`.
```bash
docker run --rm -v $(pwd)/data:/data \
           philterdiffuse \
           --input pii_counts.json \
           --output privatized_counts.csv
```

Always mount `/data`. The spent privacy budget is kept in `/data/privacy_budget.json`, so without the mount it is lost when the container exits and the next run starts with a fresh budget, which defeats `--budget-ceiling`.

**Example: Reading counts from a Philter container**
To read counts from a [Philter](https://github.com/philterd/philter) container through its API (Philter 4.0 or later), attach both containers to one Docker network and address Philter by its alias `philter`, which is in the subject alternative name of the self-signed certificate Philter's image generates. Export that certificate into `data/` first (see [Usage](usage.md#through-philters-api-recommended)).
```bash
docker network create philter-net
docker network connect --alias philter philter-net <philter-container>
docker run --rm --network philter-net -v $(pwd)/data:/data \
           -e PHILTER_API_KEY \
           philterdiffuse \
           --philter-url https://philter:8080 \
           --philter-ca-cert philter.pem \
           --output privatized_counts.csv
```

`-e PHILTER_API_KEY` passes the key from your shell's environment without putting it on the command line. The budget is keyed by the URL, so keep using the same `--philter-url` for a deployment: `https://philter:8080` and `https://localhost:8080` are separate budget entries.

## Verification

After installation, verify that Philter Diffuse is ready to use by running the help command:

```bash
python main.py --help
```

You should see a list of available command-line options and their descriptions.
