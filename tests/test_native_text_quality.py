import math
import unittest

from scripts import native_text_quality as quality


def payload(text, boxes=None):
    return {'text': text, 'blocks': [{'t': text, 'b': box} for box in (boxes or [[.1, .1, .9, .2]])]}


class NativeTextQualityTests(unittest.TestCase):
    def test_mathematical_greek_in_latin_prose_is_not_a_substitution_alphabet(self):
        value = payload('Mathematical expression αβγ ∀δε × ζηθ ∃ ' * 20)
        self.assertTrue(quality.assess(value, 'en', True)['usable'])

    def test_corrupt_cyrillic_hidden_layer_is_rejected(self):
        value = payload('αβγ ∀δε × ζηθ ∃ ' * 20)
        report = quality.assess(value, 'ru', True)
        self.assertFalse(report['usable'])
        self.assertIn('native-language-script-conflict', report['reasons'])
        self.assertIn('native-substitution-alphabet', report['reasons'])

    def test_control_and_replacement_characters_keep_offsets(self):
        report = quality.assess(payload('visible\x01bad\ufffd'))
        self.assertFalse(report['usable'])
        self.assertEqual(report['invalid_character_offsets'], [7, 11])

    def test_absent_invalid_and_overlapping_coordinates_cannot_pass(self):
        for value in [{'text': 'text', 'blocks': []}, payload('text', [[.1, .1, .1, .2]]),
                      payload('text', [[.1, .1, math.nan, .2]]),
                      payload('text', [[.1, .1, .8, .2], [.11, .1, .79, .2]])]:
            with self.subTest(value=value):
                self.assertFalse(quality.assess(value)['usable'])

    def test_explicit_font_rejection_is_not_overruled_by_readable_text(self):
        value = {**payload('CONTENTS'), 'native_evidence': {'reasons': ['unverified-type3-font']}}
        self.assertFalse(quality.assess(value)['usable'])
        self.assertTrue(quality.assess(payload('xii CONTENTS'))['usable'])
