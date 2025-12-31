from django.apps import AppConfig


class AgromashConfig(AppConfig):
    name = 'agromash'

    def ready(self):
        import agromash.signals
