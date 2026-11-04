FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py LICENSE ./

# Run as an unprivileged user that owns /data, so a named volume mounted there is writable.
RUN useradd --create-home --uid 1000 philter && mkdir /data && chown philter:philter /data
USER philter

# Run from /data so privacy_budget.json and relative --input and --output paths land on the volume
# mounted there. Without the mount, the budget is lost with the container and every run starts afresh.
WORKDIR /data
ENV PHILTER_DIFFUSE_BUDGET_FILE=/data/privacy_budget.json
ENTRYPOINT ["python", "/app/main.py"]
