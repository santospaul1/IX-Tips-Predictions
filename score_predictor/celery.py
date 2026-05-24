import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'score_predictor.settings')

app = Celery("score_predictor")
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'run-staggered-predictions-daily': {
        'task': 'predict.tasks.trigger_staggered_scheduling',
        'schedule': crontab(hour=6, minute=0),
    },
    'cache-training-data-daily': {
        'task': 'predict.tasks.cache_training_data',
        'schedule': crontab(hour=5, minute=30),
    },
    'refresh-daily-odds-cache': {
        'task': 'predict.tasks.refresh_daily_odds_cache',
        'schedule': crontab(hour=6, minute=20),
    },
    'refresh-combo-slips-daily': {
        'task': 'predict.tasks.refresh_combo_slips',
        'schedule': crontab(hour=6, minute=30),
    },
    'refresh-league-standings': {
        'task': 'predict.tasks.refresh_all_league_tables',
        'schedule': crontab(minute=1),
    },
    "update_metadata_hourly": {
        "task": "predict.tasks.update_metadata_task",
        "schedule": crontab(minute=0, hour='*'),
    },
    "refresh-live-match-data-every-5-minutes": {
        "task": "predict.tasks.refresh_live_match_data",
        "schedule": crontab(minute="*/5"),
    },
}
