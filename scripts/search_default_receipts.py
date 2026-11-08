"""Observe a site's explicitly configured search-data build default.

A variable read is evidence of configuration only; it does not prove the
running site loaded a generation or that the variable cannot change later.
"""
from __future__ import annotations

from datetime import UTC, datetime

try:
    from . import search_snapshot
    from .search_publication_pointer import _generation
except ImportError:
    import search_snapshot
    from search_publication_pointer import _generation


VARIABLE = "SEARCH_DATA_GENERATION"
PAGES = "vomebook/search"
SPACE = "VoiceOfML/Search"


def from_observation(site: str, generation: str, observed: str, *, timestamp: str | None = None) -> bytes:
    _generation(generation)
    _generation(observed)
    if site not in {"pages", "space"}:
        raise ValueError("invalid search site")
    if generation != observed:
        raise ValueError("configured search default does not match expected generation")
    time = timestamp or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not isinstance(time, str) or not time or len(time) > 64:
        raise ValueError("invalid default observation time")
    return search_snapshot.canonical({"version": 1, "kind": "search-default-observation", "site": site,
                                      "generation": generation, "observed_at": time,
                                      "details": {"variable": VARIABLE}})


def observe_pages(generation: str, *, session, timestamp: str | None = None) -> bytes:
    """Read the explicit GitHub Actions variable; a missing variable is not a default receipt."""
    url = f"https://api.github.com/repos/{PAGES}/actions/variables/{VARIABLE}"
    response = session.get(url, timeout=20)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("name") != VARIABLE:
        raise ValueError("invalid Pages variable response")
    return from_observation("pages", generation, body.get("value"), timestamp=timestamp)


def observe_space(generation: str, *, api, timestamp: str | None = None) -> bytes:
    """Read the explicit Space variable without printing any unrelated configuration."""
    values = api.get_space_variables(SPACE)
    item = values.get(VARIABLE) if isinstance(values, dict) else None
    if item is None:
        raise ValueError("Space search default variable is missing")
    return from_observation("space", generation, getattr(item, "value", None), timestamp=timestamp)
