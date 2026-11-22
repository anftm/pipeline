import hashlib
from types import SimpleNamespace as Result
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import reader_mht_s3_preflight as task


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class HostedPreflightTests(TestCase):
    def setUp(self):
        blobs = {task.INDEX: b'index', task.POINTER: ('{"generation":"' + task.GEN + '"}').encode(),
                 task.SIDECAR: b'packed sidecar', task.CATALOG: b'catalog',
                 task.OLD_ROOT+'document.html': b'html', task.OLD_ROOT+'document-manifest.json': b'manifest'}
        self.hub = Mock()
        self.hub.read_bytes.side_effect = lambda bucket, path: blobs[path]
        self.s3 = Mock()
        self.s3.read_bytes.side_effect = lambda bucket, path: blobs[path]
        self.s3._client.return_value.head_object.side_effect = lambda Bucket, Key: {
            'ETag': '"same"', 'ContentLength': len(blobs[Key])}
        self.index = Mock()
        self.index.read.return_value = Result(raw=blobs[task.INDEX], raw_sha256=digest(blobs[task.INDEX]), etag='"same"')
        self.pointer = Mock()
        self.pointer.read_for_update.return_value = blobs[task.POINTER], '"same"'
        self.legacy = Mock()
        from scripts.reader_image_history_publish_s3 import Observed
        self.legacy.observe.side_effect = lambda path: Observed(digest(blobs[path]),len(blobs[path]),'"same"')
        keys = dict(catalog=task.CATALOG, sidecar=task.SIDECAR, index=task.INDEX,
                    old_html=task.OLD_ROOT+'document.html', old_manifest=task.OLD_ROOT+'document-manifest.json')
        self.pins = {name:(digest(blobs[path]),len(blobs[path])) for name,path in keys.items()}

    def run_review(self):
        with patch.object(task, 'PINS', self.pins):
            return task.review(self.index,self.pointer,self.legacy,self.hub,self.s3)

    def test_authority_is_observed_without_put(self):
        result = self.run_review()
        self.assertEqual(result['remote_writes'],0)
        self.assertIs(result['publication_ready'],False)
        self.assertEqual(result['catalog']['etag'],'"same"')
        self.s3._client.return_value.put_object.assert_not_called()

    def test_legacy_drift_fails_closed(self):
        self.s3._client.return_value.head_object.side_effect = None
        self.s3._client.return_value.head_object.return_value = {'ETag':'"different"','ContentLength':0}
        with self.assertRaisesRegex(ValueError,'legacy root changed'):
            self.run_review()

    def test_missing_parent_fails_closed(self):
        self.index.read.return_value = Result(raw=None)
        with self.assertRaisesRegex(ValueError,'missing reviewed MHT input'):
            self.run_review()
