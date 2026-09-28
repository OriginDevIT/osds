from django.apps import AppConfig


class SmtpConfig(AppConfig):
    name = "adapters.smtp"
    label = "adapters_smtp"

    def ready(self) -> None:
        from adapters.smtp.sender import SmtpSender
        from osds.adapters import register_capability

        register_capability("email.send", SmtpSender())
