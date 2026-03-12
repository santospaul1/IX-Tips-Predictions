import json

from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.utils.timezone import now

from django_celery_beat.models import CrontabSchedule, PeriodicTask


OBSOLETE_TASKS = (
    "predict.tasks.update_actual_results",
    "predict.tasks.update_predictions_and_cache",
    "predict.tasks.schedule_predictions",
    "predict.tasks.predict_for_competition",
    "predict.tasks.update_match_status_task",
    "predict.tasks.update_actual_results_for_competition",
    "predict.tasks.update_match_status_and_results",
    "predict.tasks.get_or_cache_training_data",
)


def ensure_periodic_task(*, name, task, minute, hour="*", day_of_week="*", day_of_month="*", month_of_year="*"):
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute=minute,
        hour=hour,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        timezone="Africa/Nairobi",
    )
    PeriodicTask.objects.update_or_create(
        name=name,
        defaults={
            "crontab": schedule,
            "task": task,
            "start_time": now(),
            "enabled": True,
            "kwargs": json.dumps({}),
        },
    )


def setup_scheduled_tasks():
    PeriodicTask.objects.filter(task__in=OBSOLETE_TASKS).delete()

    ensure_periodic_task(
        name="Run Staggered Predictions Daily",
        task="predict.tasks.trigger_staggered_scheduling",
        hour="6",
        minute="0",
    )
    ensure_periodic_task(
        name="Cache Training Data Daily",
        task="predict.tasks.cache_training_data",
        hour="5",
        minute="30",
    )
    ensure_periodic_task(
        name="Refresh Daily Odds Cache",
        task="predict.tasks.refresh_daily_odds_cache",
        hour="6",
        minute="20",
    )
    ensure_periodic_task(
        name="Refresh League Standings",
        task="predict.tasks.refresh_all_league_tables",
        minute="1",
    )
    ensure_periodic_task(
        name="Update Metadata Hourly",
        task="predict.tasks.update_metadata_task",
        hour="*",
        minute="0",
    )
    ensure_periodic_task(
        name="Refresh Live Match Data Every 5 Minutes",
        task="predict.tasks.refresh_live_match_data",
        minute="*/5",
    )


@receiver(post_migrate)
def create_periodic_tasks(sender, **kwargs):
    if sender.name != "predict":
        return

    try:
        setup_scheduled_tasks()
    except Exception as exc:
        print(f"Error setting up scheduled tasks: {exc}")
