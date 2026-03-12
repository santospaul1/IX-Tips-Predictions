from django.apps import AppConfig


class PredictConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'predict'

    def ready(self):
        from . import signals  # we'll create this next