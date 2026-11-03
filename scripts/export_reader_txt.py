#!/usr/bin/env python3
"""Export complete verified OCR/effective text to plain UTF-8; no remote writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile

try:
    from . import pdf_reading_v3 as v3, pdf_text_layer as text, reader_text_admission as admission
    from .reader_text_storage import VerifiedReader, TextPageSpool
except ImportError:
    import pdf_reading_v3 as v3
    import pdf_text_layer as text
    import reader_text_admission as admission
    from reader_text_storage import VerifiedReader, TextPageSpool


RECIPE = "ocr-plain-utf8-pages-v1"
PAGE_SEPARATOR = "\n\n"


class PermanentTextRejection(ValueError):
    """The source's own text is unusable; retrying the same bytes cannot help."""


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def source_identity(spec: dict) -> tuple[str, str, int]:
    repo, path = spec.get("origin_repo"), spec.get("origin_rel_path")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("invalid origin repository")
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 for char in path)):
        raise ValueError("invalid original relative path")
    revision = spec.get("origin_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("origin must be pinned to a commit")
    number = spec.get("doc_number")
    if type(number) is not int or not 1 <= number < (1 << 63):
        raise ValueError("explicit stable OCR doc_number is required")
    text.sha(spec.get("source_sha256"))
    text.sha(spec.get("origin_sha256"))
    return repo, path, number


def build_export(spec: dict, read, *, allow_unreviewed: bool = False, max_input_bytes=None,
                 cache_bytes=8 * 1024 * 1024) -> dict:
    checked_read = VerifiedReader(read, cache_bytes=cache_bytes, max_input_bytes=max_input_bytes)
    with TextPageSpool() as layers:
        return _build_export(spec, checked_read, layers, allow_unreviewed=allow_unreviewed)


def _build_export(spec, checked_read, layers, *, allow_unreviewed):
    repo, original, number = source_identity(spec)
    count = spec.get("page_count")
    if type(count) is not int or count < 1:
        raise ValueError("complete page count is required")
    ref = v3.resource(spec.get("text_manifest"))
    manifest = v3.verify_text_bundle(ref, checked_read, spec["source_sha256"], count, verified_layers=layers)
    references = {(ref["bucket"], ref["path"]): dict(ref)}
    if spec.get("reading_manifest"):
        reading_ref = v3.resource(spec["reading_manifest"])
        reading = v3.validate_reading_manifest(v3.decode(checked_read(reading_ref)))
        if (reading["source_key"] != repo + "\0" + original or reading["source_sha256"] != spec["source_sha256"]
                or reading["page_count"] != count or reading.get("text_layer") != ref):
            raise ValueError("text export differs from its pinned reading manifest")
        references[(reading_ref["bucket"], reading_ref["path"])] = dict(reading_ref)
    if spec["origin_sha256"] != spec["source_sha256"]:
        derivation_ref = v3.resource(spec.get("derivation_manifest"))
        derivation = v3.decode(checked_read(derivation_ref))
        if derivation.get("kind") == "pdf-derived-source":
            valid = (derivation.get("version") == 1
                     and derivation.get("repo") == repo and derivation.get("source_path") == original
                     and derivation.get("source_revision") == spec["origin_revision"]
                     and derivation.get("source_sha256") == spec["origin_sha256"]
                     and derivation.get("derived_sha256") == spec["source_sha256"])
        elif derivation.get("kind") == "static-pdf-document-stream":
            # Office/PostScript/static-document sources are converted once into
            # a document-owned PDF. That manifest names the original source
            # digest and the converted PDF digest, but not a repo/path identity.
            valid = (derivation.get("version") == 1
                     and derivation.get("path") == "document.pdf"
                     and derivation.get("source_sha256") == spec["origin_sha256"]
                     and derivation.get("sha256") == spec["source_sha256"])
        else:
            valid = False
        if not valid:
            raise ValueError("OCR primary differs from its pinned original derivation")
        references[(derivation_ref["bucket"], derivation_ref["path"])] = dict(derivation_ref)
        if derivation.get('symbol_conversion'):
            evidence = v3.resource(derivation['symbol_conversion'])
            conversion = v3.decode(checked_read(evidence))
            if conversion.get('source_sha256') != spec['origin_sha256']:
                raise ValueError('symbol conversion differs from the original source')
            references[(evidence['bucket'], evidence['path'])] = dict(evidence)

    def retain(value):
        if isinstance(value, dict):
            if {"bucket", "path", "sha256", "bytes", "role"} <= value.keys():
                v3.resource(value)
                key = (value["bucket"], value["path"])
                previous = references.get(key)
                if previous and (previous["sha256"], previous["bytes"]) != (value["sha256"], value["bytes"]):
                    raise ValueError("conflicting retained resource identity")
                references[key] = dict(value)
            for child in value.values():
                if isinstance(child, (dict, list)): retain(child)
        elif isinstance(value, list):
            for child in value: retain(child)

    retain(manifest)
    for partition in manifest["partitions"]:
        entries = v3.decode(checked_read(partition["resource"]))
        retain(entries)
    for layer in layers:
        retain(layer)
    if [layer["page"] for layer in layers] != list(range(1, count + 1)):
        raise ValueError("export does not cover every physical page")
    accepted = sum(layer["revision"] == "accepted" for layer in layers)
    if not accepted and not allow_unreviewed:
        raise ValueError("unreviewed OCR needs explicit admission; no model is called")
    body_admission = admission.check(spec, layers, checked_read)
    parts, pages, offset = [], [], 0
    for index, layer in enumerate(layers):
        if index:
            parts.append(PAGE_SEPARATOR)
            offset += len(PAGE_SEPARATOR)
        content = layer["text"]
        if any(ord(char) < 32 and char not in "\t\r\n" for char in content):
            raise PermanentTextRejection(
                "OCR text contains binary controls; keep the source quarantined")
        pages.append({"page": layer["page"], "start": offset, "end": offset + len(content),
                      "text_sha256": hashlib.sha256(content.encode()).hexdigest(),
                      "text_generation": layer["generation"], "quality": layer["quality"]})
        parts.append(content)
        offset += len(content)
    raw = "".join(parts).encode("utf-8")
    if not any(layer["text"].strip() for layer in layers):
        raise PermanentTextRejection("empty book cannot be admitted as readable OCR")
    if not any(char.isalnum() for layer in layers for char in layer["text"]):
        raise PermanentTextRejection(
            "OCR has no usable recognized text; inspect the source conversion")
    digest = hashlib.sha256(raw).hexdigest()
    original_path = PurePosixPath(original)
    readable = repo + "/" + str(original_path.with_suffix(".txt"))
    book_id = hashlib.sha256((repo + "\0" + original).encode()).hexdigest()
    identity = {"recipe": RECIPE, "origin_repo": repo, "origin_rel_path": original,
                "origin_revision": spec["origin_revision"], "origin_sha256": spec["origin_sha256"],
                "source_sha256": spec["source_sha256"],
                "doc_id": f"OCR:{number}", "text_generation": manifest["generation"], "txt_sha256": digest,
                'body_admission_sha256': text.digest(body_admission)}
    generation = text.digest(identity)
    object_path = f"text/objects/{digest[:2]}/{digest}.txt"
    export = {"version": 1, "kind": "ccrd-ocr-text-export", "complete": True,
              "generation": generation, "book_id": book_id, **identity,
              "title": original_path.stem, "readable_rel_path": readable, "page_count": count,
              "source_sha256_role": "reader-primary",
              "accepted_pages": accepted, "quality": "partially-reviewed" if accepted else "unreviewed",
               "offset_unit": "unicode-codepoint", "page_separator": PAGE_SEPARATOR, "pages": pages,
              'body_admission': body_admission,
              "text": {"bucket": "vomebook/ccrd-index", "path": object_path, "sha256": digest,
                       "bytes": len(raw), "role": "runtime"},
              "retained_reader_resources": [references[key] for key in sorted(references)]}
    export_path = f"text/exports/{book_id}/{generation}/manifest.json"
    return {"manifest": export, "manifest_path": export_path,
            "files": {object_path: raw, export_path: json_bytes(export)},
            "input_objects_read": len(checked_read.identities), "input_bytes_read": checked_read.input_bytes,
            'peak_cached_input_bytes': checked_read.peak_cache_bytes}


def export_specs(value, read, output: Path, *, allow_unreviewed=False):
    """Stage a complete batch atomically, keeping stable IDs and accepted text."""
    specs = [value] if isinstance(value, dict) else value
    if not isinstance(specs, list) or not 1 <= len(specs) <= 32:
        raise ValueError('expected one export spec or a batch of 1..32 specs')
    identities, numbers = set(), set()
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError('every export spec must be an object')
        repo, path, number = source_identity(spec)
        if (repo, path) in identities or number in numbers:
            raise ValueError('duplicate original source or stable OCR ID in export batch')
        identities.add((repo, path))
        numbers.add(number)
    if output.exists():
        raise ValueError('use a fresh export directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    refs, receipts = [], []
    with tempfile.TemporaryDirectory(prefix='ocr-export-', dir=output.parent) as directory:
        staging = Path(directory) / 'candidate'
        staging.mkdir()
        for spec in specs:
            result = build_export(spec, read, allow_unreviewed=allow_unreviewed)
            for relative, raw in result['files'].items():
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != raw:
                    raise ValueError('conflicting immutable export object')
                target.write_bytes(raw)
            manifest = result['manifest']
            raw = result['files'][result['manifest_path']]
            refs.append({'bucket': 'vomebook/ccrd-index', 'path': result['manifest_path'],
                         'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'role': 'provenance'})
            receipts.append({'manifest_path': result['manifest_path'], 'doc_id': manifest['doc_id'],
                             'generation': manifest['generation'], 'pages': manifest['page_count'],
                             'accepted_pages': manifest['accepted_pages']})
        receipt = {'exports': receipts, 'books': len(receipts), 'pages': sum(r['pages'] for r in receipts),
                   'remote_writes': 0, 'model_calls': 0,
                   'publication_gate': 'four-bucket reference closure and coherent CCRD release required'}
        if len(receipts) == 1:
            receipt.update(receipts[0])
        (staging / 'export-receipt.json').write_bytes(json_bytes(receipt))
        (staging / 'export-refs.json').write_bytes(json_bytes(refs))
        staging.rename(output)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    reader = parser.add_mutually_exclusive_group(required=True)
    reader.add_argument("--mirror", type=Path, help="Local verified objects at bucket/path")
    reader.add_argument("--remote", action="store_true", help="Read exact qualified resources; still no uploads")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-unreviewed", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("use a fresh export directory")
    if args.remote:
        try:
            from .reader_bucket_store import HubBucketStore
        except ImportError:
            from reader_bucket_store import HubBucketStore
        store = HubBucketStore()
        def read(resource):
            return store.read_bytes(resource["bucket"], resource["path"])
    else:
        mirror = args.mirror.resolve()
        def read(resource):
            target = (mirror / resource["bucket"] / resource["path"]).resolve()
            if mirror not in target.parents:
                raise ValueError("resource outside mirror")
            return target.read_bytes()
    receipt = export_specs(json.loads(args.spec.read_text()), read, args.output,
                           allow_unreviewed=args.allow_unreviewed)
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
