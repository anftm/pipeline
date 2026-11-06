"""Small native-text and deterministic JSON helpers shared by Reader modules.

This module does not load recognition models or generate OCR text. PDF native
text remains the Reader's responsibility; DjVu helpers use the utilities below.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
from pathlib import Path


def clean_text(value: str) -> str:
    value = html.unescape(str(value or "")).replace("\x00", "")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def normalize_blocks(blocks, width: float, height: float) -> list[dict]:
    width = max(1.0, float(width or 1))
    height = max(1.0, float(height or 1))
    output = []
    for block in blocks or []:
        value = clean_text(block.get("text", ""))
        if not value:
            continue
        x0 = max(0.0, min(1.0, float(block.get("x0", 0)) / width))
        y0 = max(0.0, min(1.0, float(block.get("y0", 0)) / height))
        x1 = max(x0, min(1.0, float(block.get("x1", 0)) / width))
        y1 = max(y0, min(1.0, float(block.get("y1", 0)) / height))
        item = {"t": value, "b": [x0, y0, x1, y1],
                "c": round(max(0.0, min(1.0, float(block.get("confidence", 1)))), 4),
                "s": block.get("source", "ocr")}
        if block.get("polygon"):
            item["q"] = [[max(0, min(1, x / width)), max(0, min(1, y / height))]
                         for x, y in block["polygon"]]
        output.append(item)
    output.sort(key=lambda item: (item["b"][1], item["b"][0]))
    return output


def write_json(path: Path, payload: dict, *, compressed: bool = False) -> tuple[str, int]:
    if compressed:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        raw = gzip.compress(raw, compresslevel=9, mtime=0)
    else:
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)
