import unittest
from unittest.mock import Mock, patch

from scripts import reader_ebook_rebuild_publish as subject


class ReleaseGuardTests(unittest.TestCase):
    @patch.object(subject.v3, 'current', return_value=({'generation':'parent'}, {}))
    def test_only_protected_immutable_writes_share_bounded_fence(self, current):
        hub, fence, clock = object(), Mock(return_value=True), Mock(return_value=10)
        parent, target = {'generation':'parent'}, {'generation':'target'}
        guard = subject.release_guard(hub, parent, target, {}, fence=fence, now=clock)
        for i in range(16):
            self.assertTrue(guard('stage:member-put'))
        self.assertEqual(fence.call_count, 1)
        self.assertTrue(guard('stage:member-put'))
        self.assertEqual(fence.call_count, 2)
        clock.return_value = 30
        self.assertTrue(guard('stage:member-put'))
        self.assertEqual(fence.call_count, 3)
        fence.return_value = False
        self.assertFalse(guard('canonical:put'))
        self.assertFalse(guard('stage:member-put'))
        self.assertEqual(fence.call_count, 5)

    @patch.object(subject.pages, 'current')
    @patch.object(subject.v3, 'current')
    def test_pointer_commit_rechecks_approved_pages_proof(self, current, pages):
        parent, target, proof = {'generation':'parent'}, {'generation':'target'}, {'code':1}
        current.return_value = (parent, {})
        guard = subject.release_guard(object(), parent, target, proof, fence=lambda *_:True)
        pages.return_value = proof
        self.assertTrue(guard('v3:write-or-readback'))
        pages.assert_called_once_with(parent, expected=proof)
        pages.return_value = {'code':2}
        self.assertFalse(guard('v3:write-or-readback'))
        current.return_value = (target, {})
        self.assertTrue(guard('v3:write-or-readback'))
        self.assertFalse(guard('legacy:readback'))
