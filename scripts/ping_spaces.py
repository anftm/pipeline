#!/usr/bin/env python3
"""Read-only Space checks; a BHA homepage HTTP 200 is not search readiness."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.client import HTTPException
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SpaceCheck:
    name: str
    url: str
    bha_health: bool = False
    wait_seconds: float = 90


SPACES = (
    SpaceCheck("Hitokoto", "https://vomebook-hitokoto.hf.space/"),
    SpaceCheck("HF Search", "https://voiceofml-search.hf.space/"),
    SpaceCheck("CCRD Search", "https://vomebook-search.hf.space/"),
    SpaceCheck("BHA Search", "https://vomebook-bha-search.hf.space/api/health", True, 300),
)
REQUEST_TIMEOUT_SECONDS = 30
RETRY_DELAY_SECONDS = 15
MAX_HEALTH_BYTES = 64 * 1024


def bha_ready(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("ok") is True
        and payload.get("index_ready") is True
        and payload.get("index_status") == "ready"
        and payload.get("es") is True
    )


def probe(space: SpaceCheck, timeout: float) -> tuple[bool, bool, str]:
    """Return (healthy, retryable, diagnostic), without credentials or mutations."""
    request = Request(space.url, headers={
        "User-Agent": "pipeline-space-keepalive",
        "Cache-Control": "no-cache",
        "Accept": "application/json" if space.bha_health else "text/html",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            if status != 200:
                return False, status == 429 or 500 <= status < 600, f"HTTP {status}"
            if not space.bha_health:
                return True, False, "HTTP 200"
            body = response.read(MAX_HEALTH_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        return False, status == 429 or 500 <= status < 600, f"HTTP {status}"
    except (URLError, OSError, HTTPException) as error:
        return False, True, f"request failed ({type(error).__name__})"

    if len(body) > MAX_HEALTH_BYTES:
        return False, True, "HTTP 200; oversized BHA health response"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return False, True, "HTTP 200; invalid BHA health JSON"
    if not bha_ready(payload):
        return False, True, "HTTP 200; BHA search is not ready"
    return True, False, "HTTP 200; BHA ok=true, index_ready=true, index_status=ready, es=true"


def check_space(space: SpaceCheck) -> bool:
    deadline = time.monotonic() + space.wait_seconds
    attempt = 0
    while (remaining := deadline - time.monotonic()) > 0:
        attempt += 1
        healthy, retryable, diagnostic = probe(space, min(REQUEST_TIMEOUT_SECONDS, remaining))
        print(f"{space.name} attempt {attempt}: {diagnostic}", flush=True)
        if healthy:
            return True
        if not retryable:
            print(f"FAILED: {space.name} returned a permanent HTTP error", flush=True)
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(RETRY_DELAY_SECONDS, remaining))
    print(f"FAILED: {space.name} not healthy within {space.wait_seconds:g}s", flush=True)
    return False


def main() -> int:
    # Independent checks must not let one cold or failing Space suppress the rest.
    with ThreadPoolExecutor(max_workers=len(SPACES)) as executor:
        results = list(executor.map(check_space, SPACES))
    failed = [space.name for space, healthy in zip(SPACES, results) if not healthy]
    if failed:
        print("Failed Space checks: " + ", ".join(failed), flush=True)
        return 1
    print("All four Space checks passed; BHA search is ready.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
