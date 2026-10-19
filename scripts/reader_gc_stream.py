"""Stream graph-relevant JSON without retaining OCR text or positioned spans."""
import gzip
import hashlib
import io
import re


SKIP_FIELDS = {"text", "text_spans", "layout", "blocks", "error", "source_url", "source_path",
               "source", "observations"}


class HashingReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.buffer = b""
        self.hash = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, target):
        while not self.buffer:
            try:
                self.buffer = next(self.chunks)
                self.hash.update(self.buffer)
            except StopIteration:
                return 0
        size = min(len(target), len(self.buffer))
        target[:size] = self.buffer[:size]
        self.buffer = self.buffer[size:]
        return size


def graph_json(chunks, compressed=False):
    import ijson
    source = HashingReader(chunks)
    stream = io.BufferedReader(source)
    decoded = gzip.GzipFile(fileobj=stream) if compressed else stream
    builder = ijson.ObjectBuilder()
    skip_value, skip_depth, possible_text_resource = False, 0, False
    for prefix, event, value in ijson.parse(decoded, use_float=True):
        if skip_depth:
            skip_depth += event in {"start_map", "start_array"}
            skip_depth -= event in {"end_map", "end_array"}
        elif possible_text_resource:
            possible_text_resource = False
            if event == "start_map":
                builder.event("map_key", "text")
                builder.event(event, value)
            else:
                skip_depth = int(event == "start_array")
        elif skip_value:
            skip_value = False
            skip_depth = int(event in {"start_map", "start_array"})
        elif event == "map_key" and value == "text" and not prefix:
            possible_text_resource = True
        elif (event == "map_key" and value in SKIP_FIELDS
              and not (value == "text" and prefix.rsplit(".", 1)[-1] == "components")):
            skip_value = True
        else:
            builder.event(event, value)
    while stream.read(65536):
        pass
    if not isinstance(builder.value, dict):
        raise ValueError("expected a JSON object")
    return builder.value, source.hash.hexdigest()


def catalog_json(chunks, compressed=False, grace_days=0):
    """Stream a Reader catalog keeping every generation id but only live references.

    Superseded, acknowledged generations are reduced to identity stubs. Live
    generations keep only references not already kept by another live
    generation, so the walk still visits every live reference exactly once.
    """
    import ijson
    try:
        from .reader_lifecycle import generation_live
    except ImportError:
        from reader_lifecycle import generation_live
    source = HashingReader(chunks)
    stream = io.BufferedReader(source)
    decoded = gzip.GzipFile(fileobj=stream) if compressed else stream
    top = ijson.ObjectBuilder()
    generations, seen = {}, set()
    current, name, entry, depth = None, None, None, 0
    for prefix, event, value in ijson.parse(decoded, use_float=True):
        if prefix == "current.generation" and event == "string":
            current = value
        if entry is None and prefix == "generations":
            if event == "map_key":
                name = value
                continue
            if event in {"start_map", "end_map"}:
                top.event(event, value)
                continue
        if entry is not None or prefix == f"generations.{name}" and name is not None \
                and prefix.startswith("generations."):
            if entry is None:
                entry, depth = ijson.ObjectBuilder(), 0
            entry.event(event, value)
            depth += event in {"start_map", "start_array"}
            depth -= event in {"end_map", "end_array"}
            if depth:
                continue
            item, entry = entry.value, None
            if isinstance(item, dict):
                live = current is None or generation_live(item, current, grace_days=grace_days)
                if live and isinstance(item.get("references"), list):
                    kept = []
                    for ref in item["references"]:
                        identity = (ref.get("bucket"), ref.get("path")) if isinstance(ref, dict) else None
                        if identity is None or identity not in seen:
                            kept.append(ref)
                            if identity is not None:
                                seen.add(identity)
                    item["references"] = kept
                elif not live:
                    item.pop("references", None)
                    item["gc_stub"] = True
            generations[name] = item
            continue
        top.event(event, value)
    while stream.read(65536):
        pass
    payload = top.value
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    if "generations" in payload:
        payload["generations"] = generations
    return payload, source.hash.hexdigest()


def compact_catalog_stream(chunks, compressed=False, *, current, acknowledged,
                           grace_days=0, now=None):
    """Stream a large Reader catalog and emit only the live generations.

    The canonical catalog can exceed 2 GB with hundreds of superseded
    generations. Decoding it as one object (or deep-copying it) is not
    affordable, so each generation is buffered once and emitted immediately.
    Superseded generations shrink to small handoff tombstones that keep only the
    snapshot identity, never their references.

    ``acknowledged`` must be true only after both deployed consumers were
    verified on this exact ``current`` generation. When it is false nothing is
    dropped, so an unverified consumer can never lose history.
    """
    import ijson
    import json as jsonlib
    try:
        from .reader_lifecycle import CATALOG_VERSION, generation_live, now_iso
    except ImportError:
        from reader_lifecycle import CATALOG_VERSION, generation_live, now_iso
    source = HashingReader(chunks)
    stream = io.BufferedReader(source)
    decoded = gzip.GzipFile(fileobj=stream) if compressed else stream
    stamp = now if isinstance(now, str) else (now.isoformat() if now else now_iso())

    head = {}
    kept = {}
    retired = {}
    name = None
    builder = None
    depth = 0
    section = None          # None, "generations", or "retired_generations"
    dropped = 0
    for prefix, event, value in ijson.parse(decoded, use_float=True):
        if builder is not None:
            builder.event(event, value)
            depth += event in {"start_map", "start_array"}
            depth -= event in {"end_map", "end_array"}
            if depth:
                continue
            item, builder = builder.value, None
            if not isinstance(item, dict):
                if section == "retired_generations":
                    retired[name] = item
                else:
                    raise ValueError("invalid Reader catalog generation")
                continue
            if section == "retired_generations":
                retired[name] = item
                continue
            item.setdefault("generation", name)
            if name == current or not acknowledged:
                kept[name] = item
                continue
            # Both deployed consumers were verified on this exact generation, so
            # the replacement is acknowledged by definition. Record it explicitly
            # rather than relying on entries having written it themselves.
            live = {**item, "replacement_acks": {"hf": True, "pages": True}}
            if generation_live(live, current, grace_days=grace_days, now=stamp):
                kept[name] = item
            else:
                dropped += 1
                snapshot = item.get("snapshot")
                expected = f"reader-index/generations/{name}/reader_assets.json.gz"
                if (not re.fullmatch(r"[0-9a-f]{32}", str(name))
                        or not isinstance(snapshot, dict)
                        or snapshot.get("bucket") != "vomebook/reader-assets-v2"
                        or snapshot.get("path") != expected):
                    # The tombstone must never name a snapshot the collector
                    # would reject; keep the retirement record without one.
                    snapshot = None
                retired[name] = {
                    "replacement_generation": current,
                    "replacement_acks": {"hf": True, "pages": True},
                    "sidecar_sha256": item.get("sidecar_sha256"),
                    "snapshot": snapshot,
                    "retired_at": stamp,
                }
            continue
        # Track which top-level object we are inside.
        if prefix in {"generations", "retired_generations"}:
            section = prefix
            if event == "map_key":
                name = value
            continue
        if prefix.startswith("generations.") or prefix.startswith("retired_generations."):
            if event == "map_key" and prefix.count(".") == 1:
                # A generation key, not an entry field.
                name = value
                continue
            if builder is None:
                builder, depth = ijson.ObjectBuilder(), 0
            builder.event(event, value)
            depth += event in {"start_map", "start_array"}
            depth -= event in {"end_map", "end_array"}
            continue
        if event in {"start_map", "start_array", "end_map", "end_array"} or not prefix:
            continue
        head[prefix] = value
    while stream.read(65536):
        pass
    result = {
        "version": head.get("version", CATALOG_VERSION),
        "kind": head.get("kind", "reader-catalog"),
        "current": {"generation": current,
                    "published_at": head.get("current.published_at")},
        "generations": {key: value for key, value in kept.items() if value is not None},
    }
    tombstones = {key: value for key, value in retired.items() if value is not None}
    if tombstones:
        result["retired_generations"] = tombstones
    encoded = (jsonlib.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode()
    return encoded, source.hash.hexdigest(), {
        "kept": len(result["generations"]),
        "dropped": dropped,
        "retired_generations": len(tombstones)}


def catalog_identity(chunks, compressed=False):
    """Stream only the cursor identity needed before a full compaction pass.

    Returns the current generation, the sidecar digest it binds, and the exact
    wire digest. Nothing else is retained, so this stays bounded on a catalog
    that is several gigabytes.
    """
    import ijson
    source = HashingReader(chunks)
    stream = io.BufferedReader(source)
    decoded = gzip.GzipFile(fileobj=stream) if compressed else stream
    current = None
    names = set()
    bound = None
    for prefix, event, value in ijson.parse(decoded, use_float=True):
        if prefix == "current.generation" and event == "string":
            current = value
            continue
        if prefix == "generations" and event == "map_key":
            names.add(value)
            continue
        if current and prefix == f"generations.{current}.sidecar_sha256" and event == "string":
            bound = value
    while stream.read(65536):
        pass
    if current is None or current not in names:
        raise ValueError("Reader catalog has no current generation")
    return {"current": current, "bound_sidecar_sha256": bound}, source.hash.hexdigest()
