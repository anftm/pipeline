"""Independent v6 token/anchor and varint contract tests."""
import hashlib
import random
import unittest

from scripts import ccrd_postings as postings
from scripts import ccrd_text_features as features


def digest(text):
    return hashlib.sha256(text.encode()).digest()[:16]


class CcrdTextFeatureTests(unittest.TestCase):
    def test_fullwidth_cjk_latin_whitespace_and_literal_anchors(self):
        text = 'ＡＢ 中国，中国\n2026'
        value = features.normalized(text)
        self.assertEqual(value, 'ab 中国,中国\n2026')
        anchors = features.anchors(text)
        self.assertEqual(anchors[digest('ab')], 0)
        self.assertEqual(anchors[digest('中')], 3)
        self.assertEqual(anchors[digest('中国')], 3)
        self.assertEqual(anchors[digest('2026')], 9)
        self.assertIn(digest('\0literal:' + value[1:4]), anchors)
        self.assertTrue(set(anchors) <= features.token_hashes(text))
        self.assertEqual(features.summarize(' a\n  b  '), 'a b')

    def test_nul_is_replaced_for_fts_but_not_posting_literal_stream(self):
        text = 'ab\0cd'
        self.assertEqual(features.normalized(text), 'ab cd')
        literal_before = digest('\0literal:b\0c')
        literal_after = digest('\0literal:b c')
        self.assertIn(literal_before, features.token_hashes(text))
        self.assertIn(literal_after, features.anchors(text))

    def test_canonical_posting_codec_roundtrips_random_strict_sets(self):
        randomizer = random.Random(127)
        for _ in range(100):
            values = sorted(randomizer.sample(range(500_000), randomizer.randrange(0, 100)))
            raw = postings.encode(values)
            self.assertEqual(postings.decode(raw, expected_count=len(values), maximum=100), values)
        self.assertEqual(postings.encode([0, 127, 128]), b'\x00\x7f\x01')

    def test_corrupt_varints_and_invalid_ids_are_rejected(self):
        for raw, count in ((b'\x80', 1), (b'\x80\x00', 1), (b'\x00\x00', 2), (b'\x01', 2),
                           (b'\xff' * 11 + b'\x01', 1)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                postings.decode(raw, expected_count=count, maximum=10)
        for ids in ([1, 1], [-1], [True], [postings.MAX_DOC_NUMBER + 1]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                postings.encode(ids)


if __name__ == '__main__':
    unittest.main()
