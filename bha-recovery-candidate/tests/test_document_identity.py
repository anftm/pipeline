"""Versioned preview-baseline tests; synthetic documents only."""

import copy
import hashlib
import json
import unittest

from app.document_identity import content_hash
from app import parse_api, proofread_api
from fastapi.responses import JSONResponse


def prior_content_only_hash(source):
    value = {"article": source.get("article") or {}, "title": source.get("title"),
             "publication_name": source.get("publication_name"), "publication_author": source.get("publication_author"),
             "publication_type": source.get("publication_type"), "source_files": source.get("source_files") or []}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DocumentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.source = {"doc_id": "3:article:publication", "archive_id": 3, "article_id": "article",
                       "publication_id": "publication", "title": "标题", "publication_name": "来源",
                       "publication_author": "甲", "publication_type": "book", "source_files": ["one"],
                       "article": {"parts": [{"type": "paragraph", "text": "正文"}], "comments": []}}

    def test_each_identity_and_editable_content_field_changes_baseline(self):
        baseline = content_hash(self.source)
        changes = {"doc_id": "other", "archive_id": 4, "article_id": "other", "publication_id": "other",
                   "title": "other", "publication_name": "other", "publication_author": "other",
                   "publication_type": "other", "source_files": ["other"], "article": {"parts": []}}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(baseline, content_hash({**self.source, field: value}))
        self.assertEqual(len(baseline), 64)
        self.assertEqual(baseline, content_hash(dict(reversed(list(self.source.items())))))

    def test_previous_hash_is_not_a_valid_new_baseline_even_if_content_is_equal(self):
        previous = prior_content_only_hash(self.source)
        self.assertNotEqual(previous, content_hash(self.source))
        for field, value in (("doc_id", "other"), ("archive_id", 4), ("article_id", "other"), ("publication_id", "other")):
            with self.subTest(field=field):
                changed = {**self.source, field: value}
                self.assertEqual(previous, prior_content_only_hash(changed))
                self.assertNotEqual(content_hash(self.source), content_hash(changed))

    def test_irrelevant_auxiliary_projection_does_not_change_baseline(self):
        self.assertEqual(content_hash(self.source), content_hash({**self.source, "variants": ["other"], "content": "derived"}))

    def test_non_finite_source_fails_closed(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                content_hash({**self.source, "article": {"value": value}})

    def test_each_identity_change_rejects_previous_hash_for_both_request_types(self):
        proof = {**self.source, "doc_id": "proof", "title": "fixture",
                 "article": {"parts": [{"type": "paragraph", "text": "old"}], "comments": []}}
        split = {**self.source, "doc_id": "parse", "archive_id": 25, "title": "【文章待拆分】fixture",
                 "article": {"page_start": 1, "page_end": 1}}
        proof_body = proofread_api.ProofreadRequest(doc_id="proof", base_content_hash=content_hash(proof),
                    patch={"version": 2, "parts": {"0": {"diff": "-3\t+new"}}, "comments": {}})
        parse_body = parse_api.ParseRequest(doc_id="parse", base_content_hash=content_hash(split),
                    articles=[{"title": "split", "page_start": 1, "page_end": 1, "content": "text", "base_part_count": 1}])
        for body, source, prepare in ((proof_body, proof, proofread_api.prepare_proofread_payload),
                                      (parse_body, split, parse_api.prepare_parse_payload)):
            for key, new_value in (("doc_id", "different"), ("archive_id", 4), ("article_id", "different"),
                                   ("publication_id", "different")):
                with self.subTest(kind=type(body).__name__, key=key):
                    altered = copy.deepcopy(source)
                    altered[key] = new_value
                    self.assertNotEqual(content_hash(altered), body.base_content_hash)
                    result = prepare(body, altered)
                    self.assertIsInstance(result, JSONResponse)
                    self.assertIn(result.status_code, (404, 409))


if __name__ == "__main__":
    unittest.main(verbosity=2)
