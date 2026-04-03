FROM python:3.12-slim

WORKDIR /app

# System deps (lxml, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code (not .venv / data / __pycache__)
COPY *.py ./
COPY *.yaml ./
COPY static/ ./static/
COPY templates/ ./templates/

# Data dir — will be shadowed by fly.io volume mount at /data
RUN mkdir -p /data

# Server listens on 8080 inside container (fly proxies to 443)
ENV PORT=8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
