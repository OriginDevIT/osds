"""osds.ids -- ULID shape and the per-entity prefix factories.

The low 80 bits are random by design, so these tests deliberately do NOT assert
any ordering between ids minted in the same millisecond, and the factory is not
monotonic.
"""

from __future__ import annotations

import time
import unittest

from osds import ids

_CROCKFORD32 = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

# (factory, expected prefix) -- every public ``*_id`` helper in osds.ids.
_FACTORIES = [
    (ids.tnt_id, "tnt_"),
    (ids.op_id, "op_"),
    (ids.lt_id, "lt_"),
    (ids.cat_id, "cat_"),
    (ids.listing_id, "listing_"),
    (ids.usr_id, "usr_"),
    (ids.claim_id, "claim_"),
    (ids.lead_id, "lead_"),
    (ids.cns_id, "cns_"),
    (ids.tier_id, "tier_"),
    (ids.ent_id, "ent_"),
    (ids.imp_id, "imp_"),
    (ids.media_id, "media_"),
]


class NewUlidTests(unittest.TestCase):
    def test_is_26_characters(self):
        for _ in range(2000):
            self.assertEqual(len(ids.new_ulid()), 26)

    def test_uses_only_the_crockford_base32_alphabet(self):
        for _ in range(2000):
            self.assertLessEqual(set(ids.new_ulid()), _CROCKFORD32)

    def test_first_character_stays_within_128_bits(self):
        # 26 * 5 = 130 encoded bits; a 128-bit value never sets the top two, so
        # the leading character is 0-7.
        for _ in range(2000):
            self.assertIn(ids.new_ulid()[0], set("01234567"))

    def test_values_do_not_collide(self):
        seen = {ids.new_ulid() for _ in range(20000)}
        self.assertEqual(len(seen), 20000)

    def test_sorts_by_time_across_milliseconds(self):
        samples = []
        for _ in range(5):
            samples.append(ids.new_ulid())
            time.sleep(0.003)  # comfortably past the 1 ms timestamp tick
        self.assertEqual(samples, sorted(samples))
        self.assertEqual(len(set(samples)), len(samples))


class PrefixFactoryTests(unittest.TestCase):
    def test_each_factory_prepends_its_prefix_to_a_26_char_ulid(self):
        for factory, prefix in _FACTORIES:
            value = factory()
            self.assertTrue(
                value.startswith(prefix), (factory.__name__, value)
            )
            body = value[len(prefix):]
            self.assertEqual(len(body), 26, factory.__name__)
            self.assertLessEqual(set(body), _CROCKFORD32, factory.__name__)

    def test_prefixes_are_distinct(self):
        prefixes = [p for _, p in _FACTORIES]
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_every_public_id_factory_is_covered(self):
        # If a new `<x>_id` helper is added to osds/ids.py, add it to _FACTORIES.
        in_module = {
            name
            for name in dir(ids)
            if name.endswith("_id") and callable(getattr(ids, name))
        }
        covered = {factory.__name__ for factory, _ in _FACTORIES}
        self.assertEqual(in_module, covered)
