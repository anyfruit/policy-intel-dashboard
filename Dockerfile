FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ ./static/
COPY templates/ ./templates/
COPY seed.db ./seed.db

CMD sh -c "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"
