FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl tzdata postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# supercronic — lightweight cron runner (replaces Celery beat)
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
RUN curl --fail --show-error --location \
    --retry 5 --retry-all-errors --retry-delay 2 --retry-max-time 120 \
    --output /tmp/supercronic "$SUPERCRONIC_URL" \
    && install -m 0755 /tmp/supercronic /usr/local/bin/supercronic \
    && rm -f /tmp/supercronic

WORKDIR /code

# Install Python deps — cached layer unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /code/backups && chmod +x /code/scripts/release.sh

EXPOSE 8080

# Gunicorn with --preload: loads Django + sklearn once, shared by the worker.
# Single worker — sklearn models are ~50MB each; more workers OOM on 1GB VM.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "120", \
     "--preload", "--access-logfile", "-", "--error-logfile", "-", \
     "score_predictor.wsgi"]
