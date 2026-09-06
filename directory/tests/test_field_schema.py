"""directory.field_schema.validate_type_schema -- rejection cases and the
frozen-field-type rule.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from directory.field_schema import normalize_type_schema, validate_type_schema


def _field(**over):
    base = {"key": "years", "label": "Years", "type": "integer"}
    base.update(over)
    return base


class ValidTests(SimpleTestCase):
    def test_empty_schema_is_valid(self):
        self.assertEqual(validate_type_schema([]), [])

    def test_minimal_field_is_valid(self):
        self.assertEqual(validate_type_schema([_field()]), [])

    def test_select_with_options_is_valid(self):
        self.assertEqual(
            validate_type_schema(
                [_field(key="plan", label="Plan", type="select", options=["a", "b"])]
            ),
            [],
        )

    def test_searchable_allowed_on_text(self):
        self.assertEqual(
            validate_type_schema(
                [_field(key="notes", label="Notes", type="text", searchable=True)]
            ),
            [],
        )


class RejectionTests(SimpleTestCase):
    def _err(self, fields):
        errors = validate_type_schema(fields)
        self.assertTrue(errors, f"expected rejection, got none for {fields}")
        return " ".join(errors)

    def test_schema_must_be_a_list(self):
        self.assertEqual(
            validate_type_schema({"key": "x"}),
            ["schema must be a list of field descriptors"],
        )

    def test_unknown_type(self):
        self.assertIn("type must be one of", self._err([_field(type="phone")]))

    def test_missing_key(self):
        self.assertIn("key is required", self._err([{"label": "L", "type": "text"}]))

    def test_missing_label(self):
        self.assertIn("label is required", self._err([{"key": "x", "type": "text"}]))

    def test_non_slug_key(self):
        self.assertIn("lowercase letters", self._err([_field(key="Years In Business")]))
        self.assertIn("lowercase letters", self._err([_field(key="1st")]))

    def test_reserved_key(self):
        self.assertIn("reserved field name", self._err([_field(key="status")]))
        self.assertIn("reserved field name", self._err([_field(key="categories")]))

    def test_duplicate_key(self):
        self.assertIn(
            "duplicate key", self._err([_field(key="a", label="A"), _field(key="a", label="B")])
        )

    def test_select_without_options(self):
        self.assertIn("needs a non-empty options list", self._err([_field(type="select")]))
        self.assertIn(
            "needs a non-empty options list", self._err([_field(type="multi_select", options=[])])
        )

    def test_options_on_non_choice_type(self):
        self.assertIn(
            "options are only valid on select",
            self._err([_field(type="text", options=["a"])]),
        )

    def test_options_must_be_non_empty_unique_strings(self):
        self.assertIn("non-empty strings", self._err([_field(type="select", options=["a", "  "])]))
        self.assertIn("must be unique", self._err([_field(type="select", options=["a", "a"])]))

    def test_searchable_on_disallowed_type(self):
        self.assertIn(
            "can be searchable", self._err([_field(type="integer", searchable=True)])
        )
        self.assertIn(
            "can be searchable", self._err([_field(type="date", searchable=True)])
        )

    def test_bool_flag_must_be_bool(self):
        self.assertIn("required must be true or false", self._err([_field(required="yes")]))

    def test_errors_accumulate(self):
        errors = validate_type_schema([_field(key="STATUS", type="nope")])
        self.assertGreaterEqual(len(errors), 2)


class FrozenFieldTypeTests(SimpleTestCase):
    PREVIOUS = [{"key": "years", "label": "Years", "type": "integer"}]

    def test_retyping_an_existing_field_is_blocked(self):
        errors = validate_type_schema(
            [_field(key="years", label="Years", type="text")], previous=self.PREVIOUS
        )
        self.assertTrue(any("type is frozen" in e for e in errors))
        self.assertTrue(any("was 'integer'" in e for e in errors))

    def test_same_type_is_fine(self):
        self.assertEqual(
            validate_type_schema(
                [_field(key="years", label="Years renamed", type="integer")],
                previous=self.PREVIOUS,
            ),
            [],
        )

    def test_removing_a_field_is_allowed(self):
        self.assertEqual(validate_type_schema([], previous=self.PREVIOUS), [])

    def test_readding_a_removed_key_with_its_original_type_is_allowed(self):
        # values were orphaned, not deleted; re-adding restores them (ruling 5)
        self.assertEqual(
            validate_type_schema(
                [_field(key="years", label="Years", type="integer")],
                previous=[],
            ),
            [],
        )

    def test_new_field_alongside_a_frozen_one(self):
        self.assertEqual(
            validate_type_schema(
                [
                    _field(key="years", label="Years", type="integer"),
                    _field(key="years_text", label="Years (text)", type="text"),
                ],
                previous=self.PREVIOUS,
            ),
            [],
        )


class NormalizeTests(SimpleTestCase):
    def test_fills_bool_flags_and_trims_label(self):
        out = normalize_type_schema([{"key": "a", "label": "  A  ", "type": "text"}])
        self.assertEqual(
            out, [{"key": "a", "label": "A", "type": "text", "required": False, "public": True, "searchable": False}]
        )

    def test_drops_options_on_non_choice_types(self):
        out = normalize_type_schema([{"key": "a", "label": "A", "type": "text", "options": ["x"]}])
        self.assertNotIn("options", out[0])

    def test_keeps_and_trims_options_on_choice_types(self):
        out = normalize_type_schema(
            [{"key": "a", "label": "A", "type": "select", "options": [" x ", "y", ""]}]
        )
        self.assertEqual(out[0]["options"], ["x", "y"])
