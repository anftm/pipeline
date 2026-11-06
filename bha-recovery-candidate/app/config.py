import os
from collections.abc import Mapping
from pathlib import Path


def parsed_repo_prefix(environment: Mapping[str, str]) -> str:
    return environment.get("BHA_PARSED_REPO_PREFIX", "https://github.com/anftm").rstrip("/")


APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(APP_ROOT / "data")))
PARSED_ROOT = DATA_ROOT / "parsed"
INDEX_NAME = os.environ.get("ES_INDEX", "article")
ES_URL = os.environ.get("ES_URL", "http://127.0.0.1:9200")
REPO_PREFIX = parsed_repo_prefix(os.environ)
ARCHIVE_START = int(os.environ.get("ARCHIVE_START", "0"))
ARCHIVE_END = int(os.environ.get("ARCHIVE_END", "31"))
RESET_INDEX = os.environ.get("RESET_INDEX", "0") == "1"
# Read-only sidecar connections map the database file instead of copying pages
# through SQLite's page cache. The window is lazy: it costs address space, not
# resident memory, and the OS drops clean pages under pressure. 0 disables it.
def _sqlite_mmap_bytes() -> int:
    try:
        return max(0, int(os.environ.get("BHA_SQLITE_MMAP_BYTES", str(256 * 1024 * 1024))))
    except ValueError:
        return 256 * 1024 * 1024


SQLITE_MMAP_BYTES = _sqlite_mmap_bytes()
INDEX_VERSION = "1.0.0"
