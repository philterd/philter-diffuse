FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Run from /data so privacy_budget.json and relative --input and --output paths land on the volume
# mounted there. Without the mount, the budget is lost with the container and every run starts afresh.
WORKDIR /data
ENTRYPOINT ["python", "/app/main.py"]
