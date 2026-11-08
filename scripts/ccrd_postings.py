"""Bounded canonical postings codec used by the CCRD CW update candidate."""
from __future__ import annotations

MAX_DOC_NUMBER = (1 << 63) - 1


def decode(data: bytes, *, expected_count: int, maximum: int) -> list[int]:
    if (not isinstance(data, bytes) or type(maximum) is not int or maximum < 0
            or type(expected_count) is not int or not 0 <= expected_count <= maximum):
        raise ValueError('invalid postings input')
    numbers: list[int] = []
    value = shift = previous = 0
    for byte in data:
        value |= (byte & 0x7f) << shift
        if value > MAX_DOC_NUMBER or shift > 63:
            raise ValueError('postings number exceeds limit')
        if byte & 0x80:
            shift += 7
            continue
        if shift and byte == 0:
            raise ValueError('postings varint is not canonical')
        number = previous + value
        if number > MAX_DOC_NUMBER or (numbers and number <= previous):
            raise ValueError('postings document IDs are not strictly increasing')
        numbers.append(number)
        if len(numbers) > expected_count:
            raise ValueError('postings document count exceeds declared count')
        previous = number
        value = shift = 0
    if shift or len(numbers) != expected_count:
        raise ValueError('postings are truncated or have a mismatched document count')
    return numbers


def encode(numbers: list[int]) -> bytes:
    if not isinstance(numbers, list):
        raise TypeError('document numbers must be a list')
    output = bytearray()
    previous = 0
    for index, number in enumerate(numbers):
        if type(number) is not int or not 0 <= number <= MAX_DOC_NUMBER or (index and number <= previous):
            raise ValueError('postings document IDs must be strictly increasing non-negative integers')
        delta = number - previous
        while delta >= 0x80:
            output.append((delta & 0x7f) | 0x80)
            delta >>= 7
        output.append(delta)
        previous = number
    return bytes(output)
