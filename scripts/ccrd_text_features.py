"""Deterministic CW:0 text features matching the CCRD v6 search contract."""
from __future__ import annotations

import hashlib
import re
import unicodedata

TOKEN = re.compile(r'[a-z0-9]+|[\u4e00-\u9fff\u3400-\u4dbf]+')
CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+\Z')
LITERAL_PREFIX = '\0literal:'
MAX_TEXT = 32 * 1024 * 1024


def normalized(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('invalid or oversized CCRD text')
    return unicodedata.normalize('NFKC', text).casefold().replace('\0', ' ')


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode('utf-8')).digest()[:16]


def summarize(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('invalid or oversized CCRD text')
    return re.sub(r'\s+', ' ', text).strip()[:700]


def anchors(text: str) -> dict[bytes, int]:
    value = normalized(text)
    result: dict[bytes, int] = {}
    for match in TOKEN.finditer(value):
        part, start = match.group(), match.start()
        if CJK.fullmatch(part):
            for index, character in enumerate(part):
                result.setdefault(_digest(character), start + index)
            for index in range(len(part) - 1):
                result.setdefault(_digest(part[index:index + 2]), start + index)
        else:
            result.setdefault(_digest(part), start)
    for index in range(len(value) - 2):
        if not TOKEN.fullmatch(value[index + 1]):
            result.setdefault(_digest(LITERAL_PREFIX + value[index:index + 3]), index)
    return result


def token_hashes(text: str) -> set[bytes]:
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('invalid or oversized CCRD text')
    # The legacy builder intentionally hashes literal trigrams before replacing
    # NUL in FTS content. Keep the two token streams distinct.
    value = unicodedata.normalize('NFKC', text).casefold()
    result: set[bytes] = set()
    for part in TOKEN.findall(value):
        if CJK.fullmatch(part):
            result.update(_digest(character) for character in part)
            result.update(_digest(part[index:index + 2]) for index in range(len(part) - 1))
        else:
            result.add(_digest(part))
    for index in range(len(value) - 2):
        if not TOKEN.fullmatch(value[index + 1]):
            result.add(_digest(LITERAL_PREFIX + value[index:index + 3]))
    return result
