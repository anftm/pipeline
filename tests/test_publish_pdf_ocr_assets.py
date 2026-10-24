import unittest

from scripts import publish_pdf_ocr_assets as publish


class OcrLanguageAnnotationTests(unittest.TestCase):
    def test_mismatched_text_is_marked_suspect(self):
        result = {"key": "repo\0book.pdf", "ocr_language": "ru", "language_source": "filename"}
        publish.annotate_language(result, {"pages": [{"text": "Latin only text " * 60}]})
        self.assertTrue(result["language_suspect"])
        self.assertEqual(result["language_check"]["observed_script"], "Latin")

    def test_matching_text_is_not_marked(self):
        result = {"key": "repo\0book.pdf", "ocr_language": "ru", "language_source": "filename"}
        publish.annotate_language(result, {"pages": [{"text": "Русский текст " * 60}]})
        self.assertNotIn("language_suspect", result)

    def test_explicit_override_is_trusted(self):
        result = {"key": "repo\0book.pdf", "ocr_language": "ru", "language_source": "override"}
        publish.annotate_language(result, {"pages": [{"text": "Latin only text " * 60}]})
        self.assertNotIn("language_suspect", result)


if __name__ == "__main__":
    unittest.main()