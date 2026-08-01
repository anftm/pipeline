#!/usr/bin/env python3
"""Shared source-file override for the CCRD corpus and Space."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


UPSTREAM_REPO = "https://raw.githubusercontent.com/ProletRevDicta/Prolet"
UPSTREAM_REF = "master"
UPSTREAM_PATHS = (
    "A4 毛泽东主席/Some of Chairman Mao ‘s Instructions 毛主席1975-1976年部分指示补遗.txt",
    "A4 毛泽东主席/Some of Chairman Mao ‘s Instructions 毛主席1975-1976年部分指示补遗【待修改】.txt",
)
CORPUS_PATH = Path("CW/1/Some of Chairman Mao ‘s Instructions 毛主席1975 1976年部分指示补遗【待修改】.txt")
SPACE_PATH = Path("CW/1/Some of Chairman Mao ‘s Instructions 毛主席1975 1976年部分指示补遗【待修改】.txt")


def source_urls() -> tuple[str, ...]:
    return tuple(
        f"{UPSTREAM_REPO}/{UPSTREAM_REF}/{urllib.parse.quote(path, safe='/')}"
        for path in UPSTREAM_PATHS
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_override(destination: Path) -> dict[str, str | int]:
    """Download the preferred upstream name, with a transition fallback."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(prefix="ccrd-source-", suffix=".tmp")
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    selected_url = ""
    try:
        for url in source_urls():
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "VoiceOfML-CCRD-Source-Pipeline/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                selected_url = url
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise RuntimeError(f"failed to download CCRD source: HTTP {exc.code}") from exc
        if not selected_url:
            raise RuntimeError("none of the configured Prolet source filenames exists")
        if temporary.stat().st_size == 0:
            raise RuntimeError("downloaded CCRD source is empty")
        temporary.read_text(encoding="utf-8")
        digest = sha256(temporary)
        size = temporary.stat().st_size
        temporary.replace(destination)
        return {"url": selected_url, "sha256": digest, "bytes": size}
    finally:
        temporary.unlink(missing_ok=True)
