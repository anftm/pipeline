"""Types for the directly used part of Brotli's public Python API."""

def compress(data: bytes, mode: int = ..., quality: int = ..., lgwin: int = ...,
             lgblock: int = ...) -> bytes: ...
