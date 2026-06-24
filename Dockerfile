FROM python:3.12-slim

RUN apt-get update && apt-get install -y libpq-dev gcc curl tzdata && rm -rf /var/lib/apt/lists/*

# supercronic — container-friendly cron that runs the scheduled jobs
# (replaces Celery beat/worker, so no Redis broker is needed).
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 \
    SUPERCRONIC=supercronic-linux-amd64
RUN curl -fsSLO "$SUPERCRONIC_URL" \
    && chmod +x "$SUPERCRONIC" \
    && mv "$SUPERCRONIC" /usr/local/bin/supercronic

RUN mkdir -p /code
WORKDIR /code

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt && rm -rf /root/.cache/

COPY . /code/

EXPOSE 8080

CMD ["gunicorn", "--bind", ":8080", "--workers", "2", "--timeout", "120", "score_predictor.wsgi"]
