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
    skip_value, skip_depth = False, 0
    for prefix, event, value in ijson.parse(decoded, use_float=True):
        if skip_depth:
            skip_depth += event in {"start_map", "start_array"}
            skip_depth -= event in {"end_map", "end_array"}
        elif skip_value:
            skip_value = False
            skip_depth = int(event in {"start_map", "start_array"})
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
