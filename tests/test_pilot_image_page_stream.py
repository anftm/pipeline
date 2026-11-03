import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from scripts import pilot_image_page_stream as stream


class ImageAssetStreamTests(unittest.TestCase):
    def test_unchanged_lfs_source_is_reused_but_changed_and_unknown_sources_are_built(self):
        rows = [{"repo": "repo", "path": name + ".jpg", "extension": "jpg", "bytes": 10,
                 "revision": "new"} for name in ("unchanged", "changed", "unknown")]
        previous = {"files": [{"key": f"repo\0{name}.jpg", "source_revision": "old",
                               "source_sha256": digest, "path": "pages/image/jpg/" + digest + "/document.webp"}
                              for name, digest in (("unchanged", "a" * 64), ("changed", "b" * 64),
                                                   ("unknown", "c" * 64))]}
        remote = [SimpleNamespace(path="unchanged.jpg", lfs=SimpleNamespace(sha256="a" * 64)),
                  SimpleNamespace(path="changed.jpg", lfs=SimpleNamespace(sha256="d" * 64)),
                  SimpleNamespace(path="unknown.jpg", lfs=None)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / "revisions.json"
            revisions.write_text('{}')
            args = SimpleNamespace(limit=0, max_source_bytes=0, shard_count=1, shard_index=0,
                                   revisions=revisions, extension="all", bucket="bucket", rebuild=False,
                                   search_data=root / "search.json", apply=False, strict=True)
            built = []
            def build(item, work, _token, _bucket):
                built.append(item['path'])
                return {"key": f"repo\0{item['path']}"}, {}
            with patch.object(stream, 'parse_args', return_value=args), \
                 patch.object(stream, 'source_records', return_value=rows), \
                 patch.object(stream, 'existing_index', return_value=previous), \
                 patch('scripts.reader_stream_index.HfApi') as api, \
                 patch.object(stream, 'build_one', side_effect=build):
                api.return_value.get_paths_info.return_value = remote
                self.assertEqual(stream.main(), 0)
            self.assertEqual(built, ['changed.jpg', 'unknown.jpg'])

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

    def test_single_frame_tiff_is_published_as_one_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            Image.new("RGB", (120, 80), "red").save(source, format="TIFF")
            original = stream.download
            stream.download = lambda _url, target, _token: target.write_bytes(source.read_bytes())
            try:
                entry, uploads = stream.build_one(
                    {"repo": "repo", "path": "scan.tif", "extension": "tif",
                     "bytes": source.stat().st_size, "revision": "revision"},
                    root / "work", None, "bucket")
            finally:
                stream.download = original
            self.assertEqual(entry["mode"], "image")
            self.assertEqual(entry["page_count"], 1)
            self.assertTrue(next(iter(uploads)).endswith("/document.webp"))

    def test_multipage_tiff_is_published_as_an_image_page_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            first = Image.new("RGB", (120, 80), "red")
            second = Image.new("RGB", (120, 80), "blue")
            first.save(source, format="TIFF", save_all=True, append_images=[second])
            original = stream.download
            stream.download = lambda _url, target, _token: target.write_bytes(source.read_bytes())
            try:
                entry, uploads = stream.build_one(
                    {"repo": "repo", "path": "scan.tif", "extension": "tif",
                     "bytes": source.stat().st_size, "revision": "revision"},
                    root / "work", None, "bucket")
                manifest = json.loads((root / "work" / "page-manifest.json").read_text())
            finally:
                stream.download = original
            self.assertEqual(entry["mode"], "page-stream")
            self.assertEqual(entry["page_count"], 2)
            self.assertEqual(manifest["kind"], "image-page-stream")
            self.assertEqual([page["path"] for page in manifest["pages"]],
                             ["pages/page-000001.webp", "pages/page-000002.webp"])
            self.assertTrue(next(iter(uploads)).endswith("/pages/page-000001.webp"))


if __name__ == "__main__":
    unittest.main()
