import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts import pilot_image_page_stream as stream


class ImageAssetStreamTests(unittest.TestCase):
    def test_gif_is_published_as_one_static_webp_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.gif"
            first = Image.new("RGB", (100, 60), "red")
            second = Image.new("RGB", (100, 60), "blue")
            first.save(source, save_all=True, append_images=[second], duration=100, loop=0)
            original = stream.download
            stream.download = lambda _url, target, _token: target.write_bytes(source.read_bytes())
            try:
                entry, uploads = stream.build_one(
                    {"repo": "repo", "path": "book.gif", "extension": "gif",
                     "bytes": source.stat().st_size, "revision": "revision"},
                    root / "work", None, "bucket")
            finally:
                stream.download = original
            self.assertEqual(entry["mode"], "image")
            self.assertEqual(entry["page_count"], 1)
            self.assertEqual(len(uploads), 1)
            remote, local = next(iter(uploads.items()))
            self.assertTrue(remote.endswith("/document.webp"))
            with Image.open(local) as image:
                self.assertEqual(image.n_frames, 1)
                red, green, blue = image.convert("RGB").getpixel((0, 0))
                self.assertGreater(red, blue)

    def test_tiff_stays_on_the_pdf_path(self):
        self.assertIn("gif", stream.SUPPORTED_EXTENSIONS)
        self.assertNotIn("tif", stream.SUPPORTED_EXTENSIONS)
        self.assertNotIn("tiff", stream.SUPPORTED_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
