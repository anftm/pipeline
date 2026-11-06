"""Synthetic in-memory CW models; no production index or source files."""

from copy import deepcopy
import random
import unittest

from scripts import ccrd_single_source_cw_shadow as shadow


class CwIndexShadowTests(unittest.TestCase):
    def setUp(self):
        self.documents = {'CW:0': '旧文 猫。 abc\0Ｄ', 'CW:1': '另一只猫 abc',
                          'CW:127': '边界文档 café', 'CW:128': '第二边界猫', 'CW:1000': '孤立文本'}
        self.old = shadow.build(self.documents)

    def compare_rebuild(self, new):
        candidate = shadow.replace_target(self.old, self.documents['CW:0'], new,
                                          expected_ids=set(self.documents))
        expected = shadow.build({**self.documents, 'CW:0': new})
        self.assertEqual(candidate, expected)
        self.assertFalse(candidate['publication_ready'])
        self.assertEqual(candidate['remote_writes'], 0)
        self.assertEqual(self.old, shadow.build(self.documents))
        return candidate

    def test_update_matches_full_rebuild_and_preserves_other_documents(self):
        result = self.compare_rebuild('新内容 猫！ ABC\0Ａ 日本語')
        for doc_id in set(self.documents) - {'CW:0'}:
            index = shadow.number(doc_id)
            self.assertEqual(result['documents'][doc_id], self.old['documents'][doc_id])
            self.assertEqual(result['content_fts'][index], self.old['content_fts'][index])
            self.assertEqual(result['snippet_anchors'][index], self.old['snippet_anchors'][index])
        for token in set(result['postings']) | set(self.old['postings']):
            before = set(shadow.decode_doc_ids(self.old['postings'].get(token, b''))) - {0}
            after = set(shadow.decode_doc_ids(result['postings'].get(token, b''))) - {0}
            self.assertEqual(before, after)

    def test_randomized_inputs_match_full_rebuild(self):
        randomizer = random.Random(1729)
        alphabet = '甲乙猫狗 abcABC09。！？\n\0Ａ'
        for _ in range(80):
            new = ''.join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(0, 90)))
            with self.subTest(new=new):
                self.compare_rebuild(new)

    def test_noop_and_same_size_replacement(self):
        self.assertEqual(self.compare_rebuild(self.documents['CW:0']), self.old)
        self.compare_rebuild('x' * len(self.documents['CW:0']))

    def test_posting_encoding_checks_zero_large_ids_and_corruption(self):
        numbers = [0, 1, 127, 128, 129, 1000, shadow.MAX_NUMBER]
        encoded = shadow.encode_doc_ids(numbers)
        self.assertEqual(shadow.decode_doc_ids(encoded), numbers)
        for value in (b'\x80', b'\x80\x00', b'\x00\x00', b'\xff' * 11):
            with self.subTest(value=value), self.assertRaises(ValueError):
                shadow.decode_doc_ids(value)

    def test_inconsistent_base_or_missing_target_fails_closed(self):
        for mode in ('old-text', 'ids', 'fts', 'anchors', 'posting', 'missing-token', 'extra-doc'):
            old = deepcopy(self.old)
            current, ids = self.documents['CW:0'], set(self.documents)
            if mode == 'old-text':
                current = 'not the current content'
            elif mode == 'ids':
                ids.remove('CW:1')
            elif mode == 'fts':
                old['content_fts'][0] = 'corrupted'
            elif mode == 'anchors':
                old['snippet_anchors'][0] = {}
            elif mode == 'posting':
                old['postings'][next(iter(old['postings']))] = b'\x80'
            elif mode == 'missing-token':
                token = next(iter(shadow.token_hashes(current)))
                del old['postings'][token]
            else:
                old['content_fts'][999999] = 'unexpected'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                shadow.replace_target(old, current, 'new content', expected_ids=ids)

    def test_invalid_document_ids_are_rejected(self):
        for value in ('CW:-1', 'CW:01', 'OTHER:0', 'CW:9223372036854775808'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                shadow.build({'CW:0': 'x', value: 'y'})


if __name__ == '__main__':
    unittest.main()
