"""Adopt an already verified two-site search generation as the desired root.

Read-only planning is the default. An apply requires independently observed
Space and Pages generation evidence, exact bytes from the immutable manifest,
and an explicit operator declaration that the known search writers are stopped.
That declaration is not evidence that unknown external credentials are fenced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
import uuid

try:
    from . import search_snapshot
    from .reader_bucket_store import HubBucketStore
    from .search_publication_pointer import (
        S3SearchPointerStore,
        _generation,
        apply_pointer,
    )
except ImportError:
    import search_snapshot
    from reader_bucket_store import HubBucketStore
    from search_publication_pointer import (
        S3SearchPointerStore,
        _generation,
        apply_pointer,
    )


BUCKET = "vomebook/search-data-v1"
SPACE = "https://voiceofml-search.hf.space"
PAGES = "https://vomebook.github.io/search"
MAX_JSON = 1024 * 1024
MAX_CORPUS = 32 * 1024 * 1024


def bounded_get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Vomebook-Search-Cutover/1.0",
                                                   "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.status != 200:
            raise ValueError(f"unexpected response: {response.status}")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response exceeds cutover verification limit")
    return data


def validate_generation(hub, generation: str) -> dict:
    _generation(generation)
    raw = hub.read_bytes(BUCKET, f"generations/{generation}/manifest.json")
    if not isinstance(raw, bytes) or len(raw) > MAX_JSON:
        raise ValueError("missing or oversized search generation manifest")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or manifest.get("kind") != "search-snapshot-local" or manifest.get("generation") != generation
            or manifest.get("object_layout") != "objects/sha256/<first-two>/<sha256>"):
        raise ValueError("invalid search generation manifest")
    excluded = {"generation", "remote_writes", "publication_ready", "object_layout", "objects", "additional_checks"}
    identity = {key: value for key, value in manifest.items() if key not in excluded}
    if hashlib.sha256(search_snapshot.canonical(identity)).hexdigest() != generation:
        raise ValueError("search generation identity mismatch")
    if type(manifest.get("record_count")) is not int or manifest["record_count"] < 1:
        raise ValueError("invalid search record count")
    objects = manifest.get("objects")
    files = manifest.get("files")
    if not isinstance(objects, dict) or not isinstance(files, dict) or set(objects) != {"pages", "space"}:
        raise ValueError("invalid search surfaces")
    unique = {}
    for surface in ("pages", "space"):
        members, expected = objects[surface], files.get(surface)
        if not isinstance(members, dict) or not isinstance(expected, dict) or set(members) != set(expected):
            raise ValueError("search generation member mismatch")
        for name, item in members.items():
            search_snapshot.safe_relative(name)
            if not isinstance(item, dict) or set(item) != {"sha256", "bytes", "object"}:
                raise ValueError("invalid search member")
            digest, size = item["sha256"], item["bytes"]
            if (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                    or type(size) is not int or not 1 <= size <= MAX_CORPUS
                    or item["object"] != f"objects/sha256/{digest[:2]}/{digest}"
                    or expected[name] != {"sha256": digest, "bytes": size}):
                raise ValueError("invalid search member identity")
            unique[item["object"]] = (digest, size)
    for key, (digest, size) in sorted(unique.items()):
        data = hub.read_bytes(BUCKET, key)
        if not isinstance(data, bytes) or len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"search object readback mismatch: {key}")
    return manifest


def observe_consumers(generation: str, manifest: dict, *, get=bounded_get) -> dict:
    space = json.loads(get(SPACE + "/api/search-data-generation", MAX_JSON))
    pages_manifest_raw = get(PAGES + "/data/generation-manifest.json", MAX_JSON)
    pages_manifest = json.loads(pages_manifest_raw)
    if (not isinstance(space, dict) or space.get("generation") != generation or space.get("surface") != "space"
            or space.get("verified") is not True or space.get("records") != manifest["record_count"]):
        raise ValueError("Space has not loaded the expected generation")
    if (not isinstance(pages_manifest, dict) or pages_manifest.get("generation") != generation
            or pages_manifest.get("files") != manifest.get("files")
            or pages_manifest.get("objects") != manifest.get("objects")):
        raise ValueError("Pages does not advertise the expected immutable generation")
    corpus = get(PAGES + "/data/search_data.json.gz", MAX_CORPUS)
    expected = manifest["files"]["pages"]["search_data.json.gz"]
    if len(corpus) != expected["bytes"] or hashlib.sha256(corpus).hexdigest() != expected["sha256"]:
        raise ValueError("Pages loaded corpus bytes do not match the generation")
    initial = json.loads(get(PAGES + "/data/initial/global.json", MAX_JSON))
    if not isinstance(initial, dict) or initial.get("total") != manifest["record_count"]:
        raise ValueError("Pages initial payload count differs")
    return {"space_generation": space["generation"], "space_api_generation": space.get("api_generation"),
            "pages_generation": pages_manifest["generation"], "pages_corpus_sha256": expected["sha256"],
            "pages_initial_total": initial["total"]}


def adopt(generation: str, *, apply: bool, writers_stopped: bool, hub, pointer_store=None, get=bounded_get) -> dict:
    manifest = validate_generation(hub, generation)
    observed = observe_consumers(generation, manifest, get=get)
    result = {"kind": "search-generation-adoption", "generation": generation, "observed": observed,
              "apply": False, "pointer_written": False, "external_writers_verified": False}
    if not apply:
        return result
    if writers_stopped is not True or pointer_store is None:
        raise ValueError("apply requires declared known-writer coordination and S3 access")
    raw, _etag = pointer_store.read_for_update()
    if raw is not None:
        raise ValueError("search pointer already exists; inspect it before any transition")
    transition = uuid.uuid4().hex
    print(json.dumps({"kind": "search-adoption-intent", "generation": generation,
                      "expected_previous": None, "transition": transition}, sort_keys=True), flush=True)
    written = apply_pointer(pointer_store, desired=generation, expected=None, transition=transition)
    return {**result, "apply": True, "pointer_written": True, "pointer": written,
            "known_writers_declared_stopped": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--known-writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    hub = HubBucketStore()
    store = S3SearchPointerStore() if args.apply else None
    result = adopt(args.generation, apply=args.apply, writers_stopped=args.known_writers_stopped,
                   hub=hub, pointer_store=store)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
