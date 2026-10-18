"""Strict source-text normalization shared with the existing TXT publisher's scores.

Round trips prove byte preservation, not the language of an ambiguous codepage.
Ambiguous legacy inputs require an explicit per-source encoding override.
"""
from __future__ import annotations

import codecs
import re

try:
    from .publish_source_txt_once import STRICT_ENCODINGS, looks_like_lfs_pointer, text_quality_score
except ImportError:
    from publish_source_txt_once import STRICT_ENCODINGS, looks_like_lfs_pointer, text_quality_score

PROFILE = "strict-utf8-bom-v1"
UTF8_BOM = codecs.BOM_UTF8
COMMON_CJK = set("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经之进着等部家自理起现实都体制当本性应开合因由然前外政社义事相全与关各重新内正反明原利质向道命此变结解问意建公系军情者立代通题党程展料员革文总品活长求老基资级图统知组别期论运农指区战任处理世")


class TextEncodingError(ValueError):
    pass


def validate_text(text: str) -> str:
    if any(ord(c) < 32 and c not in "\t\n\r\f" for c in text):
        raise TextEncodingError("text contains NUL or binary control bytes")
    # Encoding is strict: no surrogate replacement, whitespace cleanup, newline
    # normalization or removal of a literal Unicode replacement character.
    text.encode("utf-8", errors="strict")
    return text


def decode_source_text(raw: bytes, *, hint: str = "", encoding: str | None = None) -> tuple[str, str, str]:
    if looks_like_lfs_pointer(raw):
        raise TextEncodingError("source is a Git LFS pointer, not document text")
    if encoding:
        try:
            canonical = codecs.lookup(encoding).name
        except LookupError as error:
            raise TextEncodingError("unknown explicit text encoding") from error
        allowed = {*STRICT_ENCODINGS, "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                   "utf-32", "utf-32-le", "utf-32-be", "cp1251", "cp1252", "iso8859-1"}
        if canonical not in {codecs.lookup(name).name for name in allowed}:
            raise TextEncodingError("unsupported explicit text encoding")
        if canonical == "utf-8" and raw.startswith(UTF8_BOM):
            canonical = "utf-8-sig"
        return validate_text(raw.decode(canonical, errors="strict")), canonical, "explicit"
    for signature, codec in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                             (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                             (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(signature):
            return validate_text(raw.decode(codec, errors="strict")), codec, "bom"
    even, odd = raw[::2].count(0), raw[1::2].count(0)
    if odd > len(raw) / 8 and odd > even * 4:
        return validate_text(raw.decode("utf-16-le", errors="strict")), "utf-16-le", "nul-parity"
    if even > len(raw) / 8 and even > odd * 4:
        return validate_text(raw.decode("utf-16-be", errors="strict")), "utf-16-be", "nul-parity"
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        return validate_text(text), "utf-8", "strict-utf8"
    if re.search(r"[\u0400-\u04ff]", hint):
        text = raw.decode("cp1251", errors="strict")
        if text.encode("cp1251") != raw:
            raise TextEncodingError("legacy source does not round-trip")
        return validate_text(text), "cp1251", "cyrillic-path"
    candidates = {}
    probe = raw[:65536]
    for codec in STRICT_ENCODINGS:
        if codec == "utf-8":
            continue
        try:
            decoder = codecs.getincrementaldecoder(codec)(errors="strict")
            text = validate_text(decoder.decode(probe, final=len(probe) == len(raw)))
            pending = decoder.getstate()[0]
            if text.encode(codec, errors="strict") + pending != probe:
                continue
        except (UnicodeError, TextEncodingError):
            continue
        sample = text[:20000]
        score = text_quality_score(text) + sum(c in COMMON_CJK for c in sample) * 5
        # GBK/GB18030 aliases producing identical text are one interpretation.
        candidates.setdefault(text, (score, codec))
    ranked = sorted(((score, codec, text) for text, (score, codec) in candidates.items()), reverse=True)
    if not ranked or ranked[0][0] <= 0:
        raise TextEncodingError("no strict legacy decoding; provide an encoding override")
    score, codec, text = ranked[0]
    if len(ranked) > 1 and score - ranked[1][0] <= max(4, len(text[:20000]) * .02):
        raise TextEncodingError("ambiguous legacy encoding; provide an encoding override")
    text = validate_text(raw.decode(codec, errors="strict"))
    if text.encode(codec, errors="strict") != raw:
        raise TextEncodingError("legacy source does not round-trip")
    return text, codec, "legacy-score"


def normalize_utf8(raw: bytes, *, hint: str = "", encoding: str | None = None) -> tuple[bytes, dict]:
    text, source_encoding, decision = decode_source_text(raw, hint=hint, encoding=encoding)
    normalized = UTF8_BOM + text.encode("utf-8", errors="strict")
    if normalized.decode("utf-8-sig", errors="strict") != text:
        raise TextEncodingError("UTF-8 verification failed")
    return normalized, {"encoding": "utf-8", "bom": True, "profile": PROFILE,
                        "source_encoding": source_encoding, "encoding_decision": decision,
                        "characters": len(text)}
