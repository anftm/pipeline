"""Stream graph-relevant JSON without retaining OCR text or positioned spans."""
import gzip
import hashlib
import io


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
