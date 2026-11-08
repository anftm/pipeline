"""Read and verify the actual loaded search generation for each site.

Evidence is a canonical, bounded observation suitable for durable storage; a
successful request at one moment does not fence later site or external writes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

try:
    from . import search_snapshot
    from .search_adopt import MAX_CORPUS, MAX_JSON, PAGES, SPACE, bounded_get
    from .search_publication_pointer import _generation
except ImportError:
    import search_snapshot
    from search_adopt import MAX_CORPUS, MAX_JSON, PAGES, SPACE, bounded_get
    from search_publication_pointer import _generation


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid consumer JSON") from error
    if not isinstance(data, dict):
        raise TypeError("consumer JSON is not an object")
    return data


def _manifest(generation: str, manifest: dict) -> tuple[dict, dict, int]:
    _generation(generation)
    if not isinstance(manifest, dict) or manifest.get("generation") != generation:
        raise ValueError("expected manifest generation mismatch")
    files = manifest.get("files")
    objects = manifest.get("objects")
    count = manifest.get("record_count")
    if (not isinstance(files, dict) or not isinstance(objects, dict)
            or not isinstance(files.get("pages"), dict) or not isinstance(objects.get("space"), dict)
            or type(count) is not int or count < 1):
        raise ValueError("invalid consumer manifest metadata")
    return files, objects, count


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def observe(site: str, generation: str, manifest: dict, *, get=bounded_get, timestamp=None) -> bytes:
    files, objects, count = _manifest(generation, manifest)
    if site == "pages":
        details = _pages(generation, manifest, files, count, get)
    elif site == "space":
        details = _space(generation, objects, count, get)
    else:
        raise ValueError("invalid search consumer site")
    time = timestamp or _timestamp()
    if not isinstance(time, str) or not time or len(time) > 64:
        raise ValueError("invalid observation timestamp")
    return search_snapshot.canonical({"version": 1, "kind": "search-consumer-observation", "site": site,
                                      "generation": generation, "records": count, "observed_at": time,
                                      "details": details})


def _pages(generation: str, manifest: dict, files: dict, count: int, get) -> dict:
    deployed = _json(get(PAGES + "/data/generation-manifest.json", MAX_JSON))
    if (deployed.get("generation") != generation or deployed.get("files") != manifest.get("files")
            or deployed.get("objects") != manifest.get("objects")):
        raise ValueError("Pages manifest does not match the expected generation")
    expected = files["pages"].get("search_data.json.gz")
    if not isinstance(expected, dict):
        raise TypeError("Pages manifest has no corpus")
    corpus = get(PAGES + "/data/search_data.json.gz", MAX_CORPUS)
    digest = hashlib.sha256(corpus).hexdigest()
    if len(corpus) != expected.get("bytes") or digest != expected.get("sha256"):
        raise ValueError("Pages corpus does not match the expected generation")
    initial = _json(get(PAGES + "/data/initial/global.json", MAX_JSON))
    if initial.get("total") != count:
        raise ValueError("Pages initial payload count differs")
    return {"corpus_sha256": digest, "initial_total": initial["total"],
            "manifest_sha256": hashlib.sha256(search_snapshot.canonical(deployed)).hexdigest()}


def _space(generation: str, objects: dict, count: int, get) -> dict:
    actual = _json(get(SPACE + "/api/search-data-generation", MAX_JSON))
    if (actual.get("generation") != generation or actual.get("surface") != "space"
            or actual.get("verified") is not True or actual.get("records") != count
            or actual.get("files") != len(objects["space"]) or not isinstance(actual.get("api_generation"), str)
            or not actual["api_generation"]):
        raise ValueError("Space has not verified the expected generation")
    return {"files": actual["files"], "api_generation": actual["api_generation"]}
