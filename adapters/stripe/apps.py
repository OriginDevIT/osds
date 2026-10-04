from django.apps import AppConfig


class StripeConfig(AppConfig):
    name = "adapters.stripe"
    label = "adapters_stripe"

    def ready(self) -> None:
        from adapters.stripe.provider import StripeProvider
        from osds.adapters import register_capability

        # Registered but unconfigured is harmless: ``available`` is false until
        # both secrets are set, so nothing offers a checkout (spec §8.6: bundled,
        # not default-enabled).
        provider = StripeProvider()
        register_capability("payments.checkout", provider)
        register_capability("payments.subscription", provider)
