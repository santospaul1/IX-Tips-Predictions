from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Run weekly predictions'

    def handle(self, *args, **kwargs):
        from predict.tasks import trigger_staggered_scheduling

        trigger_staggered_scheduling.delay()
