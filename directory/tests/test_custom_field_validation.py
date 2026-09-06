"""directory.field_schema.validate_custom_fields."""

from __future__ import annotations

from django.test import SimpleTestCase

from directory.field_schema import SchemaError, validate_custom_fields


class _Type:
    def __init__(self, fields):
        self.fields = fields


F_YEARS = {"key": "years", "label": "Years", "type": "integer", "required": False}
F_NAME = {"key": "contact_name", "label": "Contact", "type": "text", "required": True}
F_PLAN = {
    "key": "plan",
    "label": "Plan",
    "type": "select",
    "options": ["basic", "pro"],
    "required": False,
}


class CustomFieldValidationTests(SimpleTestCase):
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(SchemaError) as ctx:
            validate_custom_fields(_Type([F_YEARS]), {"nope": 1}, creating=True)
        self.assertIn("not a field on this listing type", str(ctx.exception))

    def test_required_enforced_on_create(self):
        with self.assertRaises(SchemaError):
            validate_custom_fields(_Type([F_NAME]), {}, creating=True)

    def test_required_not_enforced_on_update_when_key_absent(self):
        self.assertEqual(
            validate_custom_fields(_Type([F_NAME]), {}, creating=False), {}
        )

    def test_required_enforced_on_update_when_key_present_and_blank(self):
        with self.assertRaises(SchemaError):
            validate_custom_fields(
                _Type([F_NAME]), {"contact_name": "  "}, creating=False
            )

    def test_enforce_required_off_for_csv(self):
        self.assertEqual(
            validate_custom_fields(
                _Type([F_NAME]), {}, creating=True, enforce_required=False
            ),
            {},
        )

    def test_integer_coercion_and_error(self):
        self.assertEqual(
            validate_custom_fields(_Type([F_YEARS]), {"years": "12"}, creating=True),
            {"years": 12},
        )
        with self.assertRaises(SchemaError):
            validate_custom_fields(_Type([F_YEARS]), {"years": "12.5"}, creating=True)

    def test_select_must_be_in_options(self):
        self.assertEqual(
            validate_custom_fields(_Type([F_PLAN]), {"plan": "pro"}, creating=True),
            {"plan": "pro"},
        )
        with self.assertRaises(SchemaError):
            validate_custom_fields(
                _Type([F_PLAN]), {"plan": "enterprise"}, creating=True
            )

    def test_explicit_none_clears(self):
        self.assertEqual(
            validate_custom_fields(
                _Type([F_YEARS]), {"years": None}, creating=False
            ),
            {"years": None},
        )

    def test_blank_string_is_treated_as_none(self):
        self.assertEqual(
            validate_custom_fields(
                _Type([F_YEARS]), {"years": ""}, creating=False
            ),
            {"years": None},
        )

    def test_decimal_and_date_stored_as_strings(self):
        t = _Type(
            [
                {"key": "rate", "label": "Rate", "type": "decimal"},
                {"key": "since", "label": "Since", "type": "date"},
            ]
        )
        out = validate_custom_fields(
            t, {"rate": "3.50", "since": "2020-01-02"}, creating=True
        )
        self.assertEqual(out, {"rate": "3.50", "since": "2020-01-02"})
