"""The bundled ``smtp`` adapter (spec §8.6): stdlib-only transactional email,
registered as the ``email.send`` capability.

``AppConfig.ready()`` in ``apps.py`` (``adapters.smtp.apps.SmtpConfig``, the
sole app config here, picked up automatically because it is the only one
declared) is where the provider registers -- never here at import time,
since module import happens on every ``from adapters.smtp import ...``,
however incidental, while ``ready()`` runs exactly once, when Django
finishes app loading.
"""
