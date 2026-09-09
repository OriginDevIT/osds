"""``directory.suppression.fingerprint`` — the suppression-key hash.

Issue #184: the CSV importer (read side) and the future ``listing.deleted``
path (write side) must produce identical hashes for the same business, or a
removed listing reappears on the next upload. One implementation, and the hash
for a fixed input is pinned below so neither side can drift from it.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from directory.suppression import fingerprint

# ---------------------------------------------------------------------------
# The normalisation is FROZEN. A SuppressionKey row stores only the hash, and
# the listing it came from is deleted, so its components can never be recovered
# to recompute. Widening the normalisation later silently stops matching every
# key already written — a removed business quietly reappears. Changing any of
# these pinned values is a data decision (issue + migration story), not a
# refactor. #184.
# ---------------------------------------------------------------------------
_FULL = "543b27b5a620b7ed4f56483b4c8160005a815382898b2b6cec044f2a4277d2ea"
_NO_ADDRESS = "095261932d155d48c4f1c68ebe3eff2b9ac6c50dd62a4d1f356b17bc02d835ca"
_NO_PHONE = "391503e074005ce710a87472dfcb6acc53583f2aab26d3fa3bdfa010319d5751"
_NAME_ONLY = "f3d6606eb21c5d6c405de547b113b38f5da020acdc864fe1f9708b980eadda74"

_HOFFMAN = dict(
    name="Hoffman Plumbing",
    address_line1="1422 W Belmont Ave",
    locality="Chicago",
    region="IL",
    postal_code="60657",
    country="US",
    phone="+1 (773) 555-0142",
)


class FingerprintPinTests(SimpleTestCase):
    def test_full_input_is_pinned(self):
        self.assertEqual(fingerprint(**_HOFFMAN), _FULL)

    def test_missing_address_is_pinned_and_distinct(self):
        got = fingerprint(name=_HOFFMAN["name"], phone=_HOFFMAN["phone"])
        self.assertEqual(got, _NO_ADDRESS)
        self.assertNotEqual(got, _FULL)

    def test_missing_phone_is_pinned_and_distinct(self):
        kw = {k: v for k, v in _HOFFMAN.items() if k != "phone"}
        got = fingerprint(**kw)
        self.assertEqual(got, _NO_PHONE)
        self.assertNotEqual(got, _FULL)

    def test_name_only_is_pinned(self):
        self.assertEqual(fingerprint(name=_HOFFMAN["name"]), _NAME_ONLY)


class FingerprintNormalisationTests(SimpleTestCase):
    def test_case_and_whitespace_are_folded(self):
        self.assertEqual(
            fingerprint(name="  HOFFMAN   plumbing ", phone="+1 (773) 555-0142"),
            _NO_ADDRESS,
        )

    def test_phone_punctuation_is_stripped_but_plus_is_significant(self):
        with_plus = fingerprint(name="x", phone="+1 (773) 555-0142")
        without_plus = fingerprint(name="x", phone="1-773-555-0142")
        self.assertEqual(with_plus, fingerprint(name="x", phone="+17735550142"))
        self.assertEqual(without_plus, fingerprint(name="x", phone="17735550142"))
        self.assertNotEqual(with_plus, without_plus)

    def test_address_component_order_is_fixed(self):
        a = fingerprint(name="x", address_line1="1 Main St", locality="Springfield")
        b = fingerprint(name="x", address_line1="Springfield", locality="1 Main St")
        self.assertNotEqual(a, b)

    def test_absent_and_blank_components_are_the_same(self):
        self.assertEqual(
            fingerprint(name="x", locality=None),
            fingerprint(name="x", locality="   "),
        )

    def test_blank_name_raises(self):
        for bad in (None, "", "   ", "\t"):
            with self.assertRaises(ValueError):
                fingerprint(name=bad, phone="+17735550142")

    def test_output_fits_the_key_hash_column(self):
        # SuppressionKey.key_hash is CharField(max_length=64).
        self.assertEqual(len(fingerprint(**_HOFFMAN)), 64)
