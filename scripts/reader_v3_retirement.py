"""Retire acknowledged v3 history roots without mutating immutable generations."""

from datetime import datetime, timedelta, timezone
import json

try:
    from . import publish_reader_v3 as publication
except ImportError:
    import publish_reader_v3 as publication

STATE = "reader-index/v3/retirement.json"


def run(store, *, apply=False, now=None):
    pointer, _ = publication.current(store)
    if not pointer:
        return {"applied": apply, "retired": []}
    history = json.loads(store.read_bytes(publication.ASSETS,
        f"reader-index/v3/promotions/{pointer['generation']}.json"))
    if history.get("acks") != {"hf": True, "pages": True}:
        return {"applied": apply, "retired": [], "blocked": "consumer-acknowledgments"}
    try:
        state = json.loads(store.read_bytes(publication.ASSETS, STATE))
    except FileNotFoundError:
        state = {"version": 1, "kind": "reader-v3-retirement", "retired_paths": [], "resources": []}
    if state.get("kind") != "reader-v3-retirement" or state.get("version") != 1:
        raise ValueError("invalid retirement state")
    retired = set(state["retired_paths"])
    now = now or datetime.now(timezone.utc)
    for path in sorted(store.list_files(publication.ASSETS, ("reader-index/v3/promotions/",))):
        if path == f"reader-index/v3/promotions/{pointer['generation']}.json" or path in retired:
            continue
        record = json.loads(store.read_bytes(publication.ASSETS, path))
        if record.get("kind") != "reader-reading-promotion" or record.get("acks") != {"hf": True, "pages": True}:
            continue
        created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or now - created < timedelta(days=record.get("retention_days", 30)):
            continue
        generation = record["generation"]
        catalog_path = f"reader-index/v3/generations/{generation}/catalog.json"
        if catalog_path == pointer["catalog"]["path"]:
            continue
        retired.update((path, catalog_path))
    state.update(retired_paths=sorted(retired), replacement_generation=pointer["generation"],
                 resources=[pointer["catalog"]], replacement_acks=history["acks"])
    if apply:
        publication.assert_writer(store)
        if publication.current(store)[0] != pointer:
            raise ValueError("retirement pointer changed")
        raw = publication.encode(state)
        store.put_bytes(publication.ASSETS, STATE, raw)
        if store.read_bytes(publication.ASSETS, STATE) != raw:
            raise ValueError("retirement journal readback failed")
    return {"applied": apply, "retired": sorted(retired)}
