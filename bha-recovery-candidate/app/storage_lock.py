import fcntl
import errno
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from .config import DATA_ROOT

LOCK_PATH = DATA_ROOT / "serving.lock"
SNAPSHOT_PUBLISH_LOCK_PATH = DATA_ROOT / "bucket-snapshot-publish.lock"


@contextmanager
def _filesystem_lock(path: Path, operation: int) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), operation)
    except OSError as exc:
        lock_file.close()
        if exc.errno not in {errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise
        raise RuntimeError("filesystem lock requires flock support") from exc
    try:
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

@contextmanager
def serving_lock(exclusive: bool = False) -> Generator[None, None, None]:
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with _filesystem_lock(LOCK_PATH, operation):
        yield


@contextmanager
def snapshot_publish_lock() -> Generator[None, None, None]:
    with _filesystem_lock(SNAPSHOT_PUBLISH_LOCK_PATH, fcntl.LOCK_EX):
        yield
