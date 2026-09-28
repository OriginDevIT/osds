"""directory.masking -- contact-detail masking for the public claim UI
(spec §9.4). Pure functions, no DB.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from directory.masking import mask_email, mask_phone_e164


class MaskPhoneTests(SimpleTestCase):
    def test_nanp_number_keeps_single_digit_code_and_last_two(self):
        self.assertEqual(mask_phone_e164("+17735550142"), "+1••••42")

    def test_two_digit_code_is_assumed_for_anything_not_1_or_7(self):
        self.assertEqual(mask_phone_e164("+442071234567"), "+44••••67")

    def test_russia_is_the_other_single_digit_code(self):
        self.assertEqual(mask_phone_e164("+79261234567"), "+7••••67")

    def test_bullet_run_is_fixed_length_regardless_of_input_length(self):
        short = mask_phone_e164("+17735550142")
        long = mask_phone_e164("+441234567890123")
        run = "••••"
        self.assertIn(run, short)
        self.assertIn(run, long)
        self.assertEqual(short.count("•"), long.count("•"))

    def test_empty_and_non_e164_pass_through_unchanged(self):
        self.assertEqual(mask_phone_e164(""), "")
        self.assertEqual(mask_phone_e164("not a phone number"), "not a phone number")

    def test_too_short_to_mask_meaningfully_hides_entirely(self):
        self.assertEqual(mask_phone_e164("+12"), "+••••")


class MaskEmailTests(SimpleTestCase):
    def test_keeps_first_character_and_full_domain(self):
        self.assertEqual(
            mask_email("dana@hoffmanplumbing.example"),
            "d••••@hoffmanplumbing.example",
        )

    def test_empty_and_missing_at_pass_through_unchanged(self):
        self.assertEqual(mask_email(""), "")
        self.assertEqual(mask_email("not-an-email"), "not-an-email")
