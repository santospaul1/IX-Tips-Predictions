FROM python:3.12-slim

RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /code
WORKDIR /code

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt && rm -rf /root/.cache/

COPY . /code/

EXPOSE 8080

# collectstatic runs at startup so SECRET_KEY env var is available
CMD ["sh", "-c", "python manage.py collectstatic --noinput && python manage.py migrate --run-syncdb && gunicorn score_predictor.wsgi --bind 0.0.0.0:8080 --log-file - --workers 2 --timeout 120"]
