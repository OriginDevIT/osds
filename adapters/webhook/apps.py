from django.apps import AppConfig


class WebhookConfig(AppConfig):
    name = "adapters.webhook"
    label = "adapters_webhook"

    def ready(self) -> None:
        from adapters.webhook.adapter import WebhookAdapter
        from osds.adapters import register

        # Bundled and always registered ("default enabled", spec §8.6): it does
        # nothing for a tenant until that tenant configures an endpoint.
        register(WebhookAdapter())
