import io
import unittest
import zipfile
from unittest.mock import patch

from scripts import reader_image_history_package as package
from tests import test_reader_image_history_runtime as fixture


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.RuntimeTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.patch = patch.multiple(package, REVIEW=self.fixture.digest, OPERATION='a' * 32)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Unit fixtures can use any shard. The deployed wrapper deliberately
        # accepts only the reviewed physical shard through the production pack.

    def members(self):
        r = self.fixture.review_inputs
        return {name: (self.fixture.dir / name).read_bytes() for name in package.FILE_LIMITS} | {
            'canonical.parent.json': r['canonical_parent'], 'shard.parent.json': r['shard_parent'],
            'sidecar.parent.gz': r['before_sidecar'], 'pointer.parent.json': r['pointer_parent'],
            'sidecar.parent.etag': r['sidecar_etag'].encode('ascii')}

    def make(self, members):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return raw, package.image._sha(raw.getvalue())

    def test_exact_package_load_matches_original_review(self):
        raw, digest = self.make(self.members())
        self.assertEqual(package.load(raw, expected_sha256=digest), self.fixture.reviewed)

    def test_bad_zip_digest_allowlist_and_inner_artifact_are_rejected(self):
        raw, _ = self.make(self.members())
        with self.assertRaisesRegex(ValueError, 'package SHA-256'):
            package.load(raw, expected_sha256='f' * 64)
        raw, digest = self.make(self.members() | {'../foreign': b'foreign'})
        with self.assertRaisesRegex(ValueError, 'unapproved entries'):
            package.load(raw, expected_sha256=digest)
        raw, digest = self.make(self.members() | {'document.avif': b'changed'})
        with self.assertRaisesRegex(ValueError, 'reviewed AVIF'):
            package.load(raw, expected_sha256=digest)
