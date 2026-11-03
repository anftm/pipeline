import hashlib
import unittest

from scripts import chm_epub_bundle_integrity as integrity


class ChmBundleIntegrityTests(unittest.TestCase):
    def test_bundle_fails_when_chapter_or_resource_changes(self):
        files = {'chapter-manifest.json': b'manifest', 'chapters/1.xhtml': b'body', 'resources/1.png': b'image'}
        digest = hashlib.sha256()
        for name, raw in sorted(files.items()):
            digest.update(name.encode() + b'\0')
            digest.update(hashlib.sha256(raw).digest())
        root = 'chapters/ebook/chm/' + 'a' * 64 + '/' + digest.hexdigest()[:16]
        objects = {root + '/' + path: raw for path, raw in files.items()}
        class Store:
            def read_bytes(self, bucket, path):return objects[path]
        report = integrity.verify_bundle(Store(), 'bucket', root, list(objects), workers=2)
        self.assertEqual(report['objects'], 3)
        objects[root + '/resources/1.png'] = b'changed'
        with self.assertRaisesRegex(ValueError, 'checksum differs'):
            integrity.verify_bundle(Store(), 'bucket', root, list(objects), workers=2)

    def test_unrelated_path_is_rejected_before_reads(self):
        root = 'chapters/ebook/chm/' + 'a' * 64 + '/' + 'b' * 16
        with self.assertRaisesRegex(ValueError, 'escaped'):
            integrity.verify_bundle(None, 'bucket', root, [root + '/../other'])
