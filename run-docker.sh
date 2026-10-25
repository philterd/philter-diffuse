#!/bin/bash -e

# Build the Docker image
echo "Building Docker image..."
docker build -t philter-diffuse .

# Create a sample input file if it doesn't exist
if [ ! -f sample_counts.json ]; then
cat <<EOF > sample_counts.json
{
  "creditcard": 15,
  "ssn": 8,
  "age": 120,
  "zipcode": 45,
  "date": 3
}
EOF
fi

echo "Running Philter Diffuse in Docker..."

docker run --rm \
  -v "$(pwd)":/data \
  philter-diffuse \
  --input sample_counts.json \
  --output docker_privatized_counts.csv \
  --threshold 5

echo "Docker run complete. Output written to docker_privatized_counts.csv, budget to privacy_budget.json"
