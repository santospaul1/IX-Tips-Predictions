from django.core.management import call_command
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Run weekly predictions (synchronous; no Celery/Redis required)'

    def handle(self, *args, **kwargs):
        call_command("run_task", "predictions")
