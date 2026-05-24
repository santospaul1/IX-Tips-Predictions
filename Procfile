web: gunicorn score_predictor.wsgi --log-file - --workers 2 --timeout 120
worker: celery -A score_predictor worker --loglevel=info --concurrency=2
beat: celery -A score_predictor beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
