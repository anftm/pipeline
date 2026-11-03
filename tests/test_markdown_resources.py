import io
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import markdown_resources as resources

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class MarkdownResourceTests(unittest.TestCase):
    def test_inline_image_is_embedded_and_cached(self):
        calls = []
        def read(path):
            calls.append(path)
            return PNG
        text = "before ![alt](images/a.png \"title\") after"
        output, images = resources.embed_markdown_images(text, "book/doc.md", read)
        self.assertIn("![alt](data:image/png;base64,", output)
        self.assertEqual(calls, ["book/images/a.png"])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].path, "book/images/a.png")
        self.assertEqual(images[0].mime, "image/png")

    def test_html_image_src_is_embedded_without_changing_other_attributes(self):
        text = '<img alt="cover" src="images/a.png" width="64">'
        output, images = resources.embed_markdown_images(
            text, "book/doc.md", lambda path: PNG
        )
        self.assertIn('alt="cover"', output)
        self.assertIn('width="64"', output)
        self.assertIn('src="data:image/png;base64,', output)
        self.assertEqual(len(images), 1)

    def test_duplicate_reference_reads_once_but_reports_each_reference(self):
        calls = []
        text = "![a](images/a.png)\n![b](images/a.png)"
        output, images = resources.embed_markdown_images(
            text, "book/doc.md", lambda path: calls.append(path) or PNG
        )
        self.assertEqual(calls, ["book/images/a.png"])
        self.assertEqual(output.count("data:image/png;base64,"), 2)
        self.assertEqual(len(images), 2)

    def test_root_level_document_can_use_its_own_images_directory(self):
        output, images = resources.embed_markdown_images(
            "![a](images/a.png)", "doc.md", lambda path: PNG
        )
        self.assertIn("data:image/png;base64,", output)
        self.assertEqual(images[0].path, "images/a.png")

    def test_traversal_absolute_remote_and_non_http_references_fail_closed(self):
        references = [
            "../secret.png",
            "../../etc/passwd",
            "/etc/passwd",
            "file:///etc/passwd",
            "//example.invalid/image.png",
            "images/a.png?x=1",
            "images/a.png#fragment",
            "images\\a.png",
        ]
        for reference in references:
            with self.subTest(reference=reference), self.assertRaises(resources.MarkdownResourceError):
                resources.embed_markdown_images(
                    f"![a]({reference})", "book/doc.md", lambda path: PNG
                )

    def test_remote_http_images_are_left_as_links(self):
        def local(path):
            if path == "book/images/b.png":
                return PNG
            raise AssertionError("remote images must not be fetched")
        text = "![a](https://example.invalid/image.png) ![b](images/b.png)"
        output, embedded = resources.embed_markdown_images(text, "book/doc.md", local)
        self.assertIn("![a](https://example.invalid/image.png)", output)
        self.assertIn("data:image/png;base64,", output)
        self.assertEqual([image.path for image in embedded], ["book/images/b.png"])

    def test_missing_and_unsupported_images_fail_closed(self):
        def missing(path):
            raise FileNotFoundError(path)
        with self.assertRaisesRegex(resources.MarkdownResourceError, "missing image"):
            resources.embed_markdown_images("![a](images/a.png)", "book/doc.md", missing)
        with self.assertRaisesRegex(resources.MarkdownResourceError, "unsupported image type"):
            resources.embed_markdown_images("![a](images/a.txt)", "book/doc.md", lambda path: PNG)

    def test_image_size_limits_fail_closed(self):
        with patch.object(resources, "MAX_IMAGE_BYTES", 4):
            with self.assertRaisesRegex(resources.MarkdownResourceError, "per-file limit"):
                resources.embed_markdown_images("![a](images/a.png)", "book/doc.md", lambda path: PNG)
        with patch.object(resources, "MAX_TOTAL_IMAGE_BYTES", 30):
            with self.assertRaisesRegex(resources.MarkdownResourceError, "total embedding limit"):
                resources.embed_markdown_images(
                    "![a](images/a.png)\n![b](images/b.png)", "book/doc.md", lambda path: PNG
                )

    def test_bmp_is_converted_to_png(self):
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), (255, 0, 0)).save(buffer, "BMP")
        bmp = buffer.getvalue()
        output, images = resources.embed_markdown_images(
            "![a](images/a.bmp)", "book/doc.md", lambda path: bmp
        )
        self.assertIn("data:image/png;base64,", output)
        self.assertEqual(images[0].mime, "image/png")

    def test_plain_markdown_is_unchanged(self):
        text = "# Title\n\nNo local images here.\n"
        output, images = resources.embed_markdown_images(text, "book/doc.md", lambda path: PNG)
        self.assertEqual(output, text)
        self.assertEqual(images, [])


if __name__ == "__main__":
    unittest.main()
