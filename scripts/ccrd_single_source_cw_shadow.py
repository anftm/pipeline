"""In-memory CW index equivalence model; never opens or writes an index.

This shadow follows the historical builder's observed token, posting, FTS and
snippet rules. It is deliberately not a SQLite producer or deployment path.
Inputs must be supplied locally; this module does not read the network or use
OCR. Verification against the exact trusted builder remains a separate gate.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import re
import unicodedata


TOKEN_RE = re.compile(r'[a-z0-9]+|[\u4e00-\u9fff\u3400-\u4dbf]+')
CHINESE_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')
LITERAL_PREFIX = '\0literal:'
TARGET = 'CW:0'
MAX_NUMBER = (1 << 63) - 1


def normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def fts_content(text: str) -> str:
    return normalize(text).replace('\0', ' ')


def token_hashes(text: str) -> set[bytes]:
    normalized = normalize(text)
    result = set()
    for part in TOKEN_RE.findall(normalized):
        tokens = ({*part, *(part[index:index + 2] for index in range(len(part) - 1))}
                  if CHINESE_RE.fullmatch(part) else {part})
        result.update(hashlib.sha256(token.encode()).digest()[:16] for token in tokens)
    for index in range(len(normalized) - 2):
        if not TOKEN_RE.fullmatch(normalized[index + 1]):
            token = LITERAL_PREFIX + normalized[index:index + 3]
            result.add(hashlib.sha256(token.encode()).digest()[:16])
    return result


def snippet_anchors(text: str) -> dict[bytes, int]:
    normalized = fts_content(text)
    anchors = {}

    def add(token, offset):
        anchors.setdefault(hashlib.sha256(token.encode()).digest()[:16], offset)

    for match in TOKEN_RE.finditer(normalized):
        part, start = match.group(), match.start()
        if CHINESE_RE.fullmatch(part):
            for index, character in enumerate(part):
                add(character, start + index)
            for index in range(len(part) - 1):
                add(part[index:index + 2], start + index)
        else:
            add(part, start)
    for index in range(len(normalized) - 2):
        if not TOKEN_RE.fullmatch(normalized[index + 1]):
            add(LITERAL_PREFIX + normalized[index:index + 3], index)
    return anchors


def number(doc_id: str) -> int:
    if not isinstance(doc_id, str) or not re.fullmatch(r'CW:(0|[1-9][0-9]*)', doc_id):
        raise ValueError('invalid canonical CW document ID')
    value = int(doc_id[3:])
    if value > MAX_NUMBER:
        raise ValueError('CW document ID exceeds SQLite signed integer range')
    return value


def encode_doc_ids(numbers) -> bytes:
    values = list(numbers)
    if any(type(value) is not int or not 0 <= value <= MAX_NUMBER for value in values):
        raise ValueError('invalid or duplicate posting IDs')
    values.sort()
    if len(values) != len(set(values)):
        raise ValueError('invalid or duplicate posting IDs')
    previous, output = 0, bytearray()
    for value in values:
        delta = value - previous
        previous = value
        while delta >= 0x80:
            output.append((delta & 0x7f) | 0x80)
            delta >>= 7
        output.append(delta)
    return bytes(output)


def decode_doc_ids(encoded: bytes) -> list[int]:
    if not isinstance(encoded, bytes):
        raise ValueError('posting bytes required')
    values, delta, shift, previous = [], 0, 0, 0
    for byte in encoded:
        delta |= (byte & 0x7f) << shift
        if byte & 0x80:
            shift += 7
            if shift > 63:
                raise ValueError('posting varint exceeds integer range')
            continue
        value = previous + delta
        if value > MAX_NUMBER or values and value <= previous:
            raise ValueError('posting IDs must be distinct and ascending')
        values.append(value)
        previous, delta, shift = value, 0, 0
    if shift or encode_doc_ids(values) != encoded:
        raise ValueError('truncated or noncanonical posting bytes')
    return values


def build(documents: dict[str, str]) -> dict:
    """Construct an in-memory semantic model from already-decoded CW text."""
    if not isinstance(documents, dict) or TARGET not in documents:
        raise ValueError('complete CW fixture must include CW:0')
    summaries, fulltext, anchors = {}, {}, {}
    postings = defaultdict(set)
    for doc_id, text in documents.items():
        index = number(doc_id)
        if not isinstance(text, str):
            raise ValueError('document text must be decoded before modeling')
        summaries[doc_id] = re.sub(r'\s+', ' ', text).strip()[:700]
        fulltext[index] = fts_content(text)
        anchors[index] = snippet_anchors(text)
        for token in token_hashes(text):
            postings[token].add(index)
    return {'documents': summaries, 'content_fts': fulltext, 'snippet_anchors': anchors,
            'postings': {token: encode_doc_ids(ids) for token, ids in postings.items()},
            'publication_ready': False, 'remote_writes': 0}


def replace_target(old: dict, current: str, new: str, *, expected_ids: set[str]) -> dict:
    """Model replacing CW:0, rejecting mismatched base target and posting data."""
    if not isinstance(current, str) or not isinstance(new, str):
        raise ValueError('target content must be decoded text')
    fields = {'documents', 'content_fts', 'snippet_anchors', 'postings'}
    if (not isinstance(old, dict) or set(old) != fields | {'publication_ready', 'remote_writes'}
            or old['publication_ready'] is not False or old['remote_writes'] != 0
            or any(not isinstance(old[field], dict) for field in fields)):
        raise ValueError('unknown CW semantic index structure')
    doc_ids = set(old['documents'])
    if doc_ids != expected_ids or TARGET not in doc_ids:
        raise ValueError('index document IDs differ from the complete expected set')
    ids = {number(doc_id) for doc_id in doc_ids}
    if set(old['content_fts']) != ids or set(old['snippet_anchors']) != ids:
        raise ValueError('index FTS/anchor document IDs differ from metadata')
    old_summary = re.sub(r'\s+', ' ', current).strip()[:700]
    if (old['documents'][TARGET] != old_summary or old['content_fts'][0] != fts_content(current)
            or old['snippet_anchors'][0] != snippet_anchors(current)):
        raise ValueError('old CW:0 does not match the current target text')
    old_tokens, new_tokens = token_hashes(current), token_hashes(new)
    posting_sets = {}
    for token, encoded in old['postings'].items():
        if not isinstance(token, bytes) or len(token) != 16:
            raise ValueError('invalid posting token')
        values = set(decode_doc_ids(encoded))
        if not values or not values <= ids or (0 in values) != (token in old_tokens):
            raise ValueError('posting references or current target tokens are inconsistent')
        posting_sets[token] = values
    if any(token not in posting_sets for token in old_tokens):
        raise ValueError('old target posting is missing')
    for token in old_tokens | new_tokens:
        values = posting_sets.setdefault(token, set())
        values.discard(0)
        if token in new_tokens:
            values.add(0)
        if not values:
            del posting_sets[token]
    documents = dict(old['documents'])
    documents[TARGET] = re.sub(r'\s+', ' ', new).strip()[:700]
    fulltext = dict(old['content_fts'])
    fulltext[0] = fts_content(new)
    anchors = {key: dict(value) for key, value in old['snippet_anchors'].items()}
    anchors[0] = snippet_anchors(new)
    return {'documents': documents, 'content_fts': fulltext, 'snippet_anchors': anchors,
            'postings': {token: encode_doc_ids(values) for token, values in posting_sets.items()},
            'publication_ready': False, 'remote_writes': 0}
