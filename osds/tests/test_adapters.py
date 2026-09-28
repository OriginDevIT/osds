"""The capability registry seam (decisions.md §4.3), beside the
event-subscriber registry it sits next to in ``osds/adapters.py``.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from osds.adapters import capability_provider, override_capability, register_capability


class CapabilityRegistryTests(SimpleTestCase):
    def setUp(self):
        # The real install has already registered "email.send" via
        # adapters.smtp's AppConfig.ready() by the time tests run --
        # override_capability starts each test from a clean slate.
        self.enterContext(override_capability({}))

    def test_unregistered_capability_returns_none(self):
        self.assertIsNone(capability_provider("email.send"))

    def test_registered_provider_is_returned(self):
        provider = object()
        register_capability("email.send", provider)
        self.assertIs(capability_provider("email.send"), provider)

    def test_second_registration_of_the_same_capability_raises(self):
        register_capability("email.send", object())
        with self.assertRaises(ValueError):
            register_capability("email.send", object())

    def test_second_registration_does_not_replace_the_first(self):
        first = object()
        register_capability("email.send", first)
        with self.assertRaises(ValueError):
            register_capability("email.send", object())
        self.assertIs(capability_provider("email.send"), first)

    def test_different_capabilities_do_not_collide(self):
        email_provider = object()
        sms_provider = object()
        register_capability("email.send", email_provider)
        register_capability("sms.send", sms_provider)
        self.assertIs(capability_provider("email.send"), email_provider)
        self.assertIs(capability_provider("sms.send"), sms_provider)


class OverrideCapabilityTests(SimpleTestCase):
    def test_installs_exactly_the_given_providers(self):
        provider = object()
        with override_capability({"email.send": provider}):
            self.assertIs(capability_provider("email.send"), provider)
            self.assertIsNone(capability_provider("sms.send"))

    def test_restores_the_previous_registry_on_exit(self):
        with override_capability({"email.send": object()}):
            pass
        # The real smtp provider, registered at app load, is back.
        self.assertIsNotNone(capability_provider("email.send"))

    def test_empty_dict_simulates_no_provider_registered(self):
        with override_capability({}):
            self.assertIsNone(capability_provider("email.send"))

    def test_yields_the_live_registry(self):
        with override_capability({}) as registry:
            registry["email.send"] = object()
            self.assertIsNotNone(capability_provider("email.send"))
