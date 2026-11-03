"""Retry read-only GitHub API requests across transient gateway failures."""

import http.client
import json
import time
import urllib.error
import urllib.request


RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def json_request(request, attempts=4, retry_seconds=1, timeout=30):
    if request.get_method() != "GET":
        raise ValueError("github_read only accepts GET requests")
    error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError:
                detail = {"message": raw}
            if exc.code not in RETRYABLE_HTTP_STATUSES or attempt + 1 == attempts:
                return exc.code, detail
            error = exc
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            ConnectionError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            error = exc
            if attempt + 1 == attempts:
                raise
        time.sleep(retry_seconds * (2 ** attempt))
    raise AssertionError(f"unreachable after GitHub request failure: {error}")


def json_request_or_fail(request, label, attempts=4, retry_seconds=1, timeout=30):
    try:
        status, data = json_request(request, attempts, retry_seconds, timeout)
    except Exception as exc:
        raise RuntimeError(f"{label} failed after {attempts} attempts: {exc}") from exc
    if status != 200:
        if status in RETRYABLE_HTTP_STATUSES:
            raise RuntimeError(f"{label} failed after {attempts} attempts: HTTP {status}")
        raise RuntimeError(f"{label} failed with HTTP {status}")
    return data
