#!/bin/bash

# Create a sample PII counts JSON file
cat <<EOF > sample_counts.json
{
  "creditcard": 15,
  "ssn": 8,
  "age": 120,
  "zipcode": 45,
  "date": 3
}
EOF

echo "Running Philter Diffuse with sample_counts.json..."
python3 main.py \
  --input sample_counts.json \
  --source sample \
  --output privatized_counts.csv \
  --threshold 5 \
  --pii-types creditcard,ssn,age,zipcode,date \
  --budget-ceiling 10.0

rm sample_counts.json