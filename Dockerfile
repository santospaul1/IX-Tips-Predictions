# ── Build stage: compile dependencies once (cached unless requirements change) ──
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl tzdata postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# supercronic — lightweight cron runner, no redis broker
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
RUN curl -fsSLo /usr/local/bin/supercronic "$SUPERCRONIC_URL" \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /code

# Install Python deps in a cached layer
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Runtime stage: slim image, no build tools ──
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev tzdata postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

# Copy supercronic + site-packages from builder
COPY --from=builder /usr/local/bin/supercronic /usr/local/bin/supercronic
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn

WORKDIR /code
COPY . .

# Backups directory (daily pg_dump, 7-day retention)
RUN mkdir -p /code/backups

EXPOSE 8080

# Gunicorn with --preload: loads Django + sklearn models once, shared by all
# workers. Single worker because sklearn models are ~50MB each — more workers
# would OOM on the 1GB Fly VM.
CMD ["gunicorn", "--bind", ":8080", "--workers", "1", "--timeout", "120", \
     "--preload", "--access-logfile", "-", "--error-logfile", "-", \
     "score_predictor.wsgi"]
