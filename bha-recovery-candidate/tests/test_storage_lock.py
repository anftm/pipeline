import errno
import fcntl
import unittest
from unittest.mock import patch

from app.storage_lock import serving_lock, snapshot_publish_lock


class StorageLockTests(unittest.TestCase):
    def test_rejects_filesystem_without_process_locking(self):
        with patch("app.storage_lock.fcntl.flock", side_effect=OSError(errno.ENOSYS, "unsupported")):
            with self.assertRaisesRegex(RuntimeError, "flock support"):
                with serving_lock():
                    pass

    def test_snapshot_publication_uses_exclusive_process_lock(self):
        with patch("app.storage_lock.fcntl.flock") as flock:
            with snapshot_publish_lock():
                pass
        self.assertEqual(flock.call_args_list[0].args[1], fcntl.LOCK_EX)


if __name__ == "__main__":
    unittest.main()
