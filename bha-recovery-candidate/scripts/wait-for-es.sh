#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import os
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

url = os.environ.get("ES_URL", "http://127.0.0.1:9200")
attempts = int(os.environ.get("ES_WAIT_ATTEMPTS", "120"))
interval = float(os.environ.get("ES_WAIT_INTERVAL_SECONDS", "2"))

for _ in range(attempts):
    try:
        with urlopen(url, timeout=min(1.0, max(0.1, interval))) as response:
            if response.status < 400:
                raise SystemExit(0)
    except (OSError, URLError):
        pass
    time.sleep(interval)

print("Elasticsearch did not become ready in time", file=sys.stderr)
raise SystemExit(1)
PY
