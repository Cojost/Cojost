from django.apps import AppConfig


class SalesLogAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'SalesLogApp'

    def ready(self):
        import SalesLogApp.checks  # noqa: F401
        import SalesLogApp.signals  # noqa: F401
