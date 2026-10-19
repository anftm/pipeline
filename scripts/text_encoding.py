"""Strict source-text normalization shared with the existing TXT publisher's scores.

Round trips prove byte preservation, not the language of an ambiguous codepage.
Ambiguous legacy inputs require an explicit per-source encoding override.
"""
from __future__ import annotations

import codecs
from collections import Counter
import hashlib
import re

try:
    from .publish_source_txt_once import STRICT_ENCODINGS, looks_like_lfs_pointer, text_quality_score
except ImportError:
    from publish_source_txt_once import STRICT_ENCODINGS, looks_like_lfs_pointer, text_quality_score

PROFILE = "strict-utf8-bom-v1"
UTF8_BOM = codecs.BOM_UTF8
COMMON_CJK = set("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经之进着等部家自理起现实都体制当本性应开合因由然前外政社义事相全与关各重新内正反明原利质向道命此变结解问意建公系军情者立代通题党程展料员革文总品活长求老基资级图统知组别期论运农指区战任处理世")

# Manually reviewed, user-authorized source repairs. These are not decoding
# guesses or a caller-supplied replacement API. Each recipe accepts only the
# complete original SHA and one exact byte span, retaining all other content.
REVIEWED_SOURCE_REPAIRS = {
    "mlmrl-1967-02-01-mojibake-v1": {
        "source_sha256": "da24e640b710afb49ba75a7a107047da066b202d29b0115d185729bbc5b34805",
        "offset": 9484,
        "original_text": "穹\0瘢\0炊\0哦荆\0驮闾ち巳嗣癫撇\0\0腔挂\0\0墒裁矗?/p>",
        "replacement_text": "民服务，反而放毒，就糟踏了人民财产，那还要它干什么？",
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/061-090/073.html",
        "reference_sha256": "0a293f7f424650fbf4d5e91b85dc369841961c04fe2420bc36adb520a5f099ee",
    },
    "mlmrl-1967-04-16-mojibake-v1": {
        "source_sha256": "7088b4a442c02e818ee63a26c9d30952fb1a78ce6fb35afe57aa75f071449f27",
        "offset": 9761,
        "original_text": "\0囊欢\0浴？\0淮未蠡岵荒苌钊氲母恪Ｔ谒枷肷希\0谡苎Х矫妫\0褂懈\0喙ぷ饕\0觯\0嬲\0\0鞑檠芯俊８闩碚嬲庑┡淹讲皇潜本┒\0翘旖蚋愕模\0比灰灿斜本┮恍┎牧峡垂\0恕Ｎ颐且\0\0徊礁呔倜\0蠖\0枷胛按蠛炱焯乇鹨\0汛蠓较蜃ソ簦\0鞑榕碚嫖侍夂苌伲\0\0奈侍庑枰\0阋桓恪?/p>",
        "replacement_text": (
            "样的一动脑。开一次大会不能深入的搞。在思想上，在哲学方面，还有更多工作要做，真正要调查研究。"
            "搞彭真这些叛徒不是北京而是天津搞的，当然也有北京一些材料看过了。"
            "我们要进一步高举毛泽东思想伟大红旗特别要把大方向抓紧，调查彭真问题很少，他的问题需要搞一搞。"
        ),
        "reference_url": "https://www.prisoncensorship.info/archive/etext/chinese/classics/jiangqing1966to1976/061-090/088.html",
        "reference_sha256": "997d95c4b48cb826561209963d5d77ea100d5366f36b538c61dcacd16d6e5774",
    },
}


class TextEncodingError(ValueError):
    pass


def validate_text(text: str, *, preserve_controls: dict | None = None) -> str:
    approved = {} if preserve_controls is None else preserve_controls
    if (not isinstance(approved, dict) or any(key != "U+001A" or type(count) is not int or count < 1
                                             for key, count in approved.items())):
        raise TextEncodingError("only exact reviewed U+001A counts may be preserved")
    controls = Counter(f"U+{ord(c):04X}" for c in text if ord(c) < 32 and c not in "\t\n\r\f")
    if controls != approved:
        raise TextEncodingError("text contains NUL or binary control bytes")
    # Encoding is strict: no surrogate replacement, whitespace cleanup, newline
    # normalization or removal of a literal Unicode replacement character.
    text.encode("utf-8", errors="strict")
    return text


def decode_windows_936(raw: bytes) -> str:
    """Strict Python GBK plus Windows' single-byte 0x80 euro, never replacement.

    A GBK trailing 0x80 belongs to its two-byte character, not to the euro mapping.
    All chunks still use the strict codec and the complete result must round-trip.
    Undefined CP936 extensions remain errors rather than guessed characters.
    """
    parts, start, cursor = [], 0, 0
    while cursor < len(raw):
        byte = raw[cursor]
        if byte == 0x80:
            parts.extend((raw[start:cursor].decode("gbk", errors="strict"), "€"))
            cursor += 1
            start = cursor
        else:
            cursor += 2 if 0x81 <= byte <= 0xfe else 1
    parts.append(raw[start:].decode("gbk", errors="strict"))
    text = "".join(parts)
    if b"\x80".join(part.encode("gbk", errors="strict") for part in text.split("€")) != raw:
        raise TextEncodingError("Windows-936 source does not round-trip")
    return text


def decode_source_text(raw: bytes, *, hint: str = "", encoding: str | None = None,
                       preserve_controls: dict | None = None) -> tuple[str, str, str]:
    if looks_like_lfs_pointer(raw):
        raise TextEncodingError("source is a Git LFS pointer, not document text")
    if preserve_controls is not None and not encoding:
        raise TextEncodingError("reviewed controls require an explicit encoding")
    if encoding:
        if encoding.lower().replace("_", "-") == "windows-936":
            return validate_text(decode_windows_936(raw), preserve_controls=preserve_controls), "windows-936", "explicit"
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
        text = validate_text(raw.decode(canonical, errors="strict"), preserve_controls=preserve_controls)
        if preserve_controls and text.encode(canonical, errors="strict") != raw:
            raise TextEncodingError("reviewed-control source does not round-trip")
        return text, canonical, "explicit"
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


def normalize_utf8(raw: bytes, *, hint: str = "", encoding: str | None = None,
                    preserve_controls: dict | None = None,
                    remove_prefix_byte: dict | None = None,
                    reviewed_source_repair: str | None = None) -> tuple[bytes, dict]:
    original = raw
    if reviewed_source_repair is not None:
        if (not encoding or remove_prefix_byte is not None or preserve_controls is not None
                or not isinstance(reviewed_source_repair, str) or reviewed_source_repair not in REVIEWED_SOURCE_REPAIRS):
            raise TextEncodingError("source repair requires one known reviewed recipe, explicit UTF-8 and no other repair")
        try:
            codec = codecs.lookup(encoding).name
        except (LookupError, TypeError) as error:
            raise TextEncodingError("unknown explicit text encoding") from error
        if codec not in {"utf-8", "utf-8-sig"}:
            raise TextEncodingError("reviewed source repair is limited to strict UTF-8 originals")
        repair = REVIEWED_SOURCE_REPAIRS[reviewed_source_repair]
        if hashlib.sha256(raw).hexdigest() != repair["source_sha256"]:
            raise TextEncodingError("source differs from the complete approved repair SHA-256")
        raw.decode("utf-8", errors="strict")
        offset = repair["offset"]
        old_span = repair["original_text"].encode("utf-8", errors="strict")
        new_span = validate_text(repair["replacement_text"]).encode("utf-8", errors="strict")
        if raw[offset:offset + len(old_span)] != old_span:
            raise TextEncodingError("reviewed source span differs from original bytes")
        raw = raw[:offset] + new_span + raw[offset + len(old_span):]
    if remove_prefix_byte is not None:
        removal = remove_prefix_byte
        if (not encoding or not isinstance(removal, dict) or set(removal) != {"offset", "hex"}
                or type(removal["offset"]) is not int or not 0 <= removal["offset"] < min(4096, len(raw))
                or not isinstance(removal["hex"], str) or not re.fullmatch(r"[0-9a-f]{2}", removal["hex"])):
            raise TextEncodingError("prefix-byte repair needs explicit encoding and one exact reviewed byte within 4096 bytes")
        offset = removal["offset"]
        if raw[offset:offset + 1].hex() != removal["hex"]:
            raise TextEncodingError("reviewed prefix byte does not match source")
        raw = raw[:offset] + raw[offset + 1:]
    text, source_encoding, decision = decode_source_text(raw, hint=hint, encoding=encoding,
                                                       preserve_controls=preserve_controls)
    normalized = UTF8_BOM + text.encode("utf-8", errors="strict")
    if normalized.decode("utf-8-sig", errors="strict") != text:
        raise TextEncodingError("UTF-8 verification failed")
    metadata = {"encoding": "utf-8", "bom": True, "profile": PROFILE,
                "source_encoding": source_encoding, "encoding_decision": decision,
                "characters": len(text)}
    if preserve_controls:
        metadata["preserved_controls"] = dict(preserve_controls)
    if remove_prefix_byte is not None:
        retained = (b"\x80".join(part.encode("gbk", errors="strict") for part in text.split("€"))
                    if source_encoding == "windows-936" else text.encode(source_encoding, errors="strict"))
        if retained != raw or retained[:offset] + bytes.fromhex(removal["hex"]) + retained[offset:] != original:
            raise TextEncodingError("reviewed prefix repair does not reconstruct the complete original")
        metadata["removed_prefix_byte"] = dict(removal)
        metadata["normalization_recipe"] = "strict-decode-with-reviewed-prefix-byte-removal-v1"
    if reviewed_source_repair is not None:
        retained = text.encode(source_encoding, errors="strict")
        reconstructed = retained[:offset] + old_span + retained[offset + len(new_span):]
        if retained != raw or reconstructed != original:
            raise TextEncodingError("reviewed source repair does not reconstruct the complete original")
        metadata["reviewed_source_repair"] = reviewed_source_repair
        metadata["replaced_source_span"] = {
            "offset": offset, "original_bytes": len(old_span), "replacement_bytes": len(new_span),
            "original_text": repair["original_text"], "replacement_text": repair["replacement_text"],
            "reference_url": repair["reference_url"], "reference_sha256": repair["reference_sha256"],
        }
        metadata["normalization_recipe"] = "strict-utf8-reviewed-source-repair-v1"
    if source_encoding == "windows-936" or preserve_controls or remove_prefix_byte is not None or reviewed_source_repair is not None:
        metadata["source_roundtrip_verified"] = True
    return normalized, metadata
