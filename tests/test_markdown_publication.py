import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pilot_text_stream as stream

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class MarkdownPublicationTests(unittest.TestCase):
    def test_markdown_images_are_embedded_with_stable_profile(self):
        item = {"repo": "VoiceOfML/Test", "path": "a/doc.md", "extension": "md", "revision": "a" * 40}
        source = b"![x](images/x.png)\n"

        def fake_download(url, target, token, max_bytes=None):
            if url.endswith("/a/doc.md"):
                target.write_bytes(source)
            elif url.endswith("/a/images/x.png"):
                target.write_bytes(PNG)
            else:
                raise AssertionError(url)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "download", side_effect=fake_download):
            entry, files = stream.build_one(item, Path(directory), None, stream.TARGET_BUCKET)
            output_bytes = Path(files[entry["object"]]).read_bytes()
            manifest = json.loads(Path(files[entry["manifest"]]).read_text(encoding="utf-8"))

        self.assertEqual(entry["profile"], stream.MARKDOWN_IMAGE_PROFILE)
        self.assertEqual(entry["embedded_images"], 1)
        self.assertEqual(entry["embedded_image_paths"], ["a/images/x.png"])
        self.assertIn(b"data:image/png;base64,", output_bytes)
        self.assertEqual(manifest["profile"], stream.MARKDOWN_IMAGE_PROFILE)

    def test_markdown_without_images_uses_the_same_stable_profile(self):
        item = {"repo": "VoiceOfML/Test", "path": "a/plain.md", "extension": "md", "revision": "a" * 40}
        source = b"# Title\n\nNo images.\n"

        def fake_download(url, target, token, max_bytes=None):
            target.write_bytes(source)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "download", side_effect=fake_download):
            entry, _files = stream.build_one(item, Path(directory), None, stream.TARGET_BUCKET)

        self.assertEqual(entry["profile"], stream.MARKDOWN_IMAGE_PROFILE)
        self.assertNotIn("embedded_images", entry)

    def test_markdown_image_traversal_fails_closed(self):
        item = {"repo": "VoiceOfML/Test", "path": "a/doc.md", "extension": "md", "revision": "a" * 40}
        source = b"![x](../secret.png)\n"

        def fake_download(url, target, token, max_bytes=None):
            target.write_bytes(source)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "download", side_effect=fake_download):
            with self.assertRaisesRegex(ValueError, "Markdown resource error"):
                stream.build_one(item, Path(directory), None, stream.TARGET_BUCKET)

    def test_missing_markdown_image_fails_closed(self):
        item = {"repo": "VoiceOfML/Test", "path": "a/doc.md", "extension": "md", "revision": "a" * 40}
        source = b"![x](images/missing.png)\n"

        def fake_download(url, target, token, max_bytes=None):
            if url.endswith("/a/doc.md"):
                target.write_bytes(source)
            else:
                raise FileNotFoundError(url)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(stream, "download", side_effect=fake_download):
            with self.assertRaisesRegex(ValueError, "Markdown resource error"):
                stream.build_one(item, Path(directory), None, stream.TARGET_BUCKET)

    def test_old_markdown_profile_is_pending_but_new_profile_is_complete(self):
        records = [
            {"repo": "VoiceOfML/Test", "path": "a.md", "extension": "md", "revision": "a" * 40},
            {"repo": "VoiceOfML/Test", "path": "b.txt", "extension": "txt", "revision": "a" * 40},
        ]
        old = {"files": [
            {"key": "VoiceOfML/Test\0a.md", "profile": stream.PROFILE, "source_revision": "a" * 40},
            {"key": "VoiceOfML/Test\0b.txt", "profile": stream.PROFILE, "source_revision": "a" * 40},
        ]}
        self.assertEqual(stream.pending_records(records, old), [records[0]])
        new = {"files": [
            {"key": "VoiceOfML/Test\0a.md", "profile": stream.MARKDOWN_IMAGE_PROFILE, "source_revision": "a" * 40},
            {"key": "VoiceOfML/Test\0b.txt", "profile": stream.PROFILE, "source_revision": "a" * 40},
        ]}
        self.assertEqual(stream.pending_records(records, new), [])


if __name__ == "__main__":
    unittest.main()
