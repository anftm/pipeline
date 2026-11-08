"""Space build-variable adapter with explicit observation and no retry.

HF Space variable writes have no compare-and-swap in this adapter.  A pre-read
only detects changes already visible; it does not fence an external writer or
an ABA.  The caller must coordinate writers and persist the intent first.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

try:
    from .search_default_receipts import SPACE, VARIABLE, observe_space
    from .search_publication_pointer import _generation
except ImportError:
    from search_default_receipts import SPACE, VARIABLE, observe_space
    from search_publication_pointer import _generation


SHA = re.compile(r"[0-9a-f]{40}\Z")


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("invalid Space variable update timestamp") from error
    else:
        raise TypeError("invalid Space variable update timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Space variable update timestamp must be UTC")
    return parsed


def variable(api) -> tuple[str, str]:
    values = api.get_space_variables(SPACE)
    item = values.get(VARIABLE) if isinstance(values, dict) else None
    value = getattr(item, "value", None)
    updated = getattr(item, "updated_at", None)
    _generation(value)
    timestamp = _timestamp(updated).astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value, timestamp


def runtime(api, *, expected_sha: str) -> dict:
    if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
        raise ValueError("invalid expected Space commit")
    info = api.space_info(SPACE, timeout=20)
    state = api.get_space_runtime(SPACE)
    raw = getattr(state, "raw", {})
    if info.sha != expected_sha or not isinstance(raw, dict) or raw.get("sha") != expected_sha:
        raise ValueError("Space repository or running revision differs")
    if state.stage != "RUNNING":
        raise ValueError("Space is not running")
    return {"sha": expected_sha, "stage": state.stage}


def set_generation(api, *, expected: str, expected_updated_at: str, target: str, expected_sha: str) -> None:
    """Single Space configuration write; uncertain responses are never replayed."""
    _generation(expected)
    _generation(target)
    previous_timestamp = _timestamp(expected_updated_at)
    if expected == target:
        raise ValueError("Space target generation is already configured")
    runtime(api, expected_sha=expected_sha)
    configured, updated = variable(api)
    if configured != expected or _timestamp(updated) != previous_timestamp:
        raise ValueError("Space generation configuration changed before update")
    # A lost response is unknown: read variable, runtime and consumer before
    # recovery; do not issue another mutation from this call.
    api.add_space_variable(SPACE, key=VARIABLE, value=target)
    configured, updated = variable(api)
    if configured != target or _timestamp(updated) < _timestamp(expected_updated_at):
        raise RuntimeError("Space configuration readback differs; inspect before recovery")


def inspect(api, *, expected: str, expected_sha: str, expected_updated_after: str) -> bytes:
    """Return a configuration receipt only when the exact Space build is running."""
    _generation(expected)
    runtime(api, expected_sha=expected_sha)
    configured, updated = variable(api)
    if configured != expected or _timestamp(updated) < _timestamp(expected_updated_after):
        raise ValueError("Space variable does not match the expected update")
    return observe_space(expected, api=api)
